import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / ".git").exists():
            raise unittest.SkipTest("repository policy requires a Git checkout")
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        cls.tracked = [
            Path(value.decode())
            for value in result.stdout.split(b"\0")
            if value
        ]

    def test_generated_market_data_and_private_cache_are_not_tracked(self):
        forbidden_names = {"snapshot-state.json", "iv-universe-state.json"}
        forbidden_parts = {".cache", "artifacts", "data"}
        violations = [
            str(path)
            for path in self.tracked
            if path.name in forbidden_names
            or path.suffix in {".dbn", ".zst"}
            or forbidden_parts.intersection(path.parts)
        ]
        self.assertEqual(violations, [])

    def test_only_environment_template_is_tracked(self):
        tracked_env = [str(path) for path in self.tracked if path.name.startswith(".env")]
        self.assertEqual(tracked_env, [".env.example"])
        values = [
            line.partition("=")[2].strip()
            for line in (ROOT / ".env.example").read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        ]
        self.assertTrue(values)
        self.assertTrue(all(not value for value in values))

    def test_no_credential_shaped_values_are_tracked(self):
        patterns = {
            "Databento": re.compile(r"db-[A-Za-z0-9]{24,}"),
            "Discord bot": re.compile(
                r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}"
            ),
            "ThetaData": re.compile(r"td1_prod_[A-Za-z0-9]{16,}"),
            "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
        }
        violations = []
        for relative in self.tracked:
            path = ROOT / relative
            try:
                content = path.read_text()
            except UnicodeDecodeError:
                continue
            for label, pattern in patterns.items():
                if pattern.search(content):
                    violations.append(f"{label}: {relative}")
        self.assertEqual(violations, [])

    def test_browser_assets_never_reference_server_credentials(self):
        content = "\n".join(
            path.read_text()
            for path in (ROOT / "dashboard").iterdir()
            if path.is_file()
        )
        for variable in (
            "DATABENTO_API_KEY",
            "DATABENTO_LIVE_API_KEY",
            "DATABENTO_OPTIONS_API_KEY",
        ):
            self.assertNotIn(variable, content)

    def test_production_container_is_local_only_and_non_root(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "deploy" / "compose.yml").read_text()

        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('"--refresh-mode", "never"', dockerfile)
        self.assertIn('"127.0.0.1:8765:8765"', compose)
        self.assertIn("discord-alerts.sqlite3", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("max-size: 10m", compose)
        self.assertNotIn("DATABENTO_", dockerfile)
        self.assertNotIn("DATABENTO_", compose)

    def test_production_refresh_runs_before_premarket_ranking(self):
        timer = (ROOT / "deploy" / "option-sniper-refresh.timer").read_text()

        self.assertIn(
            "OnCalendar=Tue..Sat *-*-* 03:15:00 America/New_York",
            timer,
        )
        self.assertIn(
            "OnCalendar=Tue..Sat *-*-* 03:45:00 America/New_York",
            timer,
        )

    def test_production_deployment_requires_ci_main_and_trusted_ssh(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        deploy_script = (ROOT / "deploy" / "ec2-deploy.sh").read_text()

        self.assertIn("workflow_run:", workflow)
        self.assertIn("workflow_run.conclusion == 'success'", workflow)
        self.assertIn("workflow_run.head_branch == 'main'", workflow)
        self.assertIn("workflow_run.event == 'push'", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertIn("secrets.EC2_SSH_KEY", workflow)
        self.assertIn("secrets.EC2_KNOWN_HOSTS", workflow)
        self.assertIn("secrets.DISCORD_BOT_TOKEN", workflow)
        self.assertNotIn("DATABENTO_", workflow)
        self.assertIn("sha256sum", deploy_script)
        self.assertIn("rollback", deploy_script)
        self.assertIn("<title>Option Sniper</title>", deploy_script)
        self.assertIn('alerter_state="$(sudo docker inspect', deploy_script)
        self.assertIn("Discord alerter did not become healthy", deploy_script)
        self.assertIn("option-sniper-discord-token-$revision", deploy_script)
        self.assertIn("discord_alert_sent", deploy_script)
        self.assertIn("up -d --no-build --force-recreate", deploy_script)


if __name__ == "__main__":
    unittest.main()
