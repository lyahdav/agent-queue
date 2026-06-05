from __future__ import annotations

import os
import socket
import time
from pathlib import Path

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
from .logs import RunLog
from .models import Project, Task
from .process import run_args_to_logs, run_shell_to_logs
from .state import StateStore, locked_file


MAX_FIX_RETRIES = 10


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


def print_event(project_id: str, message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {project_id}: {message}", flush=True)


def read_command_file(repo: str, command_file: str) -> str:
    path = Path(repo) / command_file
    if not path.exists():
        raise RuntimeError(f"command file does not exist: {path}")
    return path.read_text()


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


def implementation_prompt(project: Project, task: Task) -> str:
    command_text = read_command_file(project.repo_path, project.command_file)
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
        f"Read {project.command_file} and follow it exactly.\n\n"
        "Command file contents:\n"
        f"{command_text}\n\n"
        "Treat the following text as the command arguments:\n\n"
        f"{task_arg}"
    )


def plan_prompt(project: Project, task: Task) -> str:
    return (
        "Create an implementation plan for this queued task.\n\n"
        f"Task:\n{task.task}\n\n"
        f"Target command file for later implementation: {project.command_file}\n"
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


def fix_prompt(task: Task, verify_command: str, last_commit: str, failure_text: str) -> str:
    return (
        f"Original task being implemented:\n{task.task}\n\n"
        "The implementation has been committed but verification is failing. "
        "Fix the failures while preserving the task implementation. "
        "Do not revert prior work; fix forward. "
        f"Do not run `{verify_command}` yourself; the wrapper will rerun it.\n\n"
        f"Most recent commit:\n{last_commit}\n\n"
        f"Verification failure output:\n{failure_text}"
    )


def run_codex(project: Project, prompt: str, run_log: RunLog, phase_name: str, sandbox: str) -> int:
    phase_log = run_log.phase_log(phase_name)
    run_log.append_output_header(phase_name)
    args = [*codex_base_args(project.repo_path, sandbox), prompt]
    return run_args_to_logs(args, project.repo_path, phase_log, run_log.output_log)


def run_codex_to_file(project: Project, prompt: str, run_log: RunLog, phase_name: str, output_file: Path) -> int:
    phase_log = run_log.phase_log(phase_name)
    run_log.append_output_header(phase_name)
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
) -> None:
    preview = reason.strip()[:5000]
    run_log.event("failed", preview)
    client.update(project.project_id, task.id, "FAILED", reason=preview, last_error=preview)
    state.finish_run(project.project_id, run_log.run_id, status="FAILED", lastError=preview)
    print_event(project.project_id, f"task {task.id} FAILED")


def verification_failure_text(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return text[-8000:]


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
        while True:
            project = self.client.get_project(project_id)
            if project is None:
                print_event(project_id, "project not found")
                return 1
            if not project.enabled:
                print_event(project_id, "disabled; worker idle")
                return 0

            task = self._claim_when_safe(project, wid)
            if task is None:
                if not forever:
                    print_event(project_id, "no task claimed")
                    return 0
                time.sleep(project.poll_seconds)
                continue

            self.process_task(project, task)

            refreshed = self.client.get_project(project_id)
            if refreshed is None or not refreshed.enabled:
                print_event(project_id, "disabled after task; stopping")
                return 0
            if not forever:
                return 0

    def _claim_when_safe(self, project: Project, wid: str) -> Task | None:
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
        print_event(project.project_id, f"task {task.id} started; attach: agentq attach --run {run_log.run_id}")
        try:
            if task.original_status == "PLAN" or task.status == "PLAN IN PROGRESS":
                self._process_plan(project, task, run_log)
            else:
                self._process_implementation(project, task, run_log, start)
        except (GitError, QueueError, RuntimeError) as exc:
            fail_task(self.client, project, task, run_log, self.state, str(exc))

    def _process_plan(self, project: Project, task: Task, run_log: RunLog) -> None:
        plan_file = run_log.run_dir / "plan.txt"
        self.state.update_run(project.project_id, run_log.run_id, status="PLANNING", currentLog=str(run_log.output_log))
        code = run_codex_to_file(project, plan_prompt(project, task), run_log, "agent.log", plan_file)
        if code != 0:
            raise RuntimeError(f"plan agent exited with code {code}")
        plan_text = plan_file.read_text(errors="replace").strip() if plan_file.exists() else ""
        if not plan_text:
            plan_text = verification_failure_text(run_log.phase_log("agent.log"))
        if not plan_text:
            raise RuntimeError("plan agent produced no plan")
        self.client.update(project.project_id, task.id, "PLAN REVIEW", reason=plan_text)
        self.state.finish_run(project.project_id, run_log.run_id, status="PLAN REVIEW")
        print_event(project.project_id, f"task {task.id} PLAN REVIEW")

    def _process_implementation(self, project: Project, task: Task, run_log: RunLog, start: float) -> None:
        self.state.update_run(project.project_id, run_log.run_id, status="AGENT", currentLog=str(run_log.output_log))
        code = run_codex(project, implementation_prompt(project, task), run_log, "agent.log", "workspace-write")
        if code != 0:
            raise RuntimeError(f"implementation agent exited with code {code}")

        add_all(project.repo_path)
        diff = staged_diff(project.repo_path)
        if not diff.strip():
            raise RuntimeError("agent produced no staged diff")

        message_file = run_log.run_dir / "commit-message.txt"
        code = run_codex_to_file(project, commit_message_prompt(task, diff), run_log, "commit-message.log", message_file)
        message = message_file.read_text(errors="replace").strip() if code == 0 and message_file.exists() else ""
        if not message:
            message = task.task[:72]
        commit(project.repo_path, message)

        verify_log = run_log.phase_log("verify.log")
        self._verify_with_fix_loop(project, task, run_log, verify_log)

        push(project.repo_path)
        sha = commit_sha(project.repo_path)
        elapsed = int(time.monotonic() - start)
        self.client.update(project.project_id, task.id, "VERIFY", sha=f"{sha} ({format_elapsed(elapsed)})")
        self.state.finish_run(project.project_id, run_log.run_id, status="VERIFY", commitSha=sha)
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
            prompt = fix_prompt(task, project.verify_command, last_commit, verification_failure_text(verify_log))
            fix_name = f"fix-{attempt + 1}.log"
            fix_code = run_codex(project, prompt, run_log, fix_name, "workspace-write")
            if fix_code != 0:
                raise RuntimeError(f"fix agent exited with code {fix_code}")
            add_all(project.repo_path)
            if not staged_diff(project.repo_path).strip():
                raise RuntimeError(f"fix attempt {attempt + 1} produced no diff")
            amend(project.repo_path)
