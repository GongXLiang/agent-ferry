"""Search and export sessions of delegated CLIs (adapted from upstream oil-delegate, MIT).

Only metadata is listed: no conversation text, no model calls, no network,
no writes. A data root may itself be a symlink or a Windows junction (people
move CLI data to another drive that way); it is resolved once. Links inside a
root are never followed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .common import DelegateError, emit, env_name, is_link_like, now_iso
from .runners import (
    RUNNER_BINARIES,
    SUPPORTED_RUNNERS,
    canonical_runner,
    detect_binary,
    preferred_resume,
    resume_commands,
)
from .storage import claude_project_dir_name

CLOUD_NAMES = {"iCloud Drive", "Screen Studio Archive", "Mobile Documents", "CloudStorage"}
DATALESS = 0x40000000  # macOS SF_DATALESS (cloud placeholder)


def _reject_unsafe(path: Path) -> None:
    for part in (path, *path.parents):
        if part.name in CLOUD_NAMES:
            raise DelegateError(f"Cloud session roots are excluded: {path}")
        if part.parent.name == "Library" and part.name in {"Mobile Documents", "CloudStorage"}:
            raise DelegateError(f"Cloud session roots are excluded: {path}")
    try:
        if getattr(path.lstat(), "st_flags", 0) & DATALESS:
            raise DelegateError(f"Cloud placeholder session paths are excluded: {path}")
    except FileNotFoundError:
        pass


def data_root(env_key: str, default: Path) -> Path:
    """A configured data root. It may be a link or junction; resolve it once."""
    raw = os.environ.get(env_key, "").strip() if env_key else ""
    root = Path(os.path.abspath(Path(raw or default).expanduser()))
    _reject_unsafe(root)
    if root.exists():
        resolved = Path(os.path.realpath(root))
        _reject_unsafe(resolved)
        return resolved
    return root


def local_entry(path: Path) -> Path:
    """An entry inside a resolved root: links, cloud names and placeholders are refused."""
    if path.name in CLOUD_NAMES:
        raise DelegateError(f"Cloud entries are excluded: {path}")
    if is_link_like(path):
        raise DelegateError(f"Links inside session roots are not followed: {path}")
    try:
        if getattr(path.lstat(), "st_flags", 0) & DATALESS:
            raise DelegateError(f"Cloud placeholder session paths are excluded: {path}")
    except FileNotFoundError:
        pass
    return path


def session_timestamp(value: Any, *, milliseconds: bool = False) -> str | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
    else:
        if milliseconds or abs(numeric) >= 100_000_000_000:
            numeric /= 1000
        stamp = dt.datetime.fromtimestamp(numeric, dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def session_summary(runner: str, sid: Any, title: Any, workspace: Any, updated: Any, *, milliseconds: bool = False) -> dict[str, Any]:
    if title is None:
        title = ""
    if not isinstance(sid, str) or not sid or not isinstance(title, str):
        raise DelegateError(f"Invalid {runner} session ID or title in metadata")
    if workspace is not None and not isinstance(workspace, str):
        raise DelegateError(f"Invalid {runner} workspace in metadata")
    return {
        "runner": runner,
        "session_id": sid,
        "title": title,
        "workspace": workspace,
        "updated_at": session_timestamp(updated, milliseconds=milliseconds),
        "source": "official" if runner == "kimi" else "local-store",
        "resumable": True,
    }


def session_children(root: Path) -> list[Path]:
    """One directory level only; unsafe entries are skipped before entering them."""
    if not root.exists():
        return []
    children = []
    for path in sorted(root.iterdir()):
        try:
            children.append(local_entry(path))
        except DelegateError:
            continue
    return children


def session_file_id(sid: str) -> str:
    if not sid or sid in {".", ".."} or any(char in sid for char in "/\\\x00"):
        raise DelegateError("Session ID must be a single filename component")
    return sid


# ---------------------------------------------------------------------------
# Claude


def claude_home() -> Path:
    return data_root("CLAUDE_CONFIG_DIR", Path.home() / ".claude")


def claude_desktop_default() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / "claude-code-sessions"
    return Path.home() / ".config" / "Claude" / "claude-code-sessions"


def claude_desktop_home() -> Path:
    return data_root(env_name("CLAUDE_DESKTOP_HOME"), claude_desktop_default())


def claude_transcript(sid: str, workspace: str) -> Path:
    return claude_home() / "projects" / claude_project_dir_name(workspace) / f"{session_file_id(sid)}.jsonl"


def claude_sessions(skipped: dict[str, int]) -> list[dict[str, Any]]:
    root = claude_desktop_home()
    latest: dict[str, dict[str, Any]] = {}
    for account in session_children(root):
        if not account.is_dir():
            continue
        for bucket in session_children(account):
            if not bucket.is_dir():
                continue
            for path in session_children(bucket):
                if not path.name.startswith("local_") or path.suffix != ".json" or not path.is_file():
                    continue
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(row, dict):
                        raise DelegateError("Invalid Claude desktop metadata")
                    if row.get("isArchived"):
                        continue
                    summary = session_summary("claude", row.get("cliSessionId"), row.get("title"), row.get("cwd"), row.get("lastActivityAt"))
                    session_file_id(summary["session_id"])
                    summary["source"] = "desktop-store"
                    transcript = claude_transcript(summary["session_id"], summary["workspace"]) if summary["workspace"] else None
                    summary["resumable"] = bool(transcript and transcript.is_file() and not is_link_like(transcript))
                except (DelegateError, ValueError, TypeError, OverflowError):
                    skipped["claude"] = skipped.get("claude", 0) + 1
                    continue
                sid = summary["session_id"]
                if sid not in latest or (summary["updated_at"] or "") > (latest[sid]["updated_at"] or ""):
                    latest[sid] = summary
    return list(latest.values())


# ---------------------------------------------------------------------------
# Kimi, Codex, Antigravity


def kimi_sessions(config: dict[str, Any], skipped: dict[str, int]) -> list[dict[str, Any]]:
    from .runners import make_launcher

    root = data_root("KIMI_CODE_HOME", Path.home() / ".kimi-code")
    binary = detect_binary("kimi", config)
    if not binary["installed"]:
        raise DelegateError(binary["error"])
    launcher = make_launcher(binary["path"])
    proc = subprocess.run(
        [*launcher.prefix, "session", "list", "--all", "--json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
        env={**os.environ, "KIMI_CODE_HOME": str(root)},
    )
    if proc.returncode:
        raise DelegateError(f"kimi session list exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    rows = json.loads(proc.stdout)
    if isinstance(rows, dict):
        rows = rows.get("sessions")
    if not isinstance(rows, list):
        raise DelegateError("kimi session list did not return a JSON session list")
    sessions = []
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise DelegateError("Invalid Kimi session metadata row")
            sessions.append(session_summary("kimi", row.get("id"), row.get("title"), row.get("workDir"), row.get("updatedAt"), milliseconds=True))
        except (DelegateError, ValueError, TypeError, OverflowError):
            skipped["kimi"] = skipped.get("kimi", 0) + 1
    return sessions


def codex_root() -> Path:
    return data_root("CODEX_HOME", Path.home() / ".codex")


def codex_sessions(skipped: dict[str, int]) -> list[dict[str, Any]]:
    index = codex_root() / "session_index.jsonl"
    if is_link_like(index):
        raise DelegateError(f"Symlinked session index is excluded: {index}")
    try:
        handle = index.open("r", encoding="utf-8")
    except FileNotFoundError:
        return []
    latest: dict[str, dict[str, Any]] = {}
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise DelegateError("Invalid Codex session metadata row")
                summary = session_summary("codex", row.get("id"), row.get("thread_name"), None, row.get("updated_at"))
            except (DelegateError, ValueError, TypeError, OverflowError):
                skipped["codex"] = skipped.get("codex", 0) + 1
                continue
            latest[summary["session_id"]] = summary  # renames append rows; the last valid one wins
    return list(latest.values())


def codex_session_workspaces(sessions: list[dict[str, Any]]) -> None:
    pending = {row["session_id"]: row for row in sessions if row["runner"] == "codex"}
    if not pending:
        return
    sessions_dir = codex_root() / "sessions"
    _reject_unsafe(sessions_dir)
    if not sessions_dir.exists():
        return
    # The sessions folder itself is often a junction/symlink to another drive: resolve it once.
    root = Path(os.path.realpath(sessions_dir))
    _reject_unsafe(root)

    def walk_error(exc: OSError) -> None:
        raise exc

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(name for name in dirs if name not in CLOUD_NAMES and not is_link_like(Path(directory) / name))
        for name in sorted(files):
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            sid = next((sid for sid in pending if name.endswith(f"-{sid}.jsonl")), None)
            path = Path(directory) / name
            if sid is None or is_link_like(path):
                continue
            with path.open("rb") as handle:  # read only the header line
                meta = json.loads(handle.readline())
            payload = meta.get("payload", {})
            if meta.get("type") != "session_meta" or payload.get("id", sid) != sid:
                raise DelegateError(f"Invalid Codex session_meta header: {path}")
            cwd = payload.get("cwd")
            if cwd is not None and not isinstance(cwd, str):
                raise DelegateError(f"Invalid Codex cwd in session_meta header: {path}")
            pending.pop(sid)["workspace"] = cwd
            if not pending:
                return


ANTIGRAVITY_STORES = (("ANTIGRAVITY_CLI_HOME", "antigravity-cli"), ("ANTIGRAVITY_HOME", "antigravity"))


def antigravity_sessions(errors: dict[str, str], skipped: dict[str, int]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    failures = []
    for suffix, dirname in ANTIGRAVITY_STORES:
        try:
            root = data_root(env_name(suffix), Path.home() / ".gemini" / dirname)
            path = root / "conversation_summaries.db"
            if not path.exists():
                continue
            local_entry(path)
            connection = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
            try:
                rows = connection.execute(
                    "SELECT conversation_id, title, workspace_uris, last_modified_time FROM conversation_summaries"
                )
                for sid, title, workspace_uris, modified in rows:
                    try:
                        uris = json.loads(workspace_uris) if workspace_uris else []
                        if not isinstance(uris, list) or any(not isinstance(uri, str) for uri in uris):
                            raise DelegateError("Invalid Antigravity workspace_uris JSON list")
                        workspace = None
                        for uri in uris:
                            parsed = urlparse(uri)
                            if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
                                workspace = unquote(parsed.path)
                                break
                        summary = session_summary("antigravity", sid, title, workspace, modified)
                        summary["source"] = "desktop-store" if dirname == "antigravity" else "local-store"
                        summary["resumable"] = dirname == "antigravity-cli"
                    except (DelegateError, ValueError, TypeError, OverflowError):
                        skipped["antigravity"] = skipped.get("antigravity", 0) + 1
                        continue
                    if sid not in latest or (summary["updated_at"] or "") > (latest[sid]["updated_at"] or ""):
                        latest[sid] = summary
            finally:
                connection.close()
        except (DelegateError, OSError, ValueError, TypeError, OverflowError, sqlite3.Error) as exc:
            failures.append(f"{dirname}: {exc}")
    if failures:
        errors["antigravity"] = "; ".join(failures)
    return list(latest.values())


def list_sessions(args: Any, config: dict[str, Any]) -> int:
    if args.limit < 1:
        raise DelegateError("--limit must be a positive integer")
    runners = [canonical_runner(args.runner)] if args.runner else list(SUPPORTED_RUNNERS)
    sessions: list[dict[str, Any]] = []
    unsupported = []
    errors: dict[str, str] = {}
    skipped: dict[str, int] = {}
    for runner in runners:
        try:
            if runner == "kimi":
                sessions.extend(kimi_sessions(config, skipped))
            elif runner == "codex":
                sessions.extend(codex_sessions(skipped))
            elif runner == "antigravity":
                sessions.extend(antigravity_sessions(errors, skipped))
            elif runner == "claude":
                sessions.extend(claude_sessions(skipped))
            else:
                unsupported.append(runner)
        except (DelegateError, OSError, ValueError, TypeError, AttributeError, OverflowError, subprocess.SubprocessError) as exc:
            errors[runner] = str(exc)
    query = (args.query or "").casefold()
    sessions = [row for row in sessions if query in row["title"].casefold()]
    sessions.sort(key=lambda row: (row["runner"], row["session_id"]))
    sessions.sort(key=lambda row: (bool(query) and row["title"].casefold() == query, row["updated_at"] or ""), reverse=True)
    sessions = sessions[: args.limit]
    try:
        codex_session_workspaces(sessions)
    except (DelegateError, OSError, ValueError, TypeError, AttributeError) as exc:
        errors["codex"] = str(exc)
    for row in sessions:
        runner = row["runner"]
        binary = detect_binary(runner, config)["path"] or RUNNER_BINARIES[runner][0]
        commands = resume_commands(runner, binary, row["session_id"]) if row["resumable"] else None
        row["resume_command"] = preferred_resume(commands)
        row["resume_commands"] = commands
    payload = {"status": "OK", "sessions": sessions, "unsupported": unsupported, "errors": errors, "skipped": skipped}
    if args.json:
        emit(payload)
    else:
        for row in sessions:
            print(f"{row['runner']}\t{row['session_id']}\t{row['title']}\t{row['workspace'] or ''}\t{row['updated_at'] or ''}")
        if unsupported:
            print(f"unsupported={','.join(unsupported)}")
        for runner, count in skipped.items():
            print(f"skipped.{runner}={count}")
        for runner, message in errors.items():
            sys.stderr.write(f"[ERROR] {runner}: {message}\n")
    return 0


# ---------------------------------------------------------------------------
# Export (Antigravity and Claude)


def protobuf_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode wire values, not printable substrings; no protobuf dependency."""
    if not isinstance(data, bytes):
        raise DelegateError("Expected an uncompressed protobuf step")
    position = 0

    def varint() -> int:
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise DelegateError("Truncated protobuf varint")
            byte = data[position]
            position += 1
            if shift == 63 and byte > 1:
                raise DelegateError("Protobuf varint exceeds 64 bits")
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise DelegateError("Invalid protobuf varint")

    fields = []
    while position < len(data):
        key = varint()
        number, wire = key >> 3, key & 7
        if not 0 < number < (1 << 29):
            raise DelegateError("Invalid protobuf field number")
        if wire == 0:
            value: Any = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else (8 if wire == 1 else 4)
            end = position + length
            if end > len(data):
                raise DelegateError("Truncated protobuf field")
            value = data[position:end]
            position = end
        else:
            raise DelegateError(f"Unsupported protobuf wire type: {wire}")
        fields.append((number, wire, value))
    return fields


