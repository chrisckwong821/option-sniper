import asyncio
import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from scripts.calibration_refresh import CalibrationRefresh, NAME, METHOD
from scripts.iv_bucket_score import BucketScores


def calibration(day='2026-09-03', scale=2.5):
    return {'months':6,'last_calibration_session':day,'generated_at_utc':day+'T21:00:00+00:00',
            'statistics':[{'ticker':'AAA','bucket':'15:30','tenor_class':'front_weekly','method':METHOD,
                           'calibration_excludes_session':day,
                           'log_iv_change_distribution':{'n':100,'status':'valid','median':0,'robust_sigma':scale}}]}


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.write(calibration())
        self.point={'ticker':'AAA','last_valid_at':'2026-09-08T19:59:00+00:00','iv_expansion_pct':5,'iv_reference_status':'yesterday'}
        self.tracker=SimpleNamespace(bucket_scores=BucketScores(self.root/NAME),results={'AAA':self.point})
        self.store=SimpleNamespace(iv_tracker=self.tracker,dirty_symbols=set(),invalidate_snapshot=Mock())
        self.shared=Mock(enabled=False)
        self.refresh=CalibrationRefresh(self.store,self.root,'2026-09-04','policy',shared=self.shared)

    def write(self,payload):
        (self.root/NAME).write_text(json.dumps(payload))

    async def test_next_day_swaps_scores_without_changing_observation(self):
        original=copy.deepcopy(self.point)
        ref={'option_expiry':'2026-09-11'}
        before=self.tracker.bucket_scores.score(self.point,ref)['z']
        self.write(calibration('2026-09-04',1.25))
        self.assertTrue(await self.refresh.check())
        self.assertAlmostEqual(self.tracker.bucket_scores.score(self.point,ref)['z'],before*2)
        self.assertEqual(self.point,original)
        self.assertEqual(self.store.dirty_symbols,{'AAA'})
        self.store.invalidate_snapshot.assert_called_once()
        self.assertFalse(await self.refresh.check())

    async def test_future_corrupt_and_older_keep_active_scorer(self):
        original=self.tracker.bucket_scores
        for payload in [calibration('2026-09-08'),calibration('2026-09-02'),calibration('2026-09-04',0)]:
            self.write(payload)
            self.assertFalse(await self.refresh.check())
            self.assertIs(self.tracker.bucket_scores,original)
        (self.root/NAME).write_text('{broken')
        self.assertFalse(await self.refresh.check())
        self.assertIs(self.tracker.bucket_scores,original)

    async def test_s3_manifest_download_and_unchanged_pointer(self):
        raw=json.dumps(calibration('2026-09-04',1.25)).encode();compressed=gzip.compress(raw)
        objects={'computed/latest.json':json.dumps({'release':'computed/new'}).encode(),
                 'computed/new/manifest.json':json.dumps({'schema_version':1,'screening_policy_fingerprint':'policy','completed_session':'2026-09-04','files':[{'name':NAME,'object':'stats.gz','bytes':len(compressed),'uncompressed_bytes':len(raw),'sha256':hashlib.sha256(compressed).hexdigest()}]}).encode(),
                 'computed/new/stats.gz':compressed}
        def get(key,path,checksum=None):
            data=objects[key]
            if checksum and hashlib.sha256(data).hexdigest()!=checksum:raise ValueError('checksum mismatch')
            Path(path).write_bytes(data);return True
        self.shared.enabled=True;self.shared.get.side_effect=get
        self.assertTrue(await self.refresh.check())
        self.shared.get.reset_mock()
        self.assertFalse(await self.refresh.check())
        self.assertEqual([c.args[0] for c in self.shared.get.call_args_list],['computed/latest.json'])
        self.shared.put.assert_not_called()
        self.shared.get.side_effect=RuntimeError('S3 unavailable')
        active=self.tracker.bucket_scores
        self.assertFalse(await self.refresh.check());self.assertIs(active,self.tracker.bucket_scores)
        self.assertIn('S3 unavailable',self.refresh.status['error'])

    async def test_s3_checksum_failure_retains_active(self):
        self.shared.enabled=True
        self.shared.get.side_effect=ValueError('checksum mismatch')
        active=self.tracker.bucket_scores
        self.assertFalse(await self.refresh.check())
        self.assertIs(active,self.tracker.bucket_scores)
        self.assertIn('checksum mismatch',self.refresh.status['error'])

    async def test_slow_fetch_does_not_block_event_loop(self):
        import threading
        entered=threading.Event();release=threading.Event()
        def slow():
            entered.set();release.wait(2);return [],[]
        self.refresh.candidate=slow
        task=asyncio.create_task(self.refresh.check())
        while not entered.is_set():await asyncio.sleep(.001)
        self.assertFalse(task.done())
        release.set();await task
