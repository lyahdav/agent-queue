from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

from .api import QueueClient, QueueError
from .config import LOCKS_DIR, ensure_app_dirs
from .gitutils import (
    GitError,
    add_all,
    amend,
    changed_paths,
    commit,
    commit_sha,
    ensure_clean_on_branch,
    push,
    staged_diff,
)
from .logs import RunLog, attach_command, format_log_timestamp
from .models import Project, Task
from .process import run_args_to_file, run_args_to_logs, run_shell_to_logs
from .state import StateStore, locked_file


MAX_FIX_RETRIES = 10
DEFAULT_QUEUE_RETRY_SECONDS = 30
DRAIN_CHECK_SECONDS = 1
AGENT_FAILURE_OUTPUT_CHARS = 4000


def _read_toml_values(path: Path) -> dict[str, object]:
    try:
        if tomllib is not None:
            with path.open("rb") as config_file:
                return tomllib.load(config_file)
        values: dict[str, object] = {}
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if line.startswith("["):
                break
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.split("#", 1)[0].strip().strip('"').strip("'")
        return values
    except (OSError, ValueError):
        return {}


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _codex_runtime_settings(
    repo_path: str | Path, home: Path, environ: Mapping[str, str]
) -> tuple[str, str]:
    codex_home = Path(environ.get("CODEX_HOME", home / ".codex"))
    settings: dict[str, object] = {}
    for path in (codex_home / "config.toml", Path(repo_path) / ".codex" / "config.toml"):
        current = _read_toml_values(path)
        for key in ("model", "model_reasoning_effort"):
            if key in current:
                settings[key] = current[key]
    return str(settings.get("model") or "default"), str(
        settings.get("model_reasoning_effort") or "default"
    )


def _claude_runtime_settings(
    repo_path: str | Path, home: Path, environ: Mapping[str, str]
) -> tuple[str, str]:
    settings: dict[str, object] = {}
    configured_environment: dict[str, str] = {}
    paths = (
        home / ".claude" / "settings.json",
        Path(repo_path) / ".claude" / "settings.json",
        Path(repo_path) / ".claude" / "settings.local.json",
    )
    for path in paths:
        current = _read_json_object(path)
        for key in ("model", "effortLevel"):
            if key in current:
                settings[key] = current[key]
        current_environment = current.get("env")
        if isinstance(current_environment, dict):
            configured_environment.update(
                {str(key): str(value) for key, value in current_environment.items()}
            )

    model = environ.get("ANTHROPIC_MODEL") or configured_environment.get("ANTHROPIC_MODEL")
    reasoning = environ.get("CLAUDE_CODE_EFFORT_LEVEL") or configured_environment.get(
        "CLAUDE_CODE_EFFORT_LEVEL"
    )
    return str(model or settings.get("model") or "default"), str(
        reasoning or settings.get("effortLevel") or "default"
    )


