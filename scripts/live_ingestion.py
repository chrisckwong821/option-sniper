"""Isolate all live SDK sockets in one killable process with bounded IPC."""
import asyncio
import base64
import contextlib
import json
import sys
import weakref

from databento_dbn import DBNDecoder

_BRIDGES = weakref.WeakKeyDictionary()


class IsolatedLive:
    """Small local proxy; no SDK session or SDK locks exist in the HTTP process."""
    def __init__(self, **config):
        self.config = config
        self.requests = []
        self.bridge = None
        self.identifier = None
        self.queue = asyncio.Queue(maxsize=32)
        self.records = []
        self.decoder = DBNDecoder(has_metadata=False)
        self.error = None
        self.closed = False

    def subscribe(self, **request):
        if self.closed:
            raise RuntimeError('Live proxy is closed')
        self.requests.append(request)
        if self.bridge:
            self.bridge.send(dict(op='subscribe', id=self.identifier, request=request))

    def __aiter__(self):
        loop = asyncio.get_running_loop()
        bridge = _BRIDGES.setdefault(loop, IngestionBridge())
        self.bridge = bridge
        bridge.register(self)
        return self

    async def __anext__(self):
        while not self.records:
            if self.error:
                raise RuntimeError(self.error)
            if self.closed:
                raise StopAsyncIteration
            data = await self.queue.get()
            if data is None:
                continue
            self.records = list(reversed(self.decoder.write_and_decode(data)))
        return self.records.pop()

    def fail(self, reason):
        self.error = reason
        self.records.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)

    def stop(self):
        if self.closed:
            return
        self.closed = True
        if self.bridge:
            self.bridge.clients.pop(self.identifier, None)
            with contextlib.suppress(RuntimeError):
                self.bridge.send(dict(op='close', id=self.identifier))
        self.fail('Live proxy closed')

    terminate = stop


class IngestionBridge:
    def __init__(self, heartbeat_timeout=15, worker_module='scripts.live_ingestion_worker'):
        self.clients = {}
        self.commands = asyncio.Queue(maxsize=128)
        self.task = None
        self.process = None
        self.next_id = 0
        self.restarts = 0
        self.last_error = None
        self.heartbeat_timeout = heartbeat_timeout
        self.worker_module = worker_module
        self.worker_health = {}

    def send(self, command):
        try:
            self.commands.put_nowait(command)
        except asyncio.QueueFull:
            raise RuntimeError('Ingestion command queue full') from None

    def register(self, client):
        self.next_id += 1
        client.identifier = str(self.next_id)
        self.clients[client.identifier] = client
        try:
            self.send(dict(op='open', id=client.identifier, config=client.config,
                           requests=client.requests))
        except Exception:
            self.clients.pop(client.identifier, None)
            raise
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name='isolated-ingestion')

    async def write_commands(self, process):
        while True:
            command = await self.commands.get()
            process.stdin.write((json.dumps(command, default=str) + '\n').encode())
            await asyncio.wait_for(process.stdin.drain(), 5)

    async def read_records(self, process):
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), self.heartbeat_timeout)
            if not line:
                raise RuntimeError('Ingestion worker exited')
            message = json.loads(line)
            if message['type'] == 'heartbeat':
                self.worker_health = message
                continue
            client = self.clients.get(message.get('id'))
            if client is None:
                continue
            if message['type'] == 'error':
                client.fail(message['error'])
            else:
                try:
                    client.queue.put_nowait(base64.b64decode(message['data'], validate=True))
                except asyncio.QueueFull:
                    # Never silently lose mappings/definitions: reconnect this
                    # feed and rebuild its mapping instead of retaining bad data.
                    client.fail('Ingestion consumer queue full; reconnecting')

    async def run(self):
        backoff = 1
        while True:
            tasks = []
            try:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', self.worker_module,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    limit=256 * 1024,
                )
                tasks = [asyncio.create_task(self.write_commands(self.process)),
                         asyncio.create_task(self.read_records(self.process))]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f'{type(exc).__name__}: {exc}'
                self.restarts += 1
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.process and self.process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
                for client in self.clients.values():
                    client.fail(self.last_error or 'Ingestion worker stopped')
                self.clients.clear()
                while not self.commands.empty():
                    self.commands.get_nowait()
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def health(self):
        return dict(pid=self.process.pid if self.process else None,
                    connections=len(self.clients), restarts=self.restarts,
                    last_error=self.last_error, worker=self.worker_health)


def ingestion_health():
    bridge = _BRIDGES.get(asyncio.get_running_loop())
    return bridge.health() if bridge else {'connections': 0}


async def close_ingestion():
    bridge = _BRIDGES.pop(asyncio.get_running_loop(), None)
    if bridge:
        await bridge.close()
