"""Start a delegated CLI, stream its output to files, enforce a timeout, kill the whole tree."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .common import EXIT_SPAWN_FAILED, EXIT_TIMEOUT


def _popen_kwargs() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": flags} if flags else {}


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the process and everything it started."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def run_captured(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin_text: str | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
) -> tuple[int, bool]:
    """Return (exit code, timed out). Output goes straight to files, so nothing is lost on a crash."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                **_popen_kwargs(),
            )
        except OSError as exc:
            err.write(f"Failed to start {argv[0]}: {exc}\n".encode("utf-8"))
            return EXIT_SPAWN_FAILED, False
        data = stdin_text.encode("utf-8") if stdin_text is not None else None
        try:
            proc.communicate(input=data, timeout=timeout)
            return proc.returncode, False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            err.flush()
            err.write(f"\nTimeout expired after {timeout}s; the process tree was terminated.\n".encode("utf-8"))
            return EXIT_TIMEOUT, True
