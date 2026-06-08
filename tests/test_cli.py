import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from agentq.cli import build_parser, cmd_status
from agentq.models import Project


class CliTests(unittest.TestCase):
    def test_attach_requires_project_or_run(self):
        parser = build_parser()
        args = parser.parse_args(["attach", "--run", "abc"])
        self.assertEqual(args.run, "abc")

    def test_worker_project_argument(self):
        parser = build_parser()
        args = parser.parse_args(["worker", "--project", "demo"])
        self.assertEqual(args.project, "demo")
        self.assertFalse(args.forever)

    def test_status_prints_module_attach_command(self):
        project = Project(
            project_id="demo",
            enabled=True,
            sheet_name="Demo",
            repo_path="/tmp/demo",
            default_branch="main",
            agent="codex",
            use_tdd=False,
            verify_command="python3 -m unittest",
            poll_seconds=5,
        )
        state = {
            "active": {"demo": "demo-7-20260608-134954"},
            "runs": {
                "demo-7-20260608-134954": {
                    "taskId": "7",
                    "status": "RUNNING",
                    "outputLog": "/tmp/output.log",
                }
            },
        }
        stdout = StringIO()

        with (
            patch("agentq.cli.QueueClient") as client_cls,
            patch("agentq.cli.StateStore") as state_cls,
            redirect_stdout(stdout),
        ):
            client_cls.return_value.list_projects.return_value = [project]
            state_cls.return_value.read.return_value = state
            result = cmd_status(build_parser().parse_args(["status"]))

        self.assertEqual(result, 0)
        self.assertIn("attach: python3 -m agentq attach --run demo-7-20260608-134954", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
