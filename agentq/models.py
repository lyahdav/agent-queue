from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


@dataclass(frozen=True)
class Project:
    project_id: str
    enabled: bool
    sheet_name: str
    repo_path: str
    default_branch: str
    agent: str
    verify_command: str
    poll_seconds: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Project":
        project_id = str(data.get("projectId") or "").strip()
        sheet_name = str(data.get("sheetName") or project_id).strip()
        poll_raw = data.get("pollSeconds") or 30
        try:
            poll_seconds = max(5, int(poll_raw))
        except (TypeError, ValueError):
            poll_seconds = 30
        return cls(
            project_id=project_id,
            enabled=_truthy(data.get("enabled")),
            sheet_name=sheet_name,
            repo_path=str(data.get("repoPath") or "").strip(),
            default_branch=str(data.get("defaultBranch") or "main").strip(),
            agent=str(data.get("agent") or "codex").strip().lower(),
            verify_command=str(data.get("verifyCommand") or "").strip(),
            poll_seconds=poll_seconds,
        )


@dataclass(frozen=True)
class Task:
    id: str
    status: str
    task: str
    commit_shas: str = ""
    redo_reason: str = ""
    claimed_from: str = ""
    resume: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data.get("id") or ""),
            status=str(data.get("status") or ""),
            task=str(data.get("task") or ""),
            commit_shas=str(data.get("commitShas") or ""),
            redo_reason=str(data.get("redoReason") or ""),
            claimed_from=str(data.get("claimedFrom") or data.get("status") or ""),
            resume=bool(data.get("resume")),
        )

    @property
    def original_status(self) -> str:
        return self.claimed_from or self.status
