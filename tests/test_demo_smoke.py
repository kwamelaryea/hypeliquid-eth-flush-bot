import unittest

from scripts.demo_smoke import main


class DemoSmokeTests(unittest.TestCase):
    def test_clean_environment_demo_server(self):
        self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
