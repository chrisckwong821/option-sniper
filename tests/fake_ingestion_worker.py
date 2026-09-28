"""Local fault-injection child; never opens provider sockets."""
import asyncio
import json
import sys
import time

async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    while True:
        print(json.dumps({'type': 'heartbeat', 'connections': 0}), flush=True)
        try:
            line = await asyncio.wait_for(reader.readline(), .05)
        except asyncio.TimeoutError:
            continue
        if not line:
            return
        message = json.loads(line)
        if message['op'] == 'open':
            if message['config']['key'] == 'stall':
                time.sleep(60)
            else:
                print(json.dumps({'type': 'records', 'id': message['id'],
                                  'data': message['config']['fixture_data']}), flush=True)

if __name__ == '__main__':
    asyncio.run(main())
