import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scripts.deployment_cache import backup, prune, restore
from scripts.deployment_health import check


NOW = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)


def healthy(now=NOW):
    return {'server_time': now.isoformat(), 'market_session': 'regular',
            'dashboard_state': {'status': 'fresh', 'data_date': '2026-09-04'},
            'calibration': {'active_version': 'validated', 'active_date': '2026-09-04'},
            'iv_expansion': {'reference_symbols': 416, 'calculated_symbols': 400, 'reference_date': '2026-09-04'},
            'feeds': {name: {'state': 'live', 'last_record_at': now.isoformat()}
                      for name in ['EQUS.MINI', 'OPRA.PILLAR']},
            'iv_expansions': [{'last_valid_at': now.isoformat()}]}


class HealthTests(unittest.TestCase):
    def test_premarket_allows_previous_calibration_without_live_option_observations(self):
        now = datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)
        payload = healthy(now)
        payload['dashboard_state']['data_date'] = '2026-09-08'
        payload['iv_expansion'].update(reference_date='2026-09-08', calculated_symbols=0)
        payload['iv_expansions'] = []
        self.assertEqual(check(payload, now), [])
        # Same state must fail once regular option trading starts, even if the
        # payload incorrectly claims pre-market.
        payload['market_session'] = 'premarket'
        regular = datetime(2026, 9, 9, 13, 30, tzinfo=timezone.utc)
        payload['server_time'] = regular.isoformat()
        self.assertTrue(check(payload, regular))

    def test_premarket_still_requires_valid_calibration_and_references(self):
        now = datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)
        for mutation in ('too_old', 'missing_version', 'missing_references', 'stale_dashboard'):
            payload = healthy(now)
            payload['iv_expansion'].update(reference_date='2026-09-08', calculated_symbols=0)
            if mutation == 'too_old': payload['calibration']['active_date'] = '2026-09-03'
            if mutation == 'missing_version': payload['calibration']['active_version'] = None
            if mutation == 'missing_references': payload['iv_expansion']['reference_symbols'] = 0
            if mutation == 'stale_dashboard': payload['dashboard_state']['status'] = 'stale'
            self.assertTrue(check(payload, now), mutation)

    def test_s3_only_consumer_can_serve_explicit_prior_calibration(self):
        now = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
        payload = healthy(now)
        payload['dashboard_state']['data_date'] = '2026-09-08'
        payload['iv_expansion']['reference_date'] = '2026-09-08'
        payload['calibration'].update(cache_mode='s3_only', is_stale=True)
        self.assertEqual(check(payload, now), [])
        payload['calibration']['active_date'] = '2026-09-03'
        self.assertTrue(check(payload, now))
        payload['calibration']['active_date'] = '2026-09-04'
        payload['feeds']['OPRA.PILLAR']['last_record_at'] = None
        self.assertTrue(check(payload, now))

    def test_healthy_and_overnight_warning(self):
        payload = healthy()
        payload['feeds']['OCEA.MEMOIR'] = {'state': 'retrying'}
        self.assertEqual(check(payload, NOW), [])

    def test_rejects_missing_stale_or_incompatible_state(self):
        changes = [('server_time', None), ('calibration', {}), ('iv_expansions', []),
                   ('feeds', {}), ('dashboard_state', {'status': 'stale'}),
                   ('iv_expansion', {})]
        for name, value in changes:
            with self.subTest(field=name):
                payload = healthy()
                payload[name] = value
                self.assertTrue(check(payload, NOW))
        payload = healthy()
        payload['calibration']['active_date'] = '2026-09-03'
        self.assertTrue(check(payload, NOW))

    def test_checks_stale_quotes_even_if_backend_says_closed(self):
        payload = healthy()
        payload['market_session'] = 'closed'
        payload['feeds']['OPRA.PILLAR']['last_record_at'] = '2026-09-08T13:00:00+00:00'
        self.assertTrue(check(payload, NOW))

    def test_no_freshness_requirement_on_holidays_or_after_early_close(self):
        for now in [datetime(2026, 9, 7, 14, tzinfo=timezone.utc),
                    datetime(2026, 11, 27, 19, tzinfo=timezone.utc)]:
            payload = healthy(now)
            payload['feeds'] = {}
            payload['iv_expansions'] = []
            self.assertEqual(check(payload, now), [])

    def test_weekend_s3_consumer_uses_friday_as_latest_closed_session(self):
        now = datetime(2026, 9, 20, 5, tzinfo=timezone.utc)
        payload = healthy(now)
        payload['dashboard_state']['data_date'] = '2026-09-18'
        payload['iv_expansion'].update(reference_date='2026-09-18', calculated_symbols=0)
        payload['iv_expansions'] = []
        payload['calibration'].update(
            active_date='2026-09-17', cache_mode='s3_only', is_stale=True)
        self.assertEqual(check(payload, now), [])
        payload['calibration']['active_date'] = '2026-09-16'
        self.assertIn('Compatible sigma calibration is not loaded for the dashboard date', check(payload, now))

    def test_reference_date_not_yesterday_panel_date(self):
        payload = healthy()
        payload['dashboard_state']['data_date'] = '2026-09-08'
        self.assertEqual(check(payload, NOW), [])


