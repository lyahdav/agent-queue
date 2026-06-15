from __future__ import annotations

import json
import time
from pathlib import Path

from .config import RUNS_DIR, ensure_app_dirs
from .models import Project, Task
from .state import append_jsonl


def format_log_timestamp() -> str:
    return time.strftime("%Y-%m-%d %I:%M:%S %p")


def make_run_id(project_id: str, task_id: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{project_id}-{task_id}-{stamp}"


def attach_command(run_id: str, *, all_logs: bool = False) -> str:
    command = f"python3 -m agentq attach --run {run_id}"
    if all_logs:
        command += " --all"
    return command


class RunLog:
    def __init__(self, project: Project, task: Task):
        ensure_app_dirs()
        self.run_id = make_run_id(project.project_id, task.id)
        self.run_dir = RUNS_DIR / project.project_id / f"{task.id}-{self.run_id.rsplit('-', 1)[-1]}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.output_log = self.run_dir / "output.log"
        self.events_log = self.run_dir / "events.jsonl"
        metadata = {
            "runId": self.run_id,
            "projectId": project.project_id,
            "taskId": task.id,
            "task": task.task,
            "status": task.status,
            "claimedFrom": task.claimed_from,
            "repoPath": project.repo_path,
            "startedAt": int(time.time()),
        }
        (self.run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")

    def phase_log(self, name: str) -> Path:
        return self.run_dir / name

    def event(self, phase: str, message: str, **fields: object) -> None:
        append_jsonl(self.events_log, {"phase": phase, "message": message, **fields})

    def append_output_header(self, title: str) -> None:
        with self.output_log.open("a") as handle:
            handle.write(f"\n===== {title} =====\n")
