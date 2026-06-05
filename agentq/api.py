from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import web_app_url
from .models import Project, Task


class QueueError(RuntimeError):
    pass


class QueueClient:
    def __init__(self, url: str | None = None):
        self.url = url or web_app_url()

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.url}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return self._decode(response.read())
        except urllib.error.URLError as exc:
            raise QueueError(f"queue GET failed: {exc}") from exc

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return self._decode(response.read())
        except urllib.error.URLError as exc:
            raise QueueError(f"queue POST failed: {exc}") from exc

    def _decode(self, body: bytes) -> dict[str, Any]:
        text = body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            preview = text.replace("\n", " ")[:300]
            raise QueueError(f"queue returned non-JSON response: {preview}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise QueueError(str(data["error"]))
        if not isinstance(data, dict):
            raise QueueError("queue returned an unexpected JSON shape")
        return data

    def list_projects(self) -> list[Project]:
        data = self._get({"action": "projects"})
        return [Project.from_json(item) for item in data.get("projects", [])]

    def get_project(self, project_id: str) -> Project | None:
        for project in self.list_projects():
            if project.project_id == project_id:
                return project
        return None

    def claim(self, project_id: str, worker_id: str, resume_only: bool = False) -> Task | None:
        data = self._post(
            {
                "action": "claim",
                "projectId": project_id,
                "workerId": worker_id,
                "resumeOnly": resume_only,
            }
        )
        task = data.get("task")
        if not task:
            return None
        return Task.from_json(task)

    def update(
        self,
        project_id: str,
        task_id: str,
        status: str,
        sha: str = "",
        reason: str = "",
        last_error: str = "",
    ) -> None:
        data = self._post(
            {
                "action": "update",
                "projectId": project_id,
                "id": task_id,
                "status": status,
                "sha": sha,
                "reason": reason,
                "lastError": last_error,
            }
        )
        if data.get("success") is not True:
            raise QueueError(f"queue update failed for task {task_id}")

    def insert(self, project_id: str, tasks: list[dict[str, str]]) -> int:
        data = self._post({"action": "insert", "projectId": project_id, "tasks": tasks})
        if data.get("success") is not True:
            raise QueueError("queue insert failed")
        return int(data.get("inserted") or 0)
