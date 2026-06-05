from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import APP_DIR, STATE_FILE, ensure_app_dirs

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux path is covered
    fcntl = None


@contextmanager
def locked_file(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            yield handle
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateStore:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path

    def read(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return {"active": {}, "runs": {}}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {"active": {}, "runs": {}}
        data.setdefault("active", {})
        data.setdefault("runs", {})
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    def update_run(self, project_id: str, run_id: str, **fields: Any) -> None:
        with locked_file(self.path.with_suffix(".lock")):
            data = self.read()
            run = data["runs"].setdefault(run_id, {})
            run.update(fields)
            run.setdefault("projectId", project_id)
            run["updatedAt"] = int(time.time())
            data["active"][project_id] = run_id
            self._write_unlocked(data)

    def finish_run(self, project_id: str, run_id: str, **fields: Any) -> None:
        with locked_file(self.path.with_suffix(".lock")):
            data = self.read()
            run = data["runs"].setdefault(run_id, {})
            run.update(fields)
            run.setdefault("projectId", project_id)
            run["updatedAt"] = int(time.time())
            if data["active"].get(project_id) == run_id:
                data["active"].pop(project_id, None)
            self._write_unlocked(data)

    def active_run_for_project(self, project_id: str) -> dict[str, Any] | None:
        data = self.read()
        run_id = data.get("active", {}).get(project_id)
        if not run_id:
            return None
        run = data.get("runs", {}).get(run_id)
        if not run:
            return None
        return {"runId": run_id, **run}

    def run(self, run_id: str) -> dict[str, Any] | None:
        run = self.read().get("runs", {}).get(run_id)
        if not run:
            return None
        return {"runId": run_id, **run}


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    event = {"ts": int(time.time()), **event}
    with path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
