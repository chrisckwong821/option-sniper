"""Conservative purchase reservations shared across startup subprocesses."""
import fcntl
import json
import math
import os
import threading
import uuid
from pathlib import Path
import pandas as pd

_LOCK = threading.Lock()
_MEMORY = {'cost': 0.0}


def start_run(root, completed_session):
    path = Path(root).resolve() / 'reconstruction-runs' / (uuid.uuid4().hex + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{}')
    os.environ['SIGMA_RECONSTRUCTION_BUDGET_FILE'] = str(path)
    os.environ['SIGMA_RECONSTRUCTION_SESSION'] = str(completed_session)


def reserve(cost, request):
    cost = float(cost)
    if not math.isfinite(cost) or cost < 0:
        raise RuntimeError('Invalid Databento cost estimate')
    a, b = pd.Timestamp(request['start']).value, pd.Timestamp(request['end']).value
    anchor = os.environ.get('SIGMA_RECONSTRUCTION_SESSION')
    if anchor and a < (pd.Timestamp(anchor, tz='UTC') - pd.Timedelta(days=7)).value:
        raise RuntimeError('Missing historical data is older than one week; approval required')
    cap = min(5.0, float(os.environ.get('SIGMA_RECONSTRUCTION_MAX_COST') or '5'))
    def update(state):
        lo, hi = min(a, state.get('start', a)), max(b, state.get('end', b))
        if hi - lo > pd.Timedelta(days=7).value:
            raise RuntimeError('Combined missing history exceeds one week; approval required')
        total = state.get('cost', 0.0) + cost
        if total > cap + 1e-9:
            raise RuntimeError(f'Databento purchase exceeds ${cap:.2f} reconstruction budget')
        return {'cost': total, 'start': lo, 'end': hi}
    with _LOCK:
        path = os.environ.get('SIGMA_RECONSTRUCTION_BUDGET_FILE')
        if path:
            with open(path, 'r+') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                state = update(json.load(stream))
                stream.seek(0); json.dump(state, stream); stream.truncate(); stream.flush()
                os.fsync(stream.fileno())
        else:
            state = update(_MEMORY)
            _MEMORY.update(state)
