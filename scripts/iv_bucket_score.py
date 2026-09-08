"""Score IV observations in their quote-time bucket, including partial buckets."""
import json
import math
from datetime import datetime, date
from zoneinfo import ZoneInfo

try:
    from .iv_expansion_history import option_expiry_for_observation, XNYS
except ImportError:
    from iv_expansion_history import option_expiry_for_observation, XNYS


class BucketScores:
    def __init__(self, path):
        self.version = None
        self.rows = {}
        try:
            payload = json.loads(path.read_text())
            self.last_session = payload.get('last_calibration_session', '')
            for row in payload.get('statistics', []):
                if row.get('method') == 'moneyness-matched-black76-robust-mad-min2dte-v2':
                    self.rows[(row['ticker'], row['bucket'], row['tenor_class'])] = row
        except (OSError, ValueError, KeyError, TypeError):
            self.last_session = ''

    @classmethod
    def from_payload(cls, payload, version):
        scorer = cls.__new__(cls)
        scorer.version = version
        scorer.last_session = payload['last_calibration_session']
        scorer.rows = {(r['ticker'], r['bucket'], r['tenor_class']): r for r in payload['statistics']}
        return scorer

    def score(self, point, reference):
        output = {'z': None, 'n': 0, 'bucket': None, 'reason': 'No compatible calibration.', 'calibration_date': self.last_session, 'calibration_version': self.version}
        try:
            stamp = datetime.fromisoformat(point['last_valid_at'])
            if stamp.tzinfo is None:
                return {**output, 'reason': 'Quote timestamp requires a timezone.'}
            stamp = stamp.astimezone(ZoneInfo('America/New_York'))
            session = stamp.date()
            minute = stamp.hour * 60 + stamp.minute
            if not XNYS.is_session(session.isoformat()) or not 570 <= minute < 960:
                return {**output, 'reason': 'Outside regular options hours.'}
            if stamp >= XNYS.session_close(session.isoformat()).to_pydatetime():
                return {**output, 'reason': 'Outside regular options hours.'}
            output['bucket'] = f'{stamp.hour:02}:{(stamp.minute // 30) * 30:02}'
            output['observation_date'] = session.isoformat()
            output['indicative'] = stamp.minute % 30 != 29
            if point.get('iv_reference_status', reference.get('reference_status', 'yesterday')) != 'yesterday':
                return {**output, 'reason': 'Fallback reference: comparable baseline required.'}
            expiry = date.fromisoformat(reference['option_expiry'])
            dte = (expiry - session).days
            target = option_expiry_for_observation(session)
            tenor = 'below_minimum_dte' if dte < 2 else 'front_weekly' if dte <= 4 else 'rolled_next_weekly' if expiry == target and dte <= 11 else 'longer_dated_fallback'
            row = self.rows.get((point['ticker'], output['bucket'], tenor))
            if not row:
                return output
            if self.last_session >= session.isoformat() and row.get('calibration_excludes_session') != session.isoformat():
                return {**output, 'reason': 'Calibration overlaps this observation session.'}
            dist = row['log_iv_change_distribution']
            output['n'] = dist.get('n', 0)
            if dist.get('status') != 'valid':
                return {**output, 'reason': 'Insufficient or degenerate history.'}
            median, scale = float(dist['median']), float(dist['robust_sigma'])
            change = float(point['iv_expansion_pct'])
            if change <= -100 or not all(map(math.isfinite, (change, median, scale))) or scale <= 0:
                return output
            return {**output, 'z': (100 * math.log1p(change / 100) - median) / scale,
                    'sigma': scale, 'median': median, 'tenor_class': tenor,
                    'reason': f'{tenor.replace("_", " ")} · Same-observation robust log-IV score.',
                    'provisional': point.get('iv_reliability') == 'low' or point.get('reference_reliability', reference.get('iv_reliability')) == 'low'}
        except (KeyError, ValueError, TypeError, OverflowError):
            return output
