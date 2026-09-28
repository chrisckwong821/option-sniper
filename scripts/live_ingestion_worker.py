"""SDK-only child. Callback ingestion avoids SDK async-iterator locking."""
import asyncio
import base64
import collections
import contextlib
import json
import os
import sys
import threading
import time

import databento as db


class PendingRecords:
    def __init__(self, limit=20000, definition_expiry=None):
        self.limit = limit
        self.definition_expiry = definition_expiry
        self.lock = threading.Lock()
        self.control = collections.deque()
        self.latest = collections.OrderedDict()
        self.last_record = time.monotonic()
        self.overflow = False

    def add(self, record):
        if isinstance(record, db.InstrumentDefMsg) and self.definition_expiry:
            if str(record.raw_symbol)[6:12] != self.definition_expiry:
                self.last_record = time.monotonic()
                return
        raw = bytes(record)
        with self.lock:
            self.last_record = time.monotonic()
            if self.overflow:
                return
            if isinstance(record, (db.CBBOMsg, db.OHLCVMsg)):
                # Resolve mappings first; retain only the newest observation for
                # each instrument/schema while the consumer catches up.
                key = (int(record.rtype), record.instrument_id)
                self.latest[key] = raw
            else:
                self.control.append(raw)
            if len(self.control) + len(self.latest) > self.limit:
                self.overflow = True

    def take(self, count=256):
        with self.lock:
            result = []
            while self.control and len(result) < count:
                result.append(self.control.popleft())
            while self.latest and len(result) < count:
                result.append(self.latest.popitem(last=False)[1])
            return b''.join(result)


async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=256 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    sessions = {}
    tasks = set()
    output_lock = asyncio.Lock()

    async def emit(message):
        async with output_lock:
            # A stuck output write is confined to this process. The parent
            # heartbeat deadline kills/reaps it, including any stuck SDK thread.
            data = (json.dumps(message) + '\n').encode()
            await asyncio.to_thread(write_output, data)

    async def sdk_call(fn, timeout=30):
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout)
        except asyncio.TimeoutError:
            # Threads cannot safely be killed; terminate this isolated process.
            os._exit(75)

    async def finish(identifier, entry, error=None):
        if sessions.get(identifier) is not entry:
            return
        sessions.pop(identifier, None)
        if entry.get('client'):
            with contextlib.suppress(Exception):
                await sdk_call(entry['client'].terminate, timeout=3)
        if error:
            await emit(dict(type='error', id=identifier, error=error))

    async def open_session(message):
        identifier = message['id']
        config = dict(message['config'])
        expiry = config.pop('definition_expiry', None)
        entry = dict(pending=PendingRecords(limit=100000 if expiry else 20000,
                                            definition_expiry=expiry), client=None, lock=asyncio.Lock())
        sessions[identifier] = entry
        def connect():
            client = db.Live(**{**config, 'reconnect_policy': 'none'})
            entry['client'] = client
            client.add_callback(entry['pending'].add)
            for request in message['requests']:
                client.subscribe(**request)
            client.start()
            return client
        try:
            async with entry['lock']:
                client = await sdk_call(connect, timeout=45)
            # Only this child awaits SDK close/reconnect state; the HTTP process
            # receives an error and retries using its existing bounded backoff.
            await client.wait_for_close()
            await finish(identifier, entry, 'Live connection closed')
        except Exception as exc:
            await finish(identifier, entry, f'{type(exc).__name__}: {exc}')

    async def command(message):
        if message['op'] == 'open':
            await open_session(message)
            return
        entry = sessions.get(message['id'])
        if entry:
            async with entry['lock']:
                if message['op'] == 'close':
                    await finish(message['id'], entry)
                else:
                    try:
                        await sdk_call(lambda: entry['client'].subscribe(**message['request']))
                    except Exception as exc:
                        await finish(message['id'], entry, f'{type(exc).__name__}: {exc}')

    async def pump():
        last_heartbeat = 0
        while True:
            now = time.monotonic()
            for identifier, entry in list(sessions.items()):
                pending = entry['pending']
                if pending.overflow or now - pending.last_record > 90:
                    await finish(identifier, entry, 'Ingestion buffer overflow' if pending.overflow else 'No protocol records for 90 seconds')
                    continue
                data = pending.take()
                if data:
                    await emit(dict(type='records', id=identifier,
                                    data=base64.b64encode(data).decode()))
            if now - last_heartbeat >= 1:
                await emit(dict(type='heartbeat', connections=len(sessions)))
                last_heartbeat = now
            await asyncio.sleep(.01)

    pump_task = asyncio.create_task(pump())
    try:
        while line := await reader.readline():
            if len(tasks) >= 128:
                raise RuntimeError('Ingestion command limit exceeded')
            task = asyncio.create_task(command(json.loads(line)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        pump_task.cancel()
        # Parent owns lifecycle; never wait indefinitely for SDK shutdown.
        os._exit(0)


def write_output(data):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    asyncio.run(main())