class CacheRollbackTests(unittest.TestCase):
    def test_prune_removes_abandoned_then_oldest_snapshot_until_space_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'cache'
            snapshots = Path(directory) / 'snapshots'
            root.mkdir()
            snapshots.mkdir()
            (root / 'dashboard-state.json').write_text('current')
            old = snapshots / 'old'
            newest = snapshots / 'newest'
            abandoned = snapshots / 'abandoned'
            for path in (old, newest, abandoned):
                path.mkdir()
            (old / 'manifest.json').write_text('{}')
            (newest / 'manifest.json').write_text('{}')
            os.utime(old, (1, 1))
            os.utime(newest, (2, 2))

            def available(_):
                return SimpleNamespace(free=0 if old.exists() else 2 * 1024**3)

            with patch('scripts.deployment_cache.shutil.disk_usage', side_effect=available):
                removed = prune(root, snapshots)

            self.assertEqual(removed, ['abandoned', 'old'])
            self.assertFalse(old.exists())
            self.assertFalse(abandoned.exists())
            self.assertTrue(newest.exists())

    def test_restore_old_cache_without_reverting_new_discord_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            root, saved = Path(directory) / 'cache', Path(directory) / 'snapshot'
            root.mkdir()
            (root / 'dashboard-state.json').write_text('{"old": true}')
            with sqlite3.connect(root / 'discord-alerts.sqlite3') as db:
                db.execute('CREATE TABLE alerts (ticker TEXT)')
                db.execute("INSERT INTO alerts VALUES ('AAA')")
            backup(root, saved)
            (root / 'dashboard-state.json').write_text('{"candidate": true}')
            (root / 'iv-expansion-statistics-session-batch.json').write_text('{}')
            with sqlite3.connect(root / 'discord-alerts.sqlite3') as db:
                db.execute("INSERT INTO alerts VALUES ('BBB')")
            restore(root, saved)
            self.assertEqual(json.loads((root / 'dashboard-state.json').read_text()), {'old': True})
            self.assertFalse((root / 'iv-expansion-statistics-session-batch.json').exists())
            with sqlite3.connect(root / 'discord-alerts.sqlite3') as db:
                self.assertEqual(db.execute('SELECT ticker FROM alerts').fetchall(), [('AAA',), ('BBB',)])

    def test_rejects_tampering_before_touching_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root, saved = Path(directory) / 'cache', Path(directory) / 'snapshot'
            root.mkdir()
            (root / 'dashboard-state.json').write_text('old')
            backup(root, saved)
            (root / 'dashboard-state.json').write_text('new')
            (saved / 'dashboard-state.json').write_text('damaged')
            with self.assertRaises(ValueError):
                restore(root, saved)
            self.assertEqual((root / 'dashboard-state.json').read_text(), 'new')

    def test_sqlite_wal_checkpoint_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root, saved = Path(directory) / 'cache', Path(directory) / 'snapshot'
            root.mkdir()
            name = 'iv-expansion-history-session-batch.sqlite3'
            with sqlite3.connect(root / name) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE observations (value INTEGER)')
                db.execute('INSERT INTO observations VALUES (1)')
                db.commit()
                backup(root, saved)
            with sqlite3.connect(root / name) as db:
                db.execute('INSERT INTO observations VALUES (2)')
            restore(root, saved)
            with sqlite3.connect(root / name) as db:
                self.assertEqual(db.execute('SELECT * FROM observations').fetchall(), [(1,)])

    def test_rejects_overlapping_cache_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                backup(Path(directory), Path(directory) / 'child')


class ActivationWiringTests(unittest.TestCase):
    def test_production_alert_threshold_is_pinned_to_one_point_five_sigma(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / 'deploy' / 'compose.yml').read_text()
        self.assertIn('ZSCORE_ALERT_THRESHOLD: "1.5"', compose)

    def test_both_lanes_use_same_guard(self):
        root = Path(__file__).resolve().parents[1]
        for name, environment in [('ec2-deploy.sh', 'production'), ('ec2-deploy-staging.sh', 'staging')]:
            self.assertIn('guarded-activate.sh" ' + environment, (root / 'deploy' / name).read_text())
        guard = (root / 'deploy/guarded-activate.sh').read_text()
        self.assertIn('flock -x 9', guard)
        self.assertIn('restore_timers', guard)
        self.assertIn('"$environment" == staging &&', guard)
        self.assertIn('scripts.deployment_health', guard)
