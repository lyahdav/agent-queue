from __future__ import annotations

from pathlib import Path

from .process import run_capture


class GitError(RuntimeError):
    pass


def git(repo: str | Path, *args: str) -> str:
    code, stdout, stderr = run_capture(["git", *args], repo)
    if code != 0:
        raise GitError((stderr or stdout or f"git {' '.join(args)} failed").strip())
    return stdout


def current_branch(repo: str | Path) -> str:
    return git(repo, "branch", "--show-current").strip()


def status_short(repo: str | Path) -> str:
    return git(repo, "status", "--short").strip()


def ensure_clean_on_branch(repo: str | Path, default_branch: str) -> tuple[bool, str]:
    branch = current_branch(repo)
    if branch != default_branch:
        return False, f"current branch is {branch!r}, expected {default_branch!r}"
    dirty = status_short(repo)
    if dirty:
        return False, "repo has uncommitted changes"
    return True, ""


def staged_diff(repo: str | Path) -> str:
    return git(repo, "diff", "--cached")


def changed_paths(repo: str | Path) -> str:
    return git(repo, "diff", "--cached", "--name-only")


def commit_sha(repo: str | Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def add_all(repo: str | Path) -> None:
    git(repo, "add", "-A")


def commit(repo: str | Path, message: str) -> None:
    git(repo, "commit", "-m", message)


def amend(repo: str | Path) -> None:
    git(repo, "commit", "--amend", "--no-edit")


def push(repo: str | Path) -> None:
    git(repo, "push")
