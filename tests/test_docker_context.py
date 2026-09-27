import unittest

from scripts.check_docker_context import SENSITIVE_CONTEXT_PATHS, check, docker_path_is_ignored


class DockerContextTests(unittest.TestCase):
    def test_sensitive_local_artifacts_are_excluded(self):
        self.assertEqual(check(), [])

    def test_negation_semantics_are_deterministic(self):
        rules = [".env.*", "!.env.example"]
        self.assertTrue(docker_path_is_ignored(".env.private", rules))
        self.assertFalse(docker_path_is_ignored(".env.example", rules))
        self.assertIn("preview_server.py", SENSITIVE_CONTEXT_PATHS)


if __name__ == "__main__":
    unittest.main()
