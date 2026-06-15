import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentq.api import QueueError
from agentq.models import Project, Task
from agentq.runner import Worker, fail_task


class FakeClient:
    def __init__(self):
        self.updates = []

    def update(self, *args, **kwargs):
        self.updates.append((args, kwargs))


class FakeRunLog:
    run_id = "run-1"

    def __init__(self, path: Path):
        self.run_dir = path
        self.output_log = path / "output.log"
        self.events = []

    def phase_log(self, name):
        return self.run_dir / name

    def event(self, phase, message, **fields):
        self.events.append((phase, message, fields))


class FakeState:
    def __init__(self):
        self.updates = []
        self.finishes = []

    def update_run(self, *args, **kwargs):
        self.updates.append((args, kwargs))

    def finish_run(self, *args, **kwargs):
        self.finishes.append((args, kwargs))


class FlakyProjectClient:
    def __init__(self, project):
        self.project = project
        self.get_project_calls = 0

    def get_project(self, project_id):
        self.get_project_calls += 1
        if self.get_project_calls == 1:
            raise QueueError("offline")
        return self.project if self.project.project_id == project_id else None

    def claim(self, *_args, **_kwargs):
        return None


class FailingProjectClient:
    def get_project(self, _project_id):
        raise QueueError("offline")


def make_project(repo_path="/tmp/demo"):
    return Project(
        project_id="demo",
        enabled=True,
        sheet_name="Demo",
        repo_path=repo_path,
        default_branch="main",
        agent="codex",
        use_tdd=False,
        verify_command="python3 -m unittest",
        poll_seconds=5,
    )


def make_disabled_project():
    project = make_project()
    return Project(
        project_id=project.project_id,
        enabled=False,
        sheet_name=project.sheet_name,
        repo_path=project.repo_path,
        default_branch=project.default_branch,
        agent=project.agent,
        use_tdd=project.use_tdd,
        verify_command=project.verify_command,
        poll_seconds=project.poll_seconds,
    )


class RunnerRuntimeTests(unittest.TestCase):
    def test_forever_worker_retries_queue_errors(self):
        client = FlakyProjectClient(make_disabled_project())
        worker = Worker(client, FakeState())
        stdout = StringIO()

        with (
            patch("agentq.runner.time.sleep") as sleep,
            patch("agentq.runner.format_log_timestamp", return_value="2026-06-15 01:49:54 PM"),
            redirect_stdout(stdout),
        ):
            result = worker._run_locked("demo", forever=True)

        self.assertEqual(result, 0)
        self.assertEqual(client.get_project_calls, 2)
        sleep.assert_called_once_with(30)
        self.assertIn("[2026-06-15 01:49:54 PM] demo: queue unavailable; retrying in 30s: offline", stdout.getvalue())

    def test_non_forever_worker_surfaces_queue_errors(self):
        worker = Worker(FailingProjectClient(), FakeState())

        with self.assertRaises(QueueError):
            worker._run_locked("demo", forever=False)

    def test_process_task_prints_module_attach_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")
            run_log = FakeRunLog(Path(tmp))
            stdout = StringIO()

            with (
                patch("agentq.runner.RunLog", return_value=run_log),
                patch.object(Worker, "_process_implementation"),
                patch("agentq.runner.format_log_timestamp", return_value="2026-06-15 01:49:54 PM"),
                redirect_stdout(stdout),
            ):
                worker.process_task(make_project(), task)

            self.assertIn(
                "[2026-06-15 01:49:54 PM] demo: task 7 started; attach: python3 -m agentq attach --run run-1 --all",
                stdout.getvalue(),
            )

    def test_fail_task_writes_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")

            fail_task(client, make_project(), task, run_log, state, "boom", "3s")

            self.assertEqual(client.updates[0][0], ("demo", "7", "FAILED"))
            self.assertEqual(client.updates[0][1]["runtime"], "3s")
            self.assertEqual(state.finishes[0][1]["runtime"], "3s")

    def test_successful_implementation_writes_plain_sha_and_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")

            with (
                patch("agentq.runner.run_codex", return_value=0),
                patch("agentq.runner.add_all"),
                patch("agentq.runner.staged_diff", return_value="diff"),
                patch("agentq.runner.run_codex_to_file", return_value=1),
                patch("agentq.runner.commit"),
                patch.object(Worker, "_verify_with_fix_loop"),
                patch("agentq.runner.push"),
                patch("agentq.runner.commit_sha", return_value="abc123"),
                patch("agentq.runner.time.monotonic", return_value=65.0),
            ):
                worker._process_implementation(make_project(tmp), task, run_log, start=0.0)

            self.assertEqual(client.updates[0][0], ("demo", "7", "VERIFY"))
            self.assertEqual(client.updates[0][1]["sha"], "abc123")
            self.assertEqual(client.updates[0][1]["runtime"], "1m 5s")

    def test_successful_plan_writes_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="8", status="PLAN IN PROGRESS", task="Plan parser")

            def write_plan(project, prompt, run_log, phase_name, output_file):
                output_file.write_text("plan text\n")
                return 0

            with (
                patch("agentq.runner.run_codex_to_file", side_effect=write_plan),
                patch("agentq.runner.time.monotonic", return_value=42.0),
            ):
                worker._process_plan(make_project(tmp), task, run_log, start=0.0)

            self.assertEqual(client.updates[0][0], ("demo", "8", "PLAN REVIEW"))
            self.assertEqual(client.updates[0][1]["reason"], "plan text")
            self.assertEqual(client.updates[0][1]["runtime"], "42s")


if __name__ == "__main__":
    unittest.main()
