import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentq.api import QueueError
from agentq.models import Project, Task
from agentq.runner import Worker, agent_runtime_description, fail_task, run_agent, run_agent_to_file


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

    def drain_requested(self):
        return False


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
        self.assertEqual(sleep.call_count, 30)
        sleep.assert_called_with(1)
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
                patch(
                    "agentq.runner.agent_runtime_description",
                    return_value="codex (gpt-5.6-sol, high reasoning)",
                ),
                patch.object(Worker, "_process_implementation"),
                patch("agentq.runner.format_log_timestamp", return_value="2026-06-15 01:49:54 PM"),
                redirect_stdout(stdout),
            ):
                worker.process_task(make_project(), task)

            self.assertIn(
                "[2026-06-15 01:49:54 PM] demo: task 7 started with "
                "codex (gpt-5.6-sol, high reasoning); "
                "attach: python3 -m agentq attach --run run-1 --all",
                stdout.getvalue(),
            )

    def test_process_task_log_names_the_claude_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")
            run_log = FakeRunLog(Path(tmp))
            stdout = StringIO()

            with (
                patch("agentq.runner.RunLog", return_value=run_log),
                patch(
                    "agentq.runner.agent_runtime_description",
                    return_value="claude (claude-opus-4-6, xhigh reasoning)",
                ),
                patch.object(Worker, "_process_implementation"),
                patch("agentq.runner.format_log_timestamp", return_value="2026-06-15 01:49:54 PM"),
                redirect_stdout(stdout),
            ):
                worker.process_task(make_claude_project(), task)

            self.assertIn(
                "task 7 started with claude (claude-opus-4-6, xhigh reasoning);",
                stdout.getvalue(),
            )

    def test_codex_runtime_description_reads_model_and_reasoning_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            repo = root / "repo"
            codex_home.mkdir()
            (repo / ".codex").mkdir(parents=True)
            (codex_home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "medium"\n'
            )
            (repo / ".codex" / "config.toml").write_text(
                'model_reasoning_effort = "high"\n'
            )

            description = agent_runtime_description(
                make_project(repo),
                home=root,
                environ={"CODEX_HOME": str(codex_home)},
            )

            self.assertEqual(description, "codex (gpt-5.6-sol, high reasoning)")

    def test_claude_runtime_description_reads_settings_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            (root / ".claude").mkdir()
            (repo / ".claude").mkdir(parents=True)
            (root / ".claude" / "settings.json").write_text(
                '{"model": "claude-sonnet-4-6", "effortLevel": "medium"}'
            )
            (repo / ".claude" / "settings.local.json").write_text(
                '{"effortLevel": "high"}'
            )

            description = agent_runtime_description(
                make_claude_project(repo),
                home=root,
                environ={
                    "ANTHROPIC_MODEL": "claude-opus-4-6",
                    "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
                },
            )

            self.assertEqual(description, "claude (claude-opus-4-6, xhigh reasoning)")

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

    def test_fail_task_writes_error_only_to_last_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")

            fail_task(client, make_project(), task, run_log, state, "boom", "3s")

            self.assertEqual(client.updates[0][0], ("demo", "7", "FAILED"))
            self.assertEqual(client.updates[0][1]["last_error"], "boom")
            self.assertNotIn("reason", client.updates[0][1])

    def test_failed_implementation_writes_agent_output_to_last_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")

            def fail_agent(
                project,
                prompt,
                run_log,
                phase_name,
                writable=True,
                last_message_file=None,
            ):
                run_log.phase_log(phase_name).write_text(
                    "starting\n"
                    "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 5:39 PM.\n"
                )
                return 1

            with (
                patch("agentq.runner.RunLog", return_value=run_log),
                patch("agentq.runner.run_agent", side_effect=fail_agent),
                patch("agentq.runner.time.monotonic", return_value=3.0),
                patch("agentq.runner.format_log_timestamp", return_value="2026-06-15 01:49:54 PM"),
                redirect_stdout(StringIO()),
            ):
                worker.process_task(make_project(tmp), task)

            self.assertEqual(client.updates[0][0], ("demo", "7", "FAILED"))
            self.assertIn("implementation agent exited with code 1", client.updates[0][1]["last_error"])
            self.assertIn("You've hit your usage limit", client.updates[0][1]["last_error"])
            self.assertNotIn("reason", client.updates[0][1])

    def test_implementation_without_changes_surfaces_agent_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")
            stdout = StringIO()

            def no_change_agent(
                project,
                prompt,
                run_log,
                phase_name,
                writable=True,
                last_message_file=None,
            ):
                self.assertIsNotNone(last_message_file)
                last_message_file.write_text(
                    "I can't implement this because the current repository has no parser code.\n"
                )
                return 0

            with (
                patch("agentq.runner.RunLog", return_value=run_log),
                patch("agentq.runner.run_agent", side_effect=no_change_agent),
                patch("agentq.runner.add_all"),
                patch("agentq.runner.staged_diff", return_value=""),
                patch("agentq.runner.time.monotonic", return_value=3.0),
                patch("agentq.runner.format_log_timestamp", return_value="2026-06-15 01:49:54 PM"),
                redirect_stdout(stdout),
            ):
                worker.process_task(make_project(tmp), task)

            self.assertEqual(client.updates[0][0], ("demo", "7", "FAILED"))
            last_error = client.updates[0][1]["last_error"]
            self.assertIn("implementation agent completed without repository changes", last_error)
            self.assertIn("current repository has no parser code", last_error)
            self.assertIn(
                "demo: task 7 FAILED: implementation agent completed without repository changes",
                stdout.getvalue(),
            )

    def test_implementation_without_changes_falls_back_to_agent_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")

            def no_change_agent(
                project,
                prompt,
                run_log,
                phase_name,
                writable=True,
                last_message_file=None,
            ):
                run_log.phase_log(phase_name).write_text(
                    "The requested component is not present in this checkout.\n"
                )
                return 0

            with (
                patch("agentq.runner.RunLog", return_value=run_log),
                patch("agentq.runner.run_agent", side_effect=no_change_agent),
                patch("agentq.runner.add_all"),
                patch("agentq.runner.staged_diff", return_value=""),
                patch("agentq.runner.time.monotonic", return_value=3.0),
                redirect_stdout(StringIO()),
            ):
                worker.process_task(make_project(tmp), task)

            self.assertIn(
                "requested component is not present",
                client.updates[0][1]["last_error"],
            )

    def test_successful_implementation_writes_plain_sha_and_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            state = FakeState()
            worker = Worker(client, state)
            run_log = FakeRunLog(Path(tmp))
            task = Task(id="7", status="IN PROGRESS", task="Fix parser")

            with (
                patch("agentq.runner.run_agent", return_value=0),
                patch("agentq.runner.add_all"),
                patch("agentq.runner.staged_diff", return_value="diff"),
                patch("agentq.runner.run_agent_to_file", return_value=1),
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
                patch("agentq.runner.run_agent_to_file", side_effect=write_plan),
                patch("agentq.runner.time.monotonic", return_value=42.0),
            ):
                worker._process_plan(make_project(tmp), task, run_log, start=0.0)

            self.assertEqual(client.updates[0][0], ("demo", "8", "PLAN REVIEW"))
            self.assertEqual(client.updates[0][1]["reason"], "plan text")
            self.assertEqual(client.updates[0][1]["runtime"], "42s")


