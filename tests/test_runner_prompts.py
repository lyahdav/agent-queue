import unittest

from agentq.models import Project, Task
from agentq.runner import fix_prompt, implementation_prompt, plan_prompt


class RunnerPromptTests(unittest.TestCase):
    def make_project(self, use_tdd=False):
        return Project(
            project_id="demo",
            enabled=True,
            sheet_name="Demo",
            repo_path="/tmp/demo",
            default_branch="main",
            agent="codex",
            use_tdd=use_tdd,
            verify_command="python3 -m unittest",
            poll_seconds=5,
        )

    def test_implementation_prompt_requires_tdd_when_enabled(self):
        prompt = implementation_prompt(
            self.make_project(use_tdd=True),
            Task(id="1", status="READY", task="Add parser feature"),
        )

        self.assertIn("Use TDD for code changes", prompt)
        self.assertIn("If the task only changes documentation", prompt)

    def test_implementation_prompt_omits_tdd_when_disabled(self):
        prompt = implementation_prompt(self.make_project(), Task(id="1", status="READY", task="Add parser feature"))

        self.assertNotIn("Use TDD for code changes", prompt)
        self.assertIn("TDD is not required for this project", prompt)

    def test_plan_prompt_mentions_later_tdd_policy_when_enabled(self):
        prompt = plan_prompt(self.make_project(use_tdd=True), Task(id="1", status="PLAN", task="Plan parser feature"))

        self.assertIn("Use TDD for code changes", prompt)

    def test_fix_prompt_mentions_tdd_policy_when_enabled(self):
        prompt = fix_prompt(
            Task(id="1", status="IN PROGRESS", task="Add parser feature"),
            "python3 -m unittest",
            "commit details",
            "failure details",
            use_tdd=True,
        )

        self.assertIn("Use TDD for any new code behavior", prompt)

    def test_fix_prompt_omits_tdd_when_disabled(self):
        prompt = fix_prompt(
            Task(id="1", status="IN PROGRESS", task="Add parser feature"),
            "python3 -m unittest",
            "commit details",
            "failure details",
            use_tdd=False,
        )

        self.assertNotIn("Use TDD", prompt)
        self.assertIn("TDD is not required for this project's fix", prompt)


if __name__ == "__main__":
    unittest.main()
