"""Independent HTTP watchdog; failed liveness exits so Docker can recover."""
import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.request


def responsive(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def terminate_group(child, grace=5):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Also reap SDK descendants, even if the app itself exited first.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def monitor(child, url, *, startup_grace=120, interval=10, failures_required=3,
            probe=responsive, clock=time.monotonic, sleep=time.sleep):
    started = clock()
    failures = 0
    while child.poll() is None:
        if clock() - started >= startup_grace:
            failures = 0 if probe(url) else failures + 1
            if failures >= failures_required:
                print('HTTP liveness failed repeatedly; exiting for container recovery', file=sys.stderr, flush=True)
                return 1
        sleep(interval)
    return child.returncode


def main():
    args = sys.argv[1:]
    port = '8765'
    for i, arg in enumerate(args):
        if arg == '--port' and i + 1 < len(args):
            port = args[i + 1]
        elif arg.startswith('--port='):
            port = arg.split('=', 1)[1]
    child = subprocess.Popen([sys.executable, '-m', 'scripts.live_price_adapter', *args],
                             start_new_session=True)
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return monitor(child, f'http://127.0.0.1:{int(port)}/api/liveness')
    finally:
        terminate_group(child)


if __name__ == '__main__':
    raise SystemExit(main())