def make_claude_project(repo_path="/tmp/demo"):
    project = make_project(repo_path)
    return Project(
        project_id=project.project_id,
        enabled=project.enabled,
        sheet_name=project.sheet_name,
        repo_path=project.repo_path,
        default_branch=project.default_branch,
        agent="claude",
        use_tdd=project.use_tdd,
        verify_command=project.verify_command,
        poll_seconds=project.poll_seconds,
    )


class AgentDispatchTests(unittest.TestCase):
    def test_run_agent_uses_codex_command_for_codex_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_log = FakeRunLog(Path(tmp))
            run_log.append_output_header = lambda *_: None
            with patch("agentq.runner.run_args_to_logs", return_value=0) as ran:
                last_message_file = Path(tmp) / "last-message.txt"
                run_agent(
                    make_project(tmp),
                    "do it",
                    run_log,
                    "agent.log",
                    writable=True,
                    last_message_file=last_message_file,
                )
            args = ran.call_args[0][0]
            self.assertEqual(args[0], "codex")
            self.assertIn("workspace-write", args)
            self.assertIn("--output-last-message", args)
            self.assertIn(str(last_message_file), args)

    def test_run_agent_uses_claude_command_for_claude_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_log = FakeRunLog(Path(tmp))
            run_log.append_output_header = lambda *_: None

            def write_agent_log(args, cwd, phase_log, output_log):
                phase_log.write_text("The requested code is not in this repository.\n")
                return 0

            with patch("agentq.runner.run_args_to_logs", side_effect=write_agent_log) as ran:
                last_message_file = Path(tmp) / "last-message.txt"
                run_agent(
                    make_claude_project(tmp),
                    "do it",
                    run_log,
                    "agent.log",
                    writable=True,
                    last_message_file=last_message_file,
                )
            args = ran.call_args[0][0]
            self.assertEqual(args[0], "claude")
            self.assertIn("bypassPermissions", args)
            self.assertEqual(args[-1], "do it")
            self.assertEqual(
                last_message_file.read_text(),
                "The requested code is not in this repository.\n",
            )

    def test_run_agent_to_file_uses_plan_mode_for_claude_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_log = FakeRunLog(Path(tmp))
            run_log.append_output_header = lambda *_: None
            with patch("agentq.runner.run_args_to_file", return_value=0) as ran:
                run_agent_to_file(
                    make_claude_project(tmp), "plan it", run_log, "agent.log", Path(tmp) / "out.txt"
                )
            args = ran.call_args[0][0]
            self.assertEqual(args[0], "claude")
            self.assertIn("plan", args)
            self.assertNotIn("bypassPermissions", args)


if __name__ == "__main__":
    unittest.main()
