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


if __name__ == "__main__":
    unittest.main()
