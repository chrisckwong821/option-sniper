import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from scripts.computed_cache import restore
from scripts.dashboard_state import DashboardStateManager, build_mock_bundle, screening_policy_fingerprint
from scripts.shared_cache import SharedCacheError
from tests.test_dashboard_state import sample_plan


class ComputedCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.state=build_mock_bundle(sample_plan(),30)
        encoded=gzip.compress(json.dumps(self.state).encode())
        raw=json.dumps(self.state).encode()
        self.manifest={'schema_version':1,'completed_session':'2026-08-27',
                       'screening_policy_fingerprint':screening_policy_fingerprint(),
                       'files':[{'name':'dashboard-state.json','object':'dashboard-state.json.gz',
                                 'bytes':len(encoded),'uncompressed_bytes':len(raw),'sha256':hashlib.sha256(encoded).hexdigest()}]}
        self.objects={'computed/latest.json':json.dumps({'release':'computed/test'}).encode(),
                      'computed/test/dashboard-state.json.gz':encoded}
        self.shared=Mock(enabled=True)
        self.shared.get.side_effect=self.get

    def get(self,key,path,checksum=None):
        if key=='computed/test/manifest.json':
            data=json.dumps(self.manifest).encode()
        elif key in self.objects:
            data=self.objects[key]
        else:return False
        if checksum and hashlib.sha256(data).hexdigest()!=checksum:raise SharedCacheError('Checksum mismatch')
        Path(path).write_bytes(data);return True

    def test_valid_computed_cache_precedes_reconstruction(self):
        rebuild=Mock(side_effect=AssertionError('Unexpected reconstruction'))
        with patch('scripts.computed_cache.SharedCache',return_value=self.shared):
            state=DashboardStateManager(self.root/'dashboard-state.json',refresh=rebuild).ensure_state(sample_plan())
        self.assertEqual(state['cache']['source'],'s3');rebuild.assert_not_called()

    def test_incompatible_computed_cache_is_not_installed(self):
        self.manifest['completed_session']='2026-08-26'
        with patch('scripts.computed_cache.SharedCache',return_value=self.shared):
            self.assertFalse(restore(self.root,'2026-08-27',screening_policy_fingerprint()))
        self.assertFalse((self.root/'dashboard-state.json').exists())

    def test_incomplete_bundle_does_not_replace_local_state(self):
        (self.root/'dashboard-state.json').write_text('old')
        del self.objects['computed/test/dashboard-state.json.gz']
        with patch('scripts.computed_cache.SharedCache',return_value=self.shared):
            with self.assertRaises(SharedCacheError):restore(self.root,'2026-08-27',screening_policy_fingerprint())
        self.assertEqual((self.root/'dashboard-state.json').read_text(),'old')

    def test_corrupt_bundle_does_not_fall_through_to_paid_rebuild(self):
        self.objects['computed/test/dashboard-state.json.gz']=b'corrupt'
        rebuild=Mock()
        with patch('scripts.computed_cache.SharedCache',return_value=self.shared):
            with self.assertRaises(SharedCacheError):DashboardStateManager(self.root/'dashboard-state.json',refresh=rebuild).ensure_state(sample_plan())
        rebuild.assert_not_called()

    def test_true_computed_miss_reconstructs(self):
        del self.objects['computed/latest.json']
        rebuild=Mock(return_value=self.state)
        with patch('scripts.computed_cache.SharedCache',return_value=self.shared), patch('scripts.computed_cache.publish'):
            state=DashboardStateManager(self.root/'dashboard-state.json',refresh=rebuild).ensure_state(sample_plan())
        self.assertEqual(state['cache']['source'],'refresh');rebuild.assert_called_once()
