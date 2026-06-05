import tempfile
import unittest
from pathlib import Path

from agentq.gitutils import ensure_clean_on_branch


class GitUtilsTests(unittest.TestCase):
    def test_ensure_clean_on_branch_reports_missing_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "missing"

            ok, reason = ensure_clean_on_branch(repo, "main")

            self.assertFalse(ok)
            self.assertIn("cannot run git", reason)
            self.assertIn(str(repo), reason)

    def test_ensure_clean_on_branch_reports_non_git_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, reason = ensure_clean_on_branch(tmp, "main")

            self.assertFalse(ok)
            self.assertIn("not a git repository", reason)


if __name__ == "__main__":
    unittest.main()
