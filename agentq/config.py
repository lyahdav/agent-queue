from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path.home() / ".agent-queue"
RUNS_DIR = APP_DIR / "runs"
LOCKS_DIR = APP_DIR / "locks"
STATE_FILE = APP_DIR / "state.json"
CONFIG_ENV = APP_DIR / "config.env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def web_app_url() -> str:
    _load_env_file(CONFIG_ENV)
    value = os.environ.get("AGENTQ_WEB_APP_URL") or os.environ.get("TODO_WEB_APP_URL")
    if not value:
        raise RuntimeError(
            "AGENTQ_WEB_APP_URL is not set. Put it in ~/.agent-queue/config.env "
            "or export it in the environment."
        )
    return value


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
