"""Exercise the actual shell transaction with mocked Docker/systemd commands."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActivationRollbackTests(unittest.TestCase):
    def run_drill(self, environment, sigma_mismatch=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            base, cache, units, commands = [root / n for n in ('releases-root', 'cache', 'units', 'bin')]
            for path in (cache, units, commands):
                path.mkdir()
            for version in ('11111111', '22222222'):
                release = base / 'releases' / version
                (release / 'scripts').mkdir(parents=True)
                (release / 'deploy').mkdir()
                shutil.copy(ROOT / 'scripts/deployment_cache.py', release / 'scripts/deployment_cache.py')
                (release / 'scripts/verify_sigma_parity.py').write_text(
                    "import os,sys\n"
                    "if '--capture' not in sys.argv and os.environ.get('GUARD_SIGMA_MISMATCH') == '1':\n"
                    " print('sigma_match: false'); sys.exit(1)\n")
            (base / 'current').symlink_to(base / 'releases/11111111')
            (cache / 'dashboard-state.json').write_text('original')
            script = (ROOT / 'deploy/guarded-activate.sh').read_text()
            # Only filesystem locations and retry count are substituted. No live
            # Docker or systemd command can be reached through this test's PATH.
            for name in ('/opt/sigma-tape-staging', '/opt/sigma-tape'):
                script = script.replace(name, str(base))
            for name in ('/var/lib/sigma-tape-staging', '/var/lib/sigma-tape'):
                script = script.replace(name, str(cache))
            script = script.replace('/etc/systemd/system', str(units)).replace('seq 1 90', 'seq 1 2')
            guard = root / 'guard.sh'
            guard.write_text(script)
            docker = '''#!/bin/sh
echo "$SIGMA_TAPE_VERSION $*" >> "$GUARD_LOG"
case "$1" in
 compose)
  case "$*" in *" up "*)
   echo "$SIGMA_TAPE_VERSION" > "$GUARD_STATE"
   if [ "$SIGMA_TAPE_VERSION" = 22222222 ]; then echo candidate > "$GUARD_CACHE/dashboard-state.json"; fi
  esac ;;
 inspect)
  if [ "$GUARD_MODE" = production ] && [ "$(cat "$GUARD_STATE")" = 22222222 ]; then
   case "$*" in *sigma-tape-alerter*) echo 'running unhealthy'; exit 0;; esac
  fi
  echo 'running healthy' ;;
esac
exit 0
'''
            wrappers = {'docker': docker, 'flock': '#!/bin/sh\nexit 0\n',
                        'sleep': '#!/bin/sh\nexit 0\n', 'systemctl': '#!/bin/sh\nexit 0\n',
                        'python3': '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n',
                        'readlink': '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c "import os,sys; print(os.path.realpath(sys.argv[-1]))" "$@"\n'}
            for name, contents in wrappers.items():
                (commands / name).write_text(contents)
                (commands / name).chmod(0o755)
            env = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
                   'GUARD_LOG': str(root / 'commands.log'), 'GUARD_STATE': str(root / 'version'),
                   'GUARD_CACHE': str(cache), 'GUARD_MODE': environment,
                   'GUARD_SIGMA_MISMATCH': '1' if sigma_mismatch else '0',
                   'SIGMA_STAGING_FAIL_HEALTH': '1' if environment == 'staging' else '0'}
            result = subprocess.run(['bash', str(guard), environment, '22222222'],
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Rollback restored previous code', result.stderr)
            self.assertEqual((cache / 'dashboard-state.json').read_text(), 'original')
            self.assertEqual((base / 'current').resolve().name, '11111111')
            self.assertEqual((root / 'version').read_text().strip(), '11111111')
            if sigma_mismatch:
                self.assertIn('sigma_match: false', result.stdout)
                log = (root / 'commands.log').read_text()
                self.assertIn('stop -t 3', log)
                self.assertNotIn('22222222 compose -f deploy/compose.yml up -d --no-build alerter', log)
                self.assertNotIn('Release readiness failed', result.stderr)
            elif environment == 'production':
                self.assertIn('Release readiness failed', result.stderr)
            else:
                self.assertIn('Intentional staging readiness failure', result.stderr)

    def test_staging_failure_restores_modified_cache_and_image(self):
        self.run_drill('staging')

    def test_unhealthy_production_alerter_fails_and_rolls_back(self):
        self.run_drill('production')

    def test_sigma_mismatch_rolls_back_before_starting_alerter(self):
        self.run_drill('production', sigma_mismatch=True)
