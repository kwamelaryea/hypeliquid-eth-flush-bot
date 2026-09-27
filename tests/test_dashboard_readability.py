import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "index.html"
STYLESHEET = ROOT / "static" / "dashboard.css"
SCRIPT = ROOT / "static" / "dashboard.js"


class DashboardReadabilityTests(unittest.TestCase):
    def test_dark_theme_text_tokens_meet_readability_floor(self):
        css = STYLESHEET.read_text()
        self.assertIn("--text-dim:      #9aa7bd;", css)
        self.assertIn("--text-muted:    #7b879c;", css)
        self.assertNotIn("--text-dim:      #64748b;", css)
        self.assertNotIn("--text-muted:    #3f4a5a;", css)

    def test_execution_log_rows_do_not_fade_below_readable_opacity(self):
        script = SCRIPT.read_text()
        self.assertIn("Math.max(0.72, 1 - (index * 0.04))", script)
        self.assertNotIn("Math.max(0.35, 1 - (i * 0.07))", script)

    def test_tiny_labels_have_readable_minimum_size(self):
        css = STYLESHEET.read_text()
        root_css = css.split("@media (max-width: 420px)")[0]
        tiny_sizes = [int(size) for size in re.findall(r"font-size:\s*(\d+)px", root_css) if int(size) < 10]
        self.assertEqual(tiny_sizes, [])

    def test_server_data_is_rendered_without_html_interpolation(self):
        script = SCRIPT.read_text()
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        self.assertIn("message.textContent", script)
        self.assertIn("reasonEl.textContent", script)


if __name__ == "__main__":
    unittest.main()
