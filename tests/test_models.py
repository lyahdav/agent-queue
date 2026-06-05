import unittest

from agentq.models import Project, Task


class ModelTests(unittest.TestCase):
    def test_project_defaults_and_truthy_enabled(self):
        project = Project.from_json(
            {
                "projectId": "demo",
                "enabled": "TRUE",
                "repoPath": "/tmp/demo",
                "verifyCommand": "python3 -m unittest",
            }
        )

        self.assertEqual(project.project_id, "demo")
        self.assertTrue(project.enabled)
        self.assertEqual(project.sheet_name, "demo")
        self.assertEqual(project.default_branch, "main")
        self.assertEqual(project.agent, "codex")
        self.assertFalse(project.use_tdd)
        self.assertEqual(project.poll_seconds, 30)

    def test_project_parses_tdd_flag(self):
        project = Project.from_json(
            {
                "projectId": "demo",
                "enabled": True,
                "repoPath": "/tmp/demo",
                "tdd": "yes",
            }
        )

        self.assertTrue(project.use_tdd)

    def test_task_preserves_claimed_from(self):
        task = Task.from_json(
            {
                "id": 4,
                "status": "IN PROGRESS",
                "task": "Fix parser",
                "claimedFrom": "REDO",
                "resume": False,
            }
        )

        self.assertEqual(task.id, "4")
        self.assertEqual(task.original_status, "REDO")
        self.assertFalse(task.resume)


if __name__ == "__main__":
    unittest.main()
