from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .api import QueueClient, QueueError
from .models import Project
from .runner import Worker, print_event
from .state import StateStore


def cmd_worker(args: argparse.Namespace) -> int:
    client = QueueClient()
    return Worker(client).run(args.project, forever=args.forever)


def _spawn_worker(project: Project) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "agentq", "worker", "--project", project.project_id, "--forever"],
        text=True,
    )


def cmd_watch(args: argparse.Namespace) -> int:
    client = QueueClient()
    workers: dict[str, subprocess.Popen[str]] = {}
    print("agentq watch started", flush=True)
    try:
        while True:
            projects = {project.project_id: project for project in client.list_projects()}
            for project_id, proc in list(workers.items()):
                if proc.poll() is not None:
                    workers.pop(project_id, None)
                    print_event(project_id, f"worker exited with code {proc.returncode}")
            for project in projects.values():
                if not project.enabled:
                    continue
                if project.project_id not in workers:
                    workers[project.project_id] = _spawn_worker(project)
                    print_event(project.project_id, "worker started")
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("agentq watch stopping", flush=True)
        for proc in workers.values():
            proc.terminate()
        for proc in workers.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        return 0
    except QueueError as exc:
        print(f"agentq watch error: {exc}", file=sys.stderr)
        return 1


def cmd_add(args: argparse.Namespace) -> int:
    entries: list[dict[str, str]] = []
    if args.task:
        entries.append({"task": args.task, "status": args.status})
    else:
        text = sys.stdin.read()
        for line in text.splitlines():
            task = line.strip()
            if task:
                entries.append({"task": task, "status": args.status})
    if not entries:
        print("No tasks provided. Use --task or stdin.", file=sys.stderr)
        return 1
    inserted = QueueClient().insert(args.project, entries)
    print(f"Inserted {inserted} task(s) into {args.project}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    client = QueueClient()
    state = StateStore()
    data = state.read()
    projects = client.list_projects()
    active = data.get("active", {})
    runs = data.get("runs", {})
    if not projects:
        print("No projects found.")
        return 0
    for project in projects:
        enabled = "enabled" if project.enabled else "disabled"
        run_id = active.get(project.project_id)
        if not run_id:
            print(f"{project.project_id}: {enabled}, idle")
            continue
        run = runs.get(run_id, {})
        task_id = run.get("taskId", "?")
        status = run.get("status", "RUNNING")
        output_log = run.get("outputLog", "")
        print(f"{project.project_id}: {enabled}, {status}, task {task_id}, run {run_id}")
        if output_log:
            print(f"  log: {output_log}")
            print(f"  attach: agentq attach --run {run_id}")
    return 0


def _tail_file(path: Path) -> int:
    if not path.exists():
        print(f"Log file does not exist yet: {path}", file=sys.stderr)
        return 1
    return subprocess.call(["tail", "-n", "80", "-f", str(path)])


def cmd_attach(args: argparse.Namespace) -> int:
    state = StateStore()
    run = None
    if args.project:
        run = state.active_run_for_project(args.project)
        if not run:
            print(f"No active run for project {args.project}", file=sys.stderr)
            return 1
    elif args.run:
        run = state.run(args.run)
        if not run:
            print(f"No run found with id {args.run}", file=sys.stderr)
            return 1
    else:
        print("Use --project or --run.", file=sys.stderr)
        return 1
    output_log = run.get("outputLog")
    if not output_log:
        print("Selected run has no output log.", file=sys.stderr)
        return 1
    return _tail_file(Path(output_log))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentq")
    sub = parser.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("watch", help="start one worker per enabled project")
    watch.add_argument("--poll-seconds", type=int, default=10)
    watch.set_defaults(func=cmd_watch)

    worker = sub.add_parser("worker", help="run one project's serial worker")
    worker.add_argument("--project", required=True)
    worker.add_argument("--forever", action="store_true")
    worker.set_defaults(func=cmd_worker)

    add = sub.add_parser("add", help="add tasks to a project tab")
    add.add_argument("--project", required=True)
    add.add_argument("--task")
    add.add_argument("--status", default="READY")
    add.set_defaults(func=cmd_add)

    status = sub.add_parser("status", help="show active projects and logs")
    status.set_defaults(func=cmd_status)

    attach = sub.add_parser("attach", help="follow one run's output log")
    group = attach.add_mutually_exclusive_group(required=True)
    group.add_argument("--project")
    group.add_argument("--run")
    attach.set_defaults(func=cmd_attach)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except QueueError as exc:
        print(f"queue error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