def agent_runtime_description(
    project: Project,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    resolved_home = home or Path.home()
    resolved_environment = environ if environ is not None else os.environ
    if project.agent == "claude":
        model, reasoning = _claude_runtime_settings(
            project.repo_path, resolved_home, resolved_environment
        )
    else:
        model, reasoning = _codex_runtime_settings(
            project.repo_path, resolved_home, resolved_environment
        )
    return f"{project.agent} ({model}, {reasoning} reasoning)"


def worker_id(project_id: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{project_id}"


def format_elapsed(seconds: int) -> str:
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def elapsed_runtime(start: float) -> str:
    return format_elapsed(int(time.monotonic() - start))


def print_event(project_id: str, message: str) -> None:
    print(f"[{format_log_timestamp()}] {project_id}: {message}", flush=True)


def codex_base_args(repo: str, sandbox: str) -> list[str]:
    return [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--cd",
        repo,
        "--sandbox",
        sandbox,
        "--color",
        "never",
    ]


def claude_base_args(writable: bool) -> list[str]:
    mode = "bypassPermissions" if writable else "plan"
    return ["claude", "-p", "--output-format", "text", "--permission-mode", mode]


def implementation_guidance(project: Project) -> str:
    if not project.use_tdd:
        return (
            "TDD is not required for this project. Implement the requested change, "
            "and make sure the project builds or passes its configured verification."
        )
    return (
        "Use TDD for code changes: first add or update tests that capture the desired behavior, "
        "then change the code until those tests pass. If the task only changes documentation or "
        "other non-code artifacts, such as README.md, TDD is not required."
    )


def implementation_prompt(project: Project, task: Task) -> str:
    task_arg = task.task
    if task.resume:
        task_arg += (
            "\n\nRESUME CONTEXT:\n"
            "This task was started in a prior run that was interrupted before completion. "
            "Review the current repository state and continue from where it left off. "
            "Do not revert prior progress; fix forward."
        )
    elif task.original_status == "REDO":
        if task.commit_shas:
            task_arg += (
                "\n\nREDO CONTEXT:\n"
                f"Redo reason: {task.redo_reason}\n"
                f"Prior commit SHAs: {task.commit_shas}\n"
                "Review the prior commits and fix forward."
            )
        else:
            task_arg += (
                "\n\nAPPROVED PLAN CONTEXT:\n"
                "This task has an approved plan but no committed implementation yet.\n\n"
                f"Approved plan:\n{task.redo_reason}"
            )
    return (
        "Implement this queued task.\n\n"
        f"{implementation_guidance(project)}\n\n"
        "Treat the following text as the command arguments:\n\n"
        f"{task_arg}"
    )


def plan_prompt(project: Project, task: Task) -> str:
    return (
        "Create an implementation plan for this queued task.\n\n"
        f"Task:\n{task.task}\n\n"
        f"For later implementation: {implementation_guidance(project)}\n"
        f"Repository: {project.repo_path}\n\n"
        "You are in plan mode:\n"
        "- Do not edit files.\n"
        "- Do not run builds or tests.\n"
        "- Return only the plan text that should be stored for review."
    )


def commit_message_prompt(task: Task, diff: str) -> str:
    return (
        "Generate a git commit message for the following change.\n\n"
        f"Task:\n{task.task}\n\n"
        f"Diff:\n{diff}\n\n"
        "Rules:\n"
        "- First line: max 50 characters.\n"
        "- Add details after a blank line only if needed.\n"
        "- Output only the commit message.\n"
        "- Do not wrap the output in markdown."
    )


def fix_prompt(task: Task, verify_command: str, last_commit: str, failure_text: str, use_tdd: bool) -> str:
    if use_tdd:
        guidance = (
            "Use TDD for any new code behavior introduced while fixing this task. "
            "If the fix only changes documentation or other non-code artifacts, TDD is not required."
        )
    else:
        guidance = (
            "TDD is not required for this project's fix. Preserve the implementation and make sure "
            "the project builds or passes verification."
        )
    return (
        f"Original task being implemented:\n{task.task}\n\n"
        f"{guidance}\n\n"
        "The implementation has been committed but verification is failing. "
        "Fix the failures while preserving the task implementation. "
        "Do not revert prior work; fix forward. "
        f"Do not run `{verify_command}` yourself; the wrapper will rerun it.\n\n"
        f"Most recent commit:\n{last_commit}\n\n"
        f"Verification failure output:\n{failure_text}"
    )


def run_agent(project: Project, prompt: str, run_log: RunLog, phase_name: str, writable: bool = True) -> int:
    phase_log = run_log.phase_log(phase_name)
    run_log.append_output_header(phase_name)
    if project.agent == "claude":
        args = [*claude_base_args(writable), prompt]
    else:
        sandbox = "workspace-write" if writable else "read-only"
        args = [*codex_base_args(project.repo_path, sandbox), prompt]
    return run_args_to_logs(args, project.repo_path, phase_log, run_log.output_log)


def run_agent_to_file(project: Project, prompt: str, run_log: RunLog, phase_name: str, output_file: Path) -> int:
    phase_log = run_log.phase_log(phase_name)
    run_log.append_output_header(phase_name)
    if project.agent == "claude":
        args = [*claude_base_args(writable=False), prompt]
        return run_args_to_file(args, project.repo_path, phase_log, run_log.output_log, output_file)
    args = [
        *codex_base_args(project.repo_path, "read-only"),
        "--output-last-message",
        str(output_file),
        prompt,
    ]
    return run_args_to_logs(args, project.repo_path, phase_log, run_log.output_log)


def fail_task(
    client: QueueClient,
    project: Project,
    task: Task,
    run_log: RunLog,
    state: StateStore,
    reason: str,
    runtime: str,
) -> None:
    preview = reason.strip()[:5000]
    run_log.event("failed", preview)
    client.update(project.project_id, task.id, "FAILED", last_error=preview, runtime=runtime)
    state.finish_run(project.project_id, run_log.run_id, status="FAILED", lastError=preview, runtime=runtime)
    print_event(project.project_id, f"task {task.id} FAILED")


def verification_failure_text(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return text[-8000:]


def agent_failure_message(agent_name: str, code: int, log_path: Path) -> str:
    message = f"{agent_name} exited with code {code}"
    if not log_path.exists():
        return message
    output = log_path.read_text(errors="replace").strip()
    if not output:
        return message
    return f"{message}\n\nAgent output:\n{output[-AGENT_FAILURE_OUTPUT_CHARS:].lstrip()}"


class Worker:
    def __init__(self, client: QueueClient, state: StateStore | None = None):
        self.client = client
        self.state = state or StateStore()

    def run(self, project_id: str, forever: bool = False) -> int:
        ensure_app_dirs()
        lock_path = LOCKS_DIR / f"{project_id}.lock"
        with locked_file(lock_path):
            return self._run_locked(project_id, forever)

    def _run_locked(self, project_id: str, forever: bool) -> int:
        wid = worker_id(project_id)
        poll_seconds = DEFAULT_QUEUE_RETRY_SECONDS
        while True:
            if self.state.drain_requested():
                return 0
            try:
                project = self.client.get_project(project_id)
            except QueueError as exc:
                if not forever:
                    raise
                print_event(project_id, f"queue unavailable; retrying in {poll_seconds}s: {exc}")
                if self._sleep_until_next_poll(project_id, poll_seconds):
                    continue
                return 0
            if project is None:
                print_event(project_id, "project not found")
                return 1
            if not project.enabled:
                print_event(project_id, "disabled; worker idle")
                return 0
            poll_seconds = project.poll_seconds

            try:
                task = self._claim_when_safe(project, wid)
            except QueueError as exc:
                if not forever:
                    raise
                print_event(project_id, f"queue unavailable; retrying in {poll_seconds}s: {exc}")
                if self._sleep_until_next_poll(project_id, poll_seconds):
                    continue
                return 0
            if task is None:
                if not forever:
                    print_event(project_id, "no task claimed")
                    return 0
                if self._sleep_until_next_poll(project_id, poll_seconds):
                    continue
                return 0

            self.process_task(project, task)

            if self.state.drain_requested():
                return 0

            try:
                refreshed = self.client.get_project(project_id)
            except QueueError as exc:
                if not forever:
                    raise
                print_event(project_id, f"queue unavailable; retrying in {poll_seconds}s: {exc}")
                if self._sleep_until_next_poll(project_id, poll_seconds):
                    continue
                return 0
            if refreshed is None or not refreshed.enabled:
                print_event(project_id, "disabled after task; stopping")
                return 0
            if not forever:
                return 0

    def _claim_when_safe(self, project: Project, wid: str) -> Task | None:
        if self.state.drain_requested():
            return None

        resume_task = self.client.claim(project.project_id, wid, resume_only=True)
        if resume_task is not None:
            print_event(project.project_id, f"resuming task {resume_task.id}")
            return resume_task

        ok, reason = ensure_clean_on_branch(project.repo_path, project.default_branch)
        if not ok:
            print_event(project.project_id, f"skip claim: {reason}")
            return None

        refreshed = self.client.get_project(project.project_id)
        if refreshed is None or not refreshed.enabled:
            print_event(project.project_id, "disabled before claim")
            return None

        task = self.client.claim(project.project_id, wid)
        if task is not None:
            print_event(project.project_id, f"claimed task {task.id}")
        return task

    def _sleep_until_next_poll(self, project_id: str, poll_seconds: int) -> bool:
        slept = 0.0
        while slept < poll_seconds:
            if self.state.drain_requested():
                return False
            interval = min(DRAIN_CHECK_SECONDS, poll_seconds - slept)
            time.sleep(interval)
            slept += interval
        return not self.state.drain_requested()

    def process_task(self, project: Project, task: Task) -> None:
        run_log = RunLog(project, task)
        start = time.monotonic()
        self.state.update_run(
            project.project_id,
            run_log.run_id,
            status="RUNNING",
            taskId=task.id,
            task=task.task,
            runDir=str(run_log.run_dir),
            outputLog=str(run_log.output_log),
        )
        print_event(
            project.project_id,
            f"task {task.id} started with {agent_runtime_description(project)}; "
            f"attach: {attach_command(run_log.run_id, all_logs=True)}",
        )
        try:
            if task.original_status == "PLAN" or task.status == "PLAN IN PROGRESS":
                self._process_plan(project, task, run_log, start)
            else:
                self._process_implementation(project, task, run_log, start)
        except (GitError, QueueError, RuntimeError) as exc:
            fail_task(self.client, project, task, run_log, self.state, str(exc), elapsed_runtime(start))

    def _process_plan(self, project: Project, task: Task, run_log: RunLog, start: float) -> None:
        plan_file = run_log.run_dir / "plan.txt"
        self.state.update_run(project.project_id, run_log.run_id, status="PLANNING", currentLog=str(run_log.output_log))
        phase_log = run_log.phase_log("agent.log")
        code = run_agent_to_file(project, plan_prompt(project, task), run_log, "agent.log", plan_file)
        if code != 0:
            raise RuntimeError(agent_failure_message("plan agent", code, phase_log))
        plan_text = plan_file.read_text(errors="replace").strip() if plan_file.exists() else ""
        if not plan_text:
            plan_text = verification_failure_text(run_log.phase_log("agent.log"))
        if not plan_text:
            raise RuntimeError("plan agent produced no plan")
        runtime = elapsed_runtime(start)
        self.client.update(project.project_id, task.id, "PLAN REVIEW", reason=plan_text, runtime=runtime)
        self.state.finish_run(project.project_id, run_log.run_id, status="PLAN REVIEW", runtime=runtime)
        print_event(project.project_id, f"task {task.id} PLAN REVIEW")

    def _process_implementation(self, project: Project, task: Task, run_log: RunLog, start: float) -> None:
        self.state.update_run(project.project_id, run_log.run_id, status="AGENT", currentLog=str(run_log.output_log))
        phase_log = run_log.phase_log("agent.log")
        code = run_agent(project, implementation_prompt(project, task), run_log, "agent.log", writable=True)
        if code != 0:
            raise RuntimeError(agent_failure_message("implementation agent", code, phase_log))

        add_all(project.repo_path)
        diff = staged_diff(project.repo_path)
        if not diff.strip():
            raise RuntimeError("agent produced no staged diff")

        message_file = run_log.run_dir / "commit-message.txt"
        code = run_agent_to_file(project, commit_message_prompt(task, diff), run_log, "commit-message.log", message_file)
        message = message_file.read_text(errors="replace").strip() if code == 0 and message_file.exists() else ""
        if not message:
            message = task.task[:72]
        commit(project.repo_path, message)

        verify_log = run_log.phase_log("verify.log")
        self._verify_with_fix_loop(project, task, run_log, verify_log)

        push(project.repo_path)
        sha = commit_sha(project.repo_path)
        runtime = elapsed_runtime(start)
        self.client.update(project.project_id, task.id, "VERIFY", sha=sha, runtime=runtime)
        self.state.finish_run(project.project_id, run_log.run_id, status="VERIFY", commitSha=sha, runtime=runtime)
        print_event(project.project_id, f"task {task.id} VERIFY {sha}")

    def _verify_with_fix_loop(self, project: Project, task: Task, run_log: RunLog, verify_log: Path) -> None:
        if not project.verify_command:
            raise RuntimeError("verifyCommand is required for full-loop tasks")

        for attempt in range(0, MAX_FIX_RETRIES + 1):
            self.state.update_run(project.project_id, run_log.run_id, status="VERIFYING", currentLog=str(run_log.output_log))
            run_log.append_output_header("verify.log")
            code = run_shell_to_logs(project.verify_command, project.repo_path, verify_log, run_log.output_log)
            if code == 0:
                return
            if attempt >= MAX_FIX_RETRIES:
                raise RuntimeError(f"verification failed after {MAX_FIX_RETRIES} fix attempts")

            self.state.update_run(project.project_id, run_log.run_id, status=f"FIX {attempt + 1}", currentLog=str(run_log.output_log))
            last_commit = ""
            try:
                from .gitutils import git

                last_commit = git(project.repo_path, "show", "HEAD")
            except GitError:
                pass
            prompt = fix_prompt(
                task,
                project.verify_command,
                last_commit,
                verification_failure_text(verify_log),
                project.use_tdd,
            )
            fix_name = f"fix-{attempt + 1}.log"
            fix_code = run_agent(project, prompt, run_log, fix_name, writable=True)
            if fix_code != 0:
                raise RuntimeError(agent_failure_message("fix agent", fix_code, run_log.phase_log(fix_name)))
            add_all(project.repo_path)
            if not staged_diff(project.repo_path).strip():
                raise RuntimeError(f"fix attempt {attempt + 1} produced no diff")
            amend(project.repo_path)
