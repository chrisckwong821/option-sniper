"""Exercise full snapshot publication and the paid-download boundary at startup."""
from contextlib import closing
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import computed_cache, startup_history
from scripts.prepare_shared_cache import prepare
from scripts.dashboard_state import build_mock_bundle, screening_policy_fingerprint
from scripts.archive_history import inventory
from tests.test_dashboard_state import sample_plan

METHOD = 'moneyness-matched-black76-robust-mad-min2dte-v2'


class StartupRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.state = build_mock_bundle(sample_plan(), 30)
        self.target = self.state['today']['anchor_date']
        self.stats = dict(last_calibration_session=self.target, months=6,
                          statistics=[dict(method=METHOD)])
        self.write('dashboard-state.json', self.state)
        self.write('iv-expansion-statistics-session-batch.json', self.stats)
        self.write('live-iv-expansion.json', {'state_key': f"{self.state['generated_at_utc']}|{self.target}|test", 'marks': []})
        with closing(sqlite3.connect(self.source / 'iv-expansion-history-session-batch.sqlite3')) as db:
            db.execute('create table observations (value real)')
            db.execute('insert into observations values (3.14)')
            db.commit()

    def write(self, name, value):
        (self.source / name).write_text(json.dumps(value))

    def test_published_snapshot_roundtrips_all_four_files(self):
        objects = {}
        shared = Mock(enabled=True)
        def put(key, path):
            objects[key] = Path(path).read_bytes()
        def get(key, path, checksum=None):
            if key not in objects:
                return False
            Path(path).write_bytes(objects[key])
            return True
        shared.put.side_effect = put
        shared.get.side_effect = get
        with patch.object(computed_cache, 'SharedCache', return_value=shared):
            computed_cache.publish(self.source)
            self.assertEqual(shared.put.call_args_list[-1].args[0], 'computed/latest.json')
            dest = self.root / 'restored'
            self.assertTrue(computed_cache.restore(dest, self.target, screening_policy_fingerprint()))
            self.assertEqual(json.loads((dest / 'dashboard-state.json').read_text()), self.state)
            with closing(sqlite3.connect(dest / 'iv-expansion-history-session-batch.sqlite3')) as db:
                self.assertEqual(db.execute('select value from observations').fetchone()[0], 3.14)
                db.execute('insert into observations values (9)')
                db.commit()
            self.assertTrue(computed_cache.restore(dest, self.target, screening_policy_fingerprint(), include_state=False))
            with closing(sqlite3.connect(dest / 'iv-expansion-history-session-batch.sqlite3')) as db:
                self.assertEqual(db.execute('select count(*) from observations').fetchone()[0], 2)
            older = self.root / 'older'
            self.assertTrue(computed_cache.restore(older, '2099-01-01', 'different', include_state=False, history_only=True))
            self.assertFalse((older / 'dashboard-state.json').exists())
            self.assertTrue((older / 'iv-expansion-history-session-batch.sqlite3').exists())

    def test_snapshot_excludes_incompatible_ancillary_data(self):
        self.write('live-iv-expansion.json', {'state_key': 'old'})
        self.write('iv-expansion-statistics-session-batch.json', {'last_calibration_session': 'old'})
        manifest = prepare(self.source, self.root / 'bundle')
        self.assertEqual({e['name'] for e in manifest['files']}, {'dashboard-state.json', 'iv-expansion-history-session-batch.sqlite3'})
        for entry in manifest['files']:
            self.assertEqual(len(gzip.decompress((self.root / 'bundle' / entry['object']).read_bytes())), entry['uncompressed_bytes'])

    def test_startup_keeps_valid_local_statistics(self):
        with patch.object(startup_history, 'SharedCache', return_value=Mock(enabled=True)), patch('scripts.computed_cache.restore') as restore:
            startup_history.ensure_bucket_statistics(self.source, self.state)
        restore.assert_not_called()

    def test_startup_s3_statistics_avoid_reconstruction(self):
        (self.source / 'iv-expansion-statistics-session-batch.json').unlink()
        with patch.object(startup_history, 'SharedCache', return_value=Mock(enabled=True)), patch('scripts.computed_cache.restore', side_effect=lambda *a, **kw: self.write('iv-expansion-statistics-session-batch.json', self.stats)), patch.object(startup_history.subprocess, 'run') as run:
            startup_history.ensure_bucket_statistics(self.source, self.state)
        run.assert_not_called()

    def test_startup_failed_reconstruction_never_publishes(self):
        (self.source / 'iv-expansion-statistics-session-batch.json').unlink()
        (self.source / 'iv-expansion-history-session-batch.sqlite3').unlink()
        with patch.object(startup_history, 'SharedCache', return_value=Mock(enabled=True)), patch('scripts.computed_cache.restore', return_value=False) as restore, patch.object(startup_history.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run, patch('scripts.computed_cache.publish') as publish:
            with self.assertRaisesRegex(RuntimeError, 'reconstruction failed'):
                startup_history.ensure_bucket_statistics(self.source, self.state, allow_download=False)
            self.assertEqual(restore.call_count, 2)
            self.assertEqual(run.call_args.kwargs['env']['SIGMA_RECONSTRUCTION_MAX_COST'], '0')
            publish.assert_not_called()

    def test_startup_success_publishes_reconstruction(self):
        self.write('iv-expansion-statistics-session-batch.json', {'last_calibration_session': 'old'})
        with patch.object(startup_history, 'SharedCache', return_value=Mock(enabled=True)), patch('scripts.computed_cache.restore', return_value=False), patch.object(startup_history.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), patch('scripts.computed_cache.publish') as publish:
            startup_history.ensure_bucket_statistics(self.source, self.state)
            publish.assert_called_once_with(self.source)

    def test_background_reconstruction_preserves_configured_budget(self):
        self.write('iv-expansion-statistics-session-batch.json', {'last_calibration_session': 'old'})
        with patch.dict(startup_history.os.environ, {'SIGMA_RECONSTRUCTION_MAX_COST': '5'}), patch.object(startup_history, 'SharedCache', return_value=Mock(enabled=True)), patch('scripts.computed_cache.restore', return_value=False), patch.object(startup_history.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                startup_history.ensure_bucket_statistics(self.source, self.state, allow_download=True)
            self.assertEqual(run.call_args.kwargs['env']['SIGMA_RECONSTRUCTION_MAX_COST'], '5')
            self.assertEqual(run.call_args.kwargs['timeout'], 900)

    def test_archive_excludes_secrets_and_marks_damaged_history(self):
        raw = self.source / 'iv-expansion-history/raw/spot/2026-08-27'
        raw.mkdir(parents=True)
        (raw / 'damaged.dbn.zst').write_bytes(b'not a DBN')
        (raw / 'empty.dbn.zst').touch()
        (raw / 'link.dbn.zst').symlink_to(raw / 'damaged.dbn.zst')
        self.write('.env', {'secret': 'do not archive'})
        universe = self.source / 'refresh/2026-08-27/nasdaq-universe.json'
        universe.parent.mkdir(parents=True)
        universe.write_text('{"tickers": ["A"]}')
        files = inventory(self.source)['files']
        self.assertEqual(len(files), 2)
        self.assertTrue(next(f for f in files if 'damaged' in f['path'])['invalid'])
        self.assertFalse(any('.env' in f['path'] for f in files))
