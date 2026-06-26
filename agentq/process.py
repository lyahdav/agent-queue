from __future__ import annotations

import subprocess
from pathlib import Path


def run_capture(args: list[str], cwd: str | Path) -> tuple[int, str, str]:
    proc = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_shell_to_logs(command: str, cwd: str | Path, phase_log: Path, output_log: Path) -> int:
    with phase_log.open("a") as phase, output_log.open("a") as combined:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            phase.write(line)
            phase.flush()
            combined.write(line)
            combined.flush()
        return proc.wait()


def run_args_to_file(
    args: list[str], cwd: str | Path, phase_log: Path, output_log: Path, output_file: Path
) -> int:
    proc = subprocess.run(args, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with phase_log.open("a") as phase, output_log.open("a") as combined:
        for stream in (proc.stdout, proc.stderr):
            if stream:
                phase.write(stream)
                combined.write(stream)
    Path(output_file).write_text(proc.stdout or "")
    return proc.returncode


def run_args_to_logs(args: list[str], cwd: str | Path, phase_log: Path, output_log: Path) -> int:
    with phase_log.open("a") as phase, output_log.open("a") as combined:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            phase.write(line)
            phase.flush()
            combined.write(line)
            combined.flush()
        return proc.wait()
