import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pandas as pd
from botocore.exceptions import ClientError
from scripts.shared_cache import SharedCache, SharedCacheError, atomic_json, digest
from scripts.historical_cache import HistoricalCache, normalized


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s3 = Mock()
        self.shared = SharedCache(self.root, self.s3, bucket='test')
        self.request = dict(dataset='EQUS.MINI', schema='ohlcv-1m', stype_in='raw_symbol',
                            symbols=['A'], start='2026-09-01T13:30:00Z', end='2026-09-01T14:30:00Z')

    def entry(self, request):
        p = self.root / 'test.dbn.zst'; p.write_bytes(b'data')
        row = dict(path=p.name, request=normalized(request), sha256=digest(p))
        atomic_json(self.root / 'history-manifest.json', dict(files=[row]))
        return row

    def test_login_profile_receives_region_before_credentials_resolve(self):
        import os
        with patch.dict(os.environ, {'SIGMA_S3_REGION':'ap-southeast-1','SIGMA_AWS_PROFILE':'test-login'}), patch('boto3.Session') as session:
            shared=SharedCache(self.root,bucket='test')
            shared.client
            session.assert_called_once_with(profile_name='test-login',region_name='ap-southeast-1')

    def test_new_remote_request_sidecar_supplies_missing_coverage(self):
        shared=Mock(enabled=True)
        row={'path':'requests/new.dbn.zst','request':normalized(self.request)}
        shared.list_keys.return_value=['history/requests/new.dbn.json']
        def get(key,path,*args):
            if key=='history/requests/new.dbn.json':
                Path(path).parent.mkdir(parents=True,exist_ok=True)
                Path(path).write_text(json.dumps(row));return True
            if key=='history/requests/new.dbn.zst':
                Path(path).parent.mkdir(parents=True,exist_ok=True)
                Path(path).write_bytes(b'data');return True
            return False
        shared.get.side_effect=get
        client=Mock()
        cache=HistoricalCache(root=self.root,shared=shared,client=client)
        self.assertEqual(cache.get_cost(**self.request),0)
        client.metadata.get_cost.assert_not_called()
        self.assertTrue((self.root/'requests/new.dbn.zst').exists())

    def test_symbol_mapping_uses_local_cache_without_api_key(self):
        shared=Mock(enabled=False);shared.get.return_value=False
        provider=Mock();provider.symbology.resolve.return_value={'result':{'A':[]}}
        request={'dataset':'EQUS.SUMMARY','symbols':['A'],'start_date':'2026-09-01','end_date':'2026-09-02'}
        first=HistoricalCache(root=self.root,shared=shared,client=provider)
        self.assertEqual(first.symbology.resolve(**request),{'result':{'A':[]}})
        second=HistoricalCache(root=self.root,shared=shared)
        self.assertEqual(second.symbology.resolve(**request),{'result':{'A':[]}})
        provider.symbology.resolve.assert_called_once()

    def test_only_not_found_is_a_miss(self):
        for code in ['403', 'AccessDenied', 'SlowDown']:
            self.s3.get_object.side_effect = ClientError({'Error': {'Code': code}}, 'GetObject')
            with self.assertRaises(SharedCacheError): self.shared.get('x', self.root / 'x')
        self.s3.get_object.side_effect = ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        self.assertFalse(self.shared.get('x', self.root / 'x'))

    def test_checksum_failure_preserves_existing_file(self):
        p = self.root / 'x'; p.write_bytes(b'old')
        self.s3.get_object.return_value = {'Body': io.BytesIO(b'wrong')}
        with self.assertRaises(SharedCacheError): self.shared.get('x', p, 'bad')
        self.assertEqual(p.read_bytes(), b'old')

    def test_local_coverage_avoids_s3_and_databento(self):
        self.entry(self.request)
        cache = HistoricalCache(root=self.root, shared=self.shared)
        self.assertEqual(cache.get_cost(**self.request), 0)
        self.s3.get_object.assert_not_called()

    def test_only_missing_time_interval_is_requested(self):
        self.entry({**self.request, 'end': '2026-09-01T14:00:00Z'})
        cache = HistoricalCache(root=self.root, shared=self.shared)
        parts, gaps = cache._parts(self.request)
        self.assertEqual(len(parts), 1)
        self.assertEqual(gaps, [(pd.Timestamp('2026-09-01T14:00:00Z').value,
                                 pd.Timestamp(self.request['end']).value, ['A'])])

    def test_only_missing_symbols_requested(self):
        self.entry(self.request)
        cache = HistoricalCache(root=self.root, shared=self.shared)
        _, gaps = cache._parts({**self.request, 'symbols': ['A','B']})
        self.assertEqual(gaps[0][2], ['B'])

    def test_schema_and_dataset_cannot_be_substituted(self):
        self.entry(self.request)
        cache = HistoricalCache(root=self.root, shared=self.shared)
        for change in [{'schema':'ohlcv-1d'}, {'dataset':'EQUS.SUMMARY'}]:
            parts, gaps = cache._parts({**self.request, **change})
            self.assertFalse(parts); self.assertTrue(gaps)

    def test_s3_denial_does_not_call_databento(self):
        client = Mock()
        self.s3.get_object.side_effect = ClientError({'Error': {'Code':'AccessDenied'}}, 'GetObject')
        cache = HistoricalCache(root=self.root, shared=self.shared, client=client)
        with self.assertRaises(SharedCacheError): cache.get_range(**self.request)
        client.timeseries.get_range.assert_not_called()
        client.metadata.get_cost.assert_not_called()

    def test_more_than_week_gap_blocked(self):
        cache = HistoricalCache(root=self.root, shared=self.shared)
        with self.assertRaisesRegex(RuntimeError, 'one week'):
            cache._requests(self.request, [(0, pd.Timedelta(days=8).value, ['A'])])

    def test_purchase_over_five_dollars_blocked(self):
        client = Mock(); client.metadata.get_cost.return_value = 5.01
        shared = Mock(enabled=False); shared.get.return_value = False
        cache = HistoricalCache(root=self.root, shared=shared, client=client)
        with patch('scripts.historical_cache._SPENT', 0):
            with self.assertRaisesRegex(RuntimeError, 'budget'): cache.get_range(**self.request)
        client.timeseries.get_range.assert_not_called()

    def test_raw_symbol_subset_is_filtered(self):
        self.entry({**self.request, 'symbols':['A','B']})
        frame = pd.DataFrame({'symbol':['A','B'], 'close':[10,20]}, index=pd.DatetimeIndex(['2026-09-01T13:31:00Z']*2, name='ts_event'))
        cache = HistoricalCache(root=self.root, shared=self.shared)
        with patch('scripts.historical_cache.db.DBNStore.from_file', return_value=SimpleNamespace(to_df=lambda: frame)):
            result=cache.get_range(**self.request).to_df()
        self.assertEqual(result['symbol'].tolist(), ['A'])

