import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StagingDeploymentTests(unittest.TestCase):
    def test_compose_is_isolated_and_has_no_alerter(self):
        compose = (ROOT / "deploy/compose.staging.yml").read_text()

        self.assertIn("name: sigma-tape-staging", compose)
        self.assertIn('"127.0.0.1:8766:8765"', compose)
        self.assertIn("/var/lib/sigma-tape-staging:/app/.cache/sigma-tape", compose)
        self.assertIn("/etc/sigma-tape/sigma-tape-staging.env", compose)
        self.assertNotIn("alerter:", compose)
        self.assertNotIn("DISCORD_BOT_TOKEN", compose)

    def test_deployer_cannot_replace_production_paths(self):
        deployer = (ROOT / "deploy/ec2-deploy-staging.sh").read_text()

        self.assertIn('release_root="/opt/sigma-tape-staging/releases"', deployer)
        self.assertIn('staging_cache="/var/lib/sigma-tape-staging"', deployer)
        self.assertIn('sudo test -f "$production_cache/$cache_name"', deployer)
        self.assertIn("http://127.0.0.1:8766/api/health", deployer)
        self.assertNotIn("docker compose -f deploy/compose.yml", deployer)
        self.assertNotIn("systemctl", deployer)

    def test_workflow_uses_the_staging_environment(self):
        workflow = (ROOT / ".github/workflows/deploy-staging.yml").read_text()

        self.assertIn("branches: [codex/staging]", workflow)
        self.assertIn("name: staging", workflow)
        self.assertIn("https://staging.optionsniper.xyz", workflow)
        self.assertNotIn("DISCORD_BOT_TOKEN", workflow)


if __name__ == "__main__":
    unittest.main()
