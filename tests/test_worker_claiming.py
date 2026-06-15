import subprocess
import tempfile
import unittest
from pathlib import Path

from agentq.models import Project, Task
from agentq.runner import Worker
from agentq.state import StateStore


class FakeClient:
    def __init__(self, project):
        self.project = project
        self.claim_calls = []
        self.task = None

    def get_project(self, project_id):
        return self.project if self.project.project_id == project_id else None

    def claim(self, project_id, worker_id, resume_only=False):
        self.claim_calls.append((project_id, resume_only))
        if resume_only:
            return None
        return self.task


def run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def make_repo(path: Path):
    run(["git", "init", "-b", "main"], path)
    run(["git", "config", "user.email", "agentq@example.com"], path)
    run(["git", "config", "user.name", "agentq tests"], path)
    (path / "README.md").write_text("hello\n")
    run(["git", "add", "README.md"], path)
    run(["git", "commit", "-m", "initial"], path)


class WorkerClaimingTests(unittest.TestCase):
    def make_project(self, repo: Path) -> Project:
        return Project(
            project_id="demo",
            enabled=True,
            sheet_name="Demo",
            repo_path=str(repo),
            default_branch="main",
            agent="codex",
            use_tdd=False,
            verify_command="python3 -m unittest",
            poll_seconds=5,
        )

    def test_dirty_repo_does_not_claim_new_task(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_tmp:
            repo = Path(tmp)
            make_repo(repo)
            (repo / "dirty.txt").write_text("not committed\n")
            project = self.make_project(repo)
            client = FakeClient(project)
            worker = Worker(client, StateStore(Path(state_tmp) / "state.json"))

            task = worker._claim_when_safe(project, "worker-1")

            self.assertIsNone(task)
            self.assertEqual(client.claim_calls, [("demo", True)])

    def test_resume_task_can_be_claimed_before_clean_check(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_tmp:
            repo = Path(tmp)
            make_repo(repo)
            (repo / "dirty.txt").write_text("partial work\n")
            project = self.make_project(repo)
            client = FakeClient(project)
            resume_task = Task(id="7", status="IN PROGRESS", task="Continue", resume=True)

            def claim(project_id, worker_id, resume_only=False):
                client.claim_calls.append((project_id, resume_only))
                return resume_task if resume_only else None

            client.claim = claim
            worker = Worker(client, StateStore(Path(state_tmp) / "state.json"))

            task = worker._claim_when_safe(project, "worker-1")

            self.assertEqual(task, resume_task)
            self.assertEqual(client.claim_calls, [("demo", True)])

    def test_drain_requested_worker_exits_without_claiming(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_tmp:
            repo = Path(tmp)
            make_repo(repo)
            project = self.make_project(repo)
            client = FakeClient(project)
            state = StateStore(Path(state_tmp) / "state.json")
            state.request_drain()
            worker = Worker(client, state)

            result = worker._run_locked("demo", forever=True)

            self.assertEqual(result, 0)
            self.assertEqual(client.claim_calls, [])

    def test_drain_after_task_stops_before_next_claim(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_tmp:
            repo = Path(tmp)
            make_repo(repo)
            project = self.make_project(repo)
            client = FakeClient(project)
            client.task = Task(id="7", status="IN PROGRESS", task="Finish current task")
            state = StateStore(Path(state_tmp) / "state.json")
            worker = Worker(client, state)

            def process_task(_project, _task):
                state.request_drain()

            worker.process_task = process_task

            result = worker._run_locked("demo", forever=True)

            self.assertEqual(result, 0)
            self.assertEqual(client.claim_calls, [("demo", True), ("demo", False)])


if __name__ == "__main__":
    unittest.main()