if __name__ == '__main__': unittest.main()

class BudgetTests(unittest.TestCase):
    def test_budget_is_shared_across_processes(self):
        import os, subprocess, sys
        from scripts.reconstruction_budget import reserve
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'; path.write_text('{}')
            request = dict(start='2026-09-01T13:30:00Z', end='2026-09-01T14:30:00Z')
            with patch.dict(os.environ, {'SIGMA_RECONSTRUCTION_BUDGET_FILE':str(path), 'SIGMA_RECONSTRUCTION_SESSION':'2026-09-01'}):
                reserve(3, request)
                process = subprocess.run([sys.executable, '-c',
                    'from scripts.reconstruction_budget import reserve; reserve(3, '+repr(request)+')'], capture_output=True, text=True)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('budget', process.stderr)
                self.assertEqual(json.loads(path.read_text())['cost'], 3)

    def test_old_missing_data_requires_approval(self):
        import os
        from scripts.reconstruction_budget import reserve
        with patch.dict(os.environ, {'SIGMA_RECONSTRUCTION_SESSION':'2026-09-04'}):
            with self.assertRaisesRegex(RuntimeError, 'older than one week'):
                reserve(0, dict(start='2026-03-01', end='2026-03-02'))

class RollingWindowTests(unittest.TestCase):
    def test_statistics_exclude_future_and_expired_rows(self):
        import sqlite3
        from datetime import date
        from scripts.iv_expansion_history import build_statistics
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE observations (ticker TEXT,bucket TEXT, session_date TEXT, iv_expansion_pct REAL, iv_log_change_pct REAL, reference_status TEXT, reference_reliability TEXT, observed_reliability TEXT, observation_dte_days INTEGER, observation_tenor_class TEXT, expiry_status TEXT)')
        for stamp in ['2026-03-03','2026-03-04','2026-09-03','2026-09-04','2026-09-05']:
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',('A','09:30',stamp,10,9,'yesterday','high','high',3,'front_weekly','target_weekly'))
        rows=build_statistics(db,['A'],holdout_session=date(2026,9,4),min_observations=1,include_low_reliability=False)
        row=next(r for r in rows if r['bucket']=='09:30')
        self.assertEqual(row['log_iv_change_distribution']['n'],2)
