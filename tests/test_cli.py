import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from agentq.api import QueueError
from agentq.cli import build_parser, cmd_attach, cmd_status, cmd_watch, main, _spawn_worker
from agentq.models import Project


class CliTests(unittest.TestCase):
    def test_attach_requires_project_or_run(self):
        parser = build_parser()
        args = parser.parse_args(["attach", "--run", "abc", "--all"])
        self.assertEqual(args.run, "abc")
        self.assertTrue(args.all)

    def test_worker_project_argument(self):
        parser = build_parser()
        args = parser.parse_args(["worker", "--project", "demo"])
        self.assertEqual(args.project, "demo")
        self.assertFalse(args.forever)

    def test_spawn_worker_uses_new_session_to_avoid_ctrl_c_tracebacks(self):
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

        with patch("agentq.cli.subprocess.Popen") as popen:
            _spawn_worker(project)

        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

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
        self.assertIn("attach: python3 -m agentq attach --run demo-7-20260608-134954 --all", stdout.getvalue())

    def test_attach_all_tails_from_start(self):
        state = {
            "active": {},
            "runs": {
                "demo-7-20260608-134954": {
                    "outputLog": "/tmp/output.log",
                }
            },
        }

        with (
            patch("agentq.cli.StateStore") as state_cls,
            patch("agentq.cli.Path.exists", return_value=True),
            patch("agentq.cli.subprocess.call", return_value=0) as call,
        ):
            state_cls.return_value.run.return_value = state["runs"]["demo-7-20260608-134954"]
            result = cmd_attach(build_parser().parse_args(["attach", "--run", "demo-7-20260608-134954", "--all"]))

        self.assertEqual(result, 0)
        call.assert_called_once_with(["tail", "-n", "+1", "-f", "/tmp/output.log"])

    def test_main_timestamps_queue_errors(self):
        stderr = StringIO()

        with (
            patch("agentq.cli.QueueClient", side_effect=QueueError("boom")),
            patch("agentq.cli.format_log_timestamp", return_value="2026-06-15 02:09:31 PM"),
            redirect_stderr(stderr),
        ):
            result = main(["status"])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "[2026-06-15 02:09:31 PM] queue error: boom\n")

    def test_watch_retries_queue_errors(self):
        stderr = StringIO()
        stdout = StringIO()

        with (
            patch("agentq.cli.QueueClient") as client_cls,
            patch("agentq.cli.StateStore") as state_cls,
            patch("agentq.cli.time.sleep") as sleep,
            patch("agentq.cli.format_log_timestamp", return_value="2026-06-15 02:09:31 PM"),
            redirect_stderr(stderr),
            redirect_stdout(stdout),
        ):
            client_cls.return_value.list_projects.side_effect = [QueueError("offline"), KeyboardInterrupt()]
            result = cmd_watch(build_parser().parse_args(["watch", "--poll-seconds", "5"]))

        self.assertEqual(result, 0)
        self.assertEqual(client_cls.return_value.list_projects.call_count, 2)
        sleep.assert_called_once_with(5)
        state_cls.return_value.request_drain.assert_called_once_with()
        self.assertIn("[2026-06-15 02:09:31 PM] agentq watch queue unavailable; retrying in 5s: offline", stderr.getvalue())
        self.assertIn("agentq watch draining: waiting for in-progress tasks to finish", stdout.getvalue())
        self.assertIn("agentq watch drained; exiting", stdout.getvalue())

    def test_watch_startup_prints_ctrl_c_hint(self):
        stdout = StringIO()

        with (
            patch("agentq.cli.QueueClient") as client_cls,
            patch("agentq.cli.StateStore"),
            redirect_stdout(stdout),
        ):
            client_cls.return_value.list_projects.side_effect = KeyboardInterrupt()
            result = cmd_watch(build_parser().parse_args(["watch", "--poll-seconds", "5"]))

        self.assertEqual(result, 0)
        self.assertIn("agentq watch started", stdout.getvalue())
        self.assertIn(
            "Press Ctrl-C to stop after in-progress tasks finish; press Ctrl-C again to force stop.",
            stdout.getvalue(),
        )

    def test_watch_first_ctrl_c_drains_without_terminating_worker(self):
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
        proc = Mock()
        proc.poll.return_value = 0
        proc.returncode = 0
        stdout = StringIO()

        with (
            patch("agentq.cli.QueueClient") as client_cls,
            patch("agentq.cli.StateStore") as state_cls,
            patch("agentq.cli._spawn_worker", return_value=proc),
            patch("agentq.cli.time.sleep", side_effect=KeyboardInterrupt()),
            redirect_stdout(stdout),
        ):
            client_cls.return_value.list_projects.return_value = [project]
            result = cmd_watch(build_parser().parse_args(["watch", "--poll-seconds", "5"]))

        self.assertEqual(result, 0)
        state_cls.return_value.request_drain.assert_called_once_with()
        proc.terminate.assert_not_called()
        self.assertIn("agentq watch draining: waiting for in-progress tasks to finish", stdout.getvalue())
        self.assertIn("agentq watch drained; exiting", stdout.getvalue())
        self.assertNotIn("worker exited with code", stdout.getvalue())

    def test_watch_second_ctrl_c_force_stops_worker(self):
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
        proc = Mock()
        proc.poll.return_value = None
        stdout = StringIO()

        with (
            patch("agentq.cli.QueueClient") as client_cls,
            patch("agentq.cli.StateStore") as state_cls,
            patch("agentq.cli._spawn_worker", return_value=proc),
            patch("agentq.cli.time.sleep", side_effect=[KeyboardInterrupt(), KeyboardInterrupt()]),
            redirect_stdout(stdout),
        ):
            client_cls.return_value.list_projects.return_value = [project]
            result = cmd_watch(build_parser().parse_args(["watch", "--poll-seconds", "5"]))

        self.assertEqual(result, 0)
        state_cls.return_value.request_drain.assert_called_once_with()
        proc.terminate.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=10)
        self.assertIn("agentq watch stopping", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
