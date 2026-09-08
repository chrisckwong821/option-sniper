import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.preflight_cache_migration import clone_cache


class MigrationCloneTests(unittest.TestCase):
    def test_preserves_committed_wal_and_does_not_copy_raw_history(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.mkdir()
            (source / 'dashboard-state.json').write_text('{}')
            (source / 'raw-options.dbn').write_bytes(b'raw')
            with sqlite3.connect(source / 'discord-alerts.sqlite3') as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE alerts (ticker TEXT)')
                db.execute("INSERT INTO alerts VALUES ('TSEM')")
                db.commit()
                clone_cache(source, target)
            with sqlite3.connect(target / 'discord-alerts.sqlite3') as db:
                self.assertEqual(db.execute('SELECT ticker FROM alerts').fetchall(), [('TSEM',)])
            self.assertFalse((target / 'raw-options.dbn').exists())
            self.assertEqual((source / 'dashboard-state.json').read_text(), '{}')

    def test_rejects_overlapping_or_nonempty_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            for destination in (source, source / 'child', Path(directory)):
                with self.assertRaises(ValueError):
                    clone_cache(source, destination)
            target = Path(directory) / 'target'
            target.mkdir()
            (target / 'existing').touch()
            with self.assertRaises(ValueError):
                clone_cache(source, target)
