"""Shared constants and small helpers."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "agent-ferry"
ENV_PREFIX = "AGENT_FERRY_"
# Bump when result/status JSON fields change incompatibly.
CONTRACT_VERSION = 1
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ONBOARDING = 10
EXIT_NEEDS_RUNNER_CHOICE = 11
EXIT_NEEDS_ROLE_CHOICE = 12
EXIT_USE_HOST = 13
EXIT_TIMEOUT = 124
EXIT_SPAWN_FAILED = 127

IS_WINDOWS = os.name == "nt"


class DelegateError(Exception):
    """A user-facing error that should name the next step."""


def env_name(suffix: str) -> str:
    return ENV_PREFIX + suffix


def env_value(suffix: str) -> str | None:
    value = os.environ.get(env_name(suffix), "").strip()
    return value or None


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return now_utc().replace(microsecond=0).isoformat()


def setup_stdio() -> None:
    """Force UTF-8 so Chinese text and emoji never crash a GBK/cp1252 console or pipe."""
    for stream, errors in ((sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(encoding="utf-8", errors=errors)
            except (ValueError, OSError):
                pass
    if isinstance(sys.stdin, io.TextIOWrapper):
        try:
            sys.stdin.reconfigure(encoding="utf-8-sig")
        except (ValueError, OSError):
            pass


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps({"contract_version": CONTRACT_VERSION, **payload}, ensure_ascii=False, indent=2))


def note(message: str) -> None:
    sys.stderr.write(f"[{APP_NAME}] {message}\n")
    sys.stderr.flush()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def is_link_like(path: Path) -> bool:
    """True for symlinks and Windows junctions (Path.is_junction needs 3.12)."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    if IS_WINDOWS:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        tag = getattr(info, "st_reparse_tag", 0)
        return bool(tag) and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    return False


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
