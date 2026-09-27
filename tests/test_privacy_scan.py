import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import hashlib
import subprocess

from scripts.privacy_scan import scan_git_history, scan_paths


class PrivacyScanTests(unittest.TestCase):
    def scan_text(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.txt"
            path.write_text(text)
            return scan_paths([path], root=Path(directory))

    def test_detects_required_privacy_and_secret_categories(self):
        cases = {
            "email": "person" + "@example.test",
            "private path": "/" + "Users/example/private/project",
            "private key": "0x" + ("a" * 64),
            "token": "ghp_" + ("A" * 24),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assertTrue(self.scan_text(value), name)

    def test_detects_known_production_identifier_by_digest(self):
        synthetic_identifier = "private-deployment-example"
        digest = hashlib.sha256(synthetic_identifier.encode()).hexdigest()
        with patch("scripts.privacy_scan.PRIVATE_PRODUCTION_ID_DIGEST", digest):
            findings = self.scan_text(synthetic_identifier)
        self.assertTrue(any("known private production id" in finding for finding in findings))

    def test_detects_known_wallet_by_digest_without_storing_identifier(self):
        synthetic_wallet = "0x" + ("1" * 40)
        digest = hashlib.sha256(synthetic_wallet.encode()).hexdigest()
        with patch("scripts.privacy_scan.OWNER_WALLET_DIGEST", digest):
            findings = self.scan_text(synthetic_wallet)
        self.assertTrue(any("known owner wallet" in finding for finding in findings))

    def test_allows_placeholders_environment_lookups_and_public_contracts(self):
        safe = "\n".join([
            "OPERATIONAL_API_TOKEN=",
            'token = os.getenv("API_TOKEN", "")',
            "contact maintainers through a private advisory",
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        ])
        self.assertEqual(self.scan_text(safe), [])

    def test_full_history_scans_deleted_blobs_and_commit_metadata_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "fixture" + "@example.test"], cwd=root, check=True)
            secret = "ghp_" + ("Z" * 24)
            old = root / "old.txt"
            old.write_text(secret)
            subprocess.run(["git", "add", "old.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "old object"], cwd=root, check=True)
            old.unlink()
            subprocess.run(["git", "add", "-u"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "delete old object"], cwd=root, check=True)

            findings, refs_scanned, objects_scanned = scan_git_history(root)

        self.assertGreaterEqual(refs_scanned, 1)
        self.assertGreaterEqual(objects_scanned, 3)
        self.assertTrue(any("token-like value" in finding for finding in findings))
        self.assertTrue(any("email address" in finding for finding in findings))
        self.assertNotIn(secret, "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