def protobuf_value(fields: list[tuple[int, int, Any]], number: int, wire: int = 2) -> Any:
    return next((value for n, w, value in reversed(fields) if n == number and w == wire), None)


def conversation_text(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    if not isinstance(value, str) or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        return ""
    return value.strip()


def antigravity_export(sid: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: dict[str, str] = {}
    metadata = next((row for row in antigravity_sessions(errors, {}) if row["session_id"] == sid), None)
    stores = list(ANTIGRAVITY_STORES)
    if metadata and metadata["source"] == "desktop-store":
        stores.reverse()
    path = None
    for suffix, dirname in stores:
        try:
            root = data_root(env_name(suffix), Path.home() / ".gemini" / dirname)
            candidate = root / "conversations" / f"{sid}.db"
            if candidate.is_file():
                path = local_entry(candidate)
                break
        except (DelegateError, OSError):
            continue  # one unavailable store must not block the other
    if path is None:
        raise DelegateError("Antigravity conversation database not found in local stores")
    metadata = metadata or session_summary("antigravity", sid, "", None, None)
    turns = []
    connection = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
    try:
        for idx, kind, fmt, payload in connection.execute(
            "SELECT idx, step_type, step_format, step_payload FROM steps WHERE step_type IN (14, 15) ORDER BY idx"
        ):
            if fmt != 0:
                raise DelegateError(f"Unsupported Antigravity step_format {fmt} at step {idx}")
            fields = protobuf_fields(payload)
            if protobuf_value(fields, 1, 0) != kind:
                raise DelegateError(f"Antigravity step type mismatch at step {idx}")
            body = protobuf_value(fields, 19 if kind == 14 else 20)
            if body is None:
                raise DelegateError(f"Missing Antigravity message body at step {idx}")
            # 19.2 is user text; 20.1 is the response, not 20.3 (thinking) or 20.7 (tools).
            text = conversation_text(protobuf_value(protobuf_fields(body), 2 if kind == 14 else 1))
            if not text:
                continue
            stamp = None
            meta = protobuf_value(fields, 5)
            created = protobuf_value(protobuf_fields(meta), 1) if meta is not None else None
            if created is not None:
                time_fields = protobuf_fields(created)
                seconds = protobuf_value(time_fields, 1, 0) or 0
                nanos = protobuf_value(time_fields, 2, 0) or 0
                if not 0 <= nanos < 1_000_000_000:
                    raise DelegateError(f"Invalid Antigravity timestamp at step {idx}")
                stamp = session_timestamp(seconds + nanos / 1_000_000_000)
            turns.append({"role": "用户" if kind == 14 else "Agent", "text": text, "timestamp": stamp})
    finally:
        connection.close()
    return metadata, turns


def claude_export(sid: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata = next((row for row in claude_sessions({}) if row["session_id"] == sid), None)
    path = claude_transcript(sid, metadata["workspace"]) if metadata and metadata["workspace"] else None
    if path is None or not path.is_file():
        matches = []
        for project in session_children(claude_home() / "projects"):
            if not project.is_dir():
                continue
            candidate = project / f"{sid}.jsonl"
            if candidate.is_file() and not is_link_like(candidate):
                matches.append(candidate)
        if len(matches) != 1:
            raise DelegateError("Claude transcript not found or session ID is ambiguous across projects")
        path = matches[0]
    local_entry(path)
    metadata = metadata or session_summary("claude", sid, "", None, None)
    turns: dict[Any, dict[str, Any]] = {}
    custom_title = ai_title = ""
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise DelegateError(f"Invalid Claude JSONL at line {line_number}") from exc
            if not isinstance(row, dict):
                raise DelegateError(f"Invalid Claude record at line {line_number}")
            if row.get("type") == "custom-title" and isinstance(row.get("customTitle"), str):
                custom_title = row["customTitle"]
            if row.get("type") == "ai-title" and isinstance(row.get("aiTitle"), str):
                ai_title = row["aiTitle"]
            if not metadata["workspace"] and isinstance(row.get("cwd"), str):
                metadata["workspace"] = row["cwd"]
            kind, message = row.get("type"), row.get("message")
            if kind not in {"user", "assistant"} or not isinstance(message, dict):
                continue
            if row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):
                continue
            content = message.get("content")
            if isinstance(content, list):
                text = "\n\n".join(filter(None, (
                    conversation_text(block.get("text"))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )))
            else:
                text = conversation_text(content)
            if text:
                key = row.get("uuid") if isinstance(row.get("uuid"), str) else line_number
                turns[key] = {"role": "用户" if kind == "user" else "Agent", "text": text, "timestamp": session_timestamp(row.get("timestamp"))}
    if not metadata["title"]:
        metadata["title"] = custom_title or ai_title
    return metadata, list(turns.values())


def write_session_export(path: Path, markdown: str, force: bool) -> None:
    # An export must never write into a CLI's data store, even through a parent link.
    output = path.resolve()
    stores = [
        ("CLAUDE_CONFIG_DIR", Path.home() / ".claude"),
        (env_name("CLAUDE_DESKTOP_HOME"), claude_desktop_default()),
        (env_name("ANTIGRAVITY_CLI_HOME"), Path.home() / ".gemini" / "antigravity-cli"),
        (env_name("ANTIGRAVITY_HOME"), Path.home() / ".gemini" / "antigravity"),
        ("CODEX_HOME", Path.home() / ".codex"),
        ("KIMI_CODE_HOME", Path.home() / ".kimi-code"),
    ]
    for env, default in stores:
        root = Path(os.environ.get(env) or default).expanduser().resolve()
        if output == root or root in output.parents:
            raise DelegateError("Export output must be outside CLI data stores")
    if path.is_symlink():
        raise DelegateError("Export output cannot be a symlink")
    if not force:
        with path.open("x", encoding="utf-8") as handle:  # exclusive creation closes the overwrite race
            handle.write(markdown)
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def export_session(args: Any) -> int:
    runner = canonical_runner(args.runner)
    if runner not in {"antigravity", "claude"}:
        raise DelegateError(f"Session export is not supported for runner '{runner}'")
    if args.max_turns < 1:
        raise DelegateError("--max-turns must be a positive integer")
    sid = session_file_id(args.session_id)
    path = Path(args.out).expanduser().absolute()
    if path.exists() and not args.force:
        raise DelegateError("Export output already exists; use --force to overwrite")
    try:
        metadata, turns = antigravity_export(sid) if runner == "antigravity" else claude_export(sid)
        previous = ""
        ordered = []
        for index, turn in enumerate(turns):
            previous = turn["timestamp"] or previous  # undated records keep their place after the last dated one
            ordered.append((previous, index, turn))
        turns = [turn for _, _, turn in sorted(ordered, key=lambda item: item[:2])]
        omitted = max(0, len(turns) - args.max_turns)
        turns = turns[omitted:]
        title = metadata["title"] or sid
        title_line = " ".join(title.splitlines())
        workspace_line = json.dumps(metadata["workspace"], ensure_ascii=False)
        lines = [f"# {title_line}", ""]
        if omitted:
            lines += [f"> 已省略较早的 {omitted} 轮，保留最近 {len(turns)} 轮。", ""]
        updated = metadata["updated_at"] or next((turn["timestamp"] for turn in reversed(turns) if turn["timestamp"]), None)
        lines += [
            f"- 工作目录：{workspace_line}",
            f"- 会话时间：{updated or '未知'}",
            f"- 导出时间：{now_iso()}",
            f"- Runner：{runner}",
            f"- Session ID：{sid}",
            "",
        ]
        for turn in turns:
            suffix = f" · {turn['timestamp']}" if turn["timestamp"] else ""
            lines += [f"## {turn['role']}{suffix}", "", turn["text"], ""]
        write_session_export(path, "\n".join(lines), args.force)
    except (OSError, ValueError, TypeError, OverflowError, sqlite3.Error) as exc:
        raise DelegateError(f"Cannot export {runner} session: {exc}") from exc
    payload = {
        "status": "OK",
        "path": str(path),
        "title": title,
        "workspace": metadata["workspace"],
        "turns": len(turns),
        "omitted_turns": omitted,
        "runner": runner,
        "session_id": sid,
    }
    if args.json:
        emit(payload)
    else:
        print(f"path={path}\nturns={len(turns)}\nomitted_turns={omitted}")
    return 0
