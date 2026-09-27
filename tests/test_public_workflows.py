import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class PublicWorkflowSafetyTests(unittest.TestCase):
    def test_account_review_and_personal_deploy_workflows_are_absent(self):
        self.assertFalse((WORKFLOWS / "weekly-review.yml").exists())
        self.assertFalse((WORKFLOWS / "fly-deploy.yml").exists())

    def test_remaining_workflows_do_not_upload_account_artifacts_or_use_master_actions(self):
        content = "\n".join(path.read_text() for path in WORKFLOWS.glob("*.yml"))
        self.assertNotIn("upload-artifact", content)
        self.assertNotIn("@master", content)
        self.assertNotIn("weekly_review_agent.py", content)
        self.assertNotIn("flyctl deploy", content)


if __name__ == "__main__":
    unittest.main()
