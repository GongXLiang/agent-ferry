"""Shared fixtures: a cross-platform fake CLI and a fully isolated environment.

Every test runs offline. Fake runners are Python files started with
sys.executable, so the same fixtures work on Windows, macOS and Linux. HOME,
USERPROFILE, APPDATA, LOCALAPPDATA, XDG_* and every CLI data root point into a
temporary folder, and PATH contains no real agent CLI.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SCRIPT_PATH = SCRIPTS / "delegate.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_ferry import audit, cli, common, config, pyfind, runners, sessions, storage, toml_compat  # noqa: E402,F401

RUNNERS = runners.SUPPORTED_RUNNERS
P = common.ENV_PREFIX
PI_SESSION = "11111111-2222-4333-8444-555555555555"
IS_WINDOWS = os.name == "nt"

FAKE_CLI = r'''# Fake {runner} CLI for offline tests.
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
runner = {runner!r}
args = sys.argv[1:]
if os.environ.get("FAKE_CALL_LOG"):
    with open(os.environ["FAKE_CALL_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"runner": runner, "argv": args}}) + "\n")
if "--version" in args:
    print("fake-" + runner + "-1.0")
    raise SystemExit(0)
if runner == "dsh" and "--help" in args:
    if os.environ.get("FAKE_DSH_LEGACY") == "1":
        print("--json --session-id")
    else:
        print("--json --session-id --permission-mode --approval-policy")
    raise SystemExit(0)
if runner == "kimi" and args == ["session", "list", "--all", "--json"]:
    if os.environ.get("FAKE_KIMI_EXIT"):
        sys.stderr.write("cannot list kimi sessions\n")
        raise SystemExit(int(os.environ["FAKE_KIMI_EXIT"]))
    if os.environ.get("FAKE_RECORD"):
        with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
            json.dump({{"argv": args, "kimi_home": os.environ.get("KIMI_CODE_HOME")}}, handle)
    fixture = os.environ.get("FAKE_KIMI_SESSIONS")
    if fixture:
        with open(fixture, encoding="utf-8") as handle:
            sys.stdout.write(handle.read())
    else:
        print("[]")
    raise SystemExit(0)

stdin_text = sys.stdin.buffer.read().decode("utf-8") if sys.stdin else ""  # the skill sends UTF-8; never trust the console code page
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({{
            "runner": runner,
            "argv": args,
            "stdin": stdin_text,
            "cwd": os.getcwd(),
            "permission": os.environ.get("OPENCODE_PERMISSION"),
            "dsh_mode": os.environ.get("DSH_PERMISSION_MODE"),
            "disable_background": os.environ.get("CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"),
            "codebuddy_sandbox": os.environ.get("CODEBUDDY_IS_SANDBOX"),
        }}, handle, ensure_ascii=False)
if os.environ.get("FAKE_SPAWN_CHILD"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_SPAWN_CHILD"], "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if os.environ.get("FAKE_FAIL") == "1":
    sys.stderr.write("fake failure\n")
    raise SystemExit(2)

session = None
for flag in ("--resume", "--conversation", "--session", "--session-id", "-S"):
    if flag in args:
        session = args[args.index(flag) + 1]
if runner == "codex" and "resume" in args:
    session = args[-2]
session = session or ({pi_session!r} if runner == "pi" else "fake-" + runner + "-session")
response = os.environ.get("FAKE_RESPONSE", "fake response")

def emit(value):
    print(json.dumps(value, ensure_ascii=False))

if os.environ.get("FAKE_DSH_BAD_FINAL") == "1":
    emit({{"type": "session", "sessionId": session}})
    emit({{"type": "final", "sessionId": session, "text": "", "reason": {{"kind": "error", "error": {{"message": "bad final"}}}}}})
    raise SystemExit(0)
if os.environ.get("FAKE_EMPTY") == "1":
    response = ""
if os.environ.get("FAKE_CODEX_FAIL_NO_DETAIL") == "1":
    emit({{"type": "thread.started", "thread_id": session}})
    emit({{"type": "item.completed", "item": {{"type": "agent_message", "text": "partial"}}}})
    emit({{"type": "turn.failed"}})
    raise SystemExit(0)

if runner in ("claude", "workbuddy"):
    result = {{"session_id": session, "result": response, "is_error": False}}
    if os.environ.get("FAKE_CLAUDE_DENIALS") == "1":
        result.update({{"num_turns": 3, "total_cost_usd": 0.01, "permission_denials": [
            {{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {{"command": "rm -rf build"}}}}]}})
    emit(result)
elif runner == "antigravity":
    emit({{"conversation_id": session, "status": "SUCCESS", "response": response}})
elif runner == "grok":
    emit({{"text": response, "stopReason": "end_turn", "sessionId": session, "requestId": "fake-request"}})
elif runner == "kimi":
    if response:
        emit({{"role": "assistant", "content": response}})
    emit({{"type": "session.resume_hint", "session_id": session}})
elif runner == "codex":
    emit({{"type": "thread.started", "thread_id": session}})
    emit({{"type": "turn.started"}})
    if os.environ.get("FAKE_CODEX_AUDIT") == "1":
        outside = os.path.join(os.path.dirname(os.getcwd()), "outside.txt")
        for event in [
            {{"type": "item.started", "item": {{"id": "c1", "type": "command_execution", "command": "pytest -q", "status": "in_progress"}}}},
            {{"type": "item.completed", "item": {{"id": "c1", "type": "command_execution", "command": "pytest -q", "aggregated_output": "1 passed", "exit_code": 0, "status": "completed"}}}},
            {{"type": "item.completed", "item": {{"id": "c2", "type": "command_execution", "command": "rm -rf build", "exit_code": None, "status": "declined"}}}},
            {{"type": "item.completed", "item": {{"id": "c3", "type": "command_execution", "command": "false", "exit_code": 1, "status": "failed"}}}},
            {{"type": "item.completed", "item": {{"id": "f1", "type": "file_change", "status": "completed", "changes": [{{"path": "src/app.py", "kind": "update"}}, {{"path": outside, "kind": "add"}}]}}}},
            {{"type": "item.started", "item": {{"id": "w1", "type": "web_search", "query": "", "action": {{"type": "search"}}}}}},
            {{"type": "item.completed", "item": {{"id": "w1", "type": "web_search", "query": "codex web search", "action": {{"type": "search", "queries": ["codex web search", "codex docs"]}}, "results": []}}}},
            {{"type": "item.completed", "item": {{"id": "w2", "type": "web_search", "query": "", "action": {{"type": "open_page", "url": "https://example.org/a"}}}}}},
            {{"type": "item.completed", "item": {{"id": "w3", "type": "web_search", "query": "", "action": {{"type": "find_in_page", "url": "https://example.org/a", "pattern": "price"}}}}}},
            {{"type": "item.completed", "item": {{"id": "m1", "type": "mcp_tool_call", "server": "docs", "tool": "search", "status": "failed", "error": {{"message": "timeout"}}}}}},
            {{"type": "item.completed", "item": {{"id": "e1", "type": "error", "message": "minor warning"}}}},
        ]:
            emit(event)
    if response:
        emit({{"type": "item.completed", "item": {{"id": "a1", "type": "agent_message", "text": response}}}})
    emit({{"type": "turn.completed", "usage": {{"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5}}}})
elif runner == "opencode":
    if response:
        emit({{"type": "text", "sessionID": session, "part": {{"text": response}}}})
    else:
        emit({{"type": "init", "sessionID": session}})
elif runner == "dsh":
    emit({{"type": "session", "sessionId": session}})
    if response:
        emit({{"type": "text", "text": response}})
    emit({{"type": "status", "phase": "turn_end", "reason": {{"kind": "completed"}}}})
    emit({{"type": "final", "text": response}})
elif runner == "pi":
    emit({{"type": "session", "id": session}})
    emit({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop", "content": [{{"type": "text", "text": response}}]}}}})
    emit({{"type": "agent_settled"}})
'''


def minimal_path() -> str:
    """System folders plus the test interpreter's own DLL folders (conda builds need them)."""
    python_dir = Path(sys.executable).parent
    extra = [str(path) for path in (python_dir, python_dir / "Library" / "bin") if path.is_dir()]
    if IS_WINDOWS:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        return os.pathsep.join([*extra, os.path.join(system_root, "System32"), system_root])
    return os.pathsep.join(["/usr/bin", "/bin"])


def write_fake_interpreter(directory: Path, name: str, version: str, *, fail: bool = False) -> Path:
    """A fake `python` that answers the probe with a fixed version (or fails)."""
    directory.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        path = directory / f"{name}.cmd"
        body = "@echo off\r\nexit /b 9009\r\n" if fail else f"@echo off\r\necho {version}\r\necho %~f0\r\n"
        path.write_text(body, encoding="ascii")
    else:
        path = directory / name
        body = "#!/bin/sh\nexit 9\n" if fail else f'#!/bin/sh\necho {version}\necho "$0"\n'
        path.write_text(body, encoding="ascii")
        path.chmod(0o755)
    return path


class CliTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ferry-test-")
        self.addCleanup(self.tmp.cleanup)  # registered first, so it runs after every other cleanup
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.state_dir = self.root / "state"
        self.records = self.root / "records"
        self.cache = self.root / "cache"
        self.workspaces = self.root / "workspaces"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.binaries = {runner: self.make_fake_cli(runner) for runner in RUNNERS}

    # -- environment ---------------------------------------------------------

    def make_fake_cli(self, runner: str, directory: Path | None = None) -> Path:
        path = (directory or self.bin_dir) / f"{runner}.py"
        path.write_text(FAKE_CLI.format(runner=runner, pi_session=PI_SESSION), encoding="utf-8")
        return path

    def base_env(self, *, locations: bool = True) -> dict[str, str]:
        drop = ("FAKE_", P, "CONDA", "MAMBA_", "CLAUDE_", "CODEX_", "KIMI_", "XDG_", "PYENV", "VIRTUAL_ENV", "UV_", "SCOOP")
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith(drop)}
        home = str(self.home)
        env.update({
            "HOME": home,
            "USERPROFILE": home,
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "CODEX_HOME": str(self.root / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-home"),
            "KIMI_CODE_HOME": str(self.root / "kimi-home"),
            P + "CLAUDE_DESKTOP_HOME": str(self.root / "claude-desktop"),
            P + "ANTIGRAVITY_CLI_HOME": str(self.root / "anti-cli-home"),
            P + "ANTIGRAVITY_HOME": str(self.root / "anti-home"),
            P + "HOME": str(self.state_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": minimal_path(),
        })
        env.pop("PYTHONIOENCODING", None)
        if locations:
            env[P + "RECORDS_DIR"] = str(self.records)
            env[P + "CACHE_DIR"] = str(self.cache)
            env[P + "WORKSPACE_ROOT"] = str(self.workspaces)
        for runner, path in self.binaries.items():
            env[f"{P}{runner.upper()}_BIN"] = str(path)
        return env

    @contextlib.contextmanager
    def patched_env(self, **overrides: str):
        env = {**self.base_env(), **overrides}
        with mock.patch.dict(os.environ, env, clear=True):
            yield

    def run_cli(
        self,
        *args: str,
        extra_env: dict[str, str] | None = None,
        remove_env: list[str] | None = None,
        input: str | None = None,
        locations: bool = True,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        env = self.base_env(locations=locations)
        for key in remove_env or []:
            env.pop(key, None)
        env.update(extra_env or {})
        kwargs: dict = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=timeout,
            **kwargs,
        )

    def call_main(self, *args: str, **env_overrides: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with self.patched_env(**env_overrides), contextlib.redirect_stdout(stdout):
            code = cli.main(list(args))
        return code, stdout.getvalue()

    # -- shortcuts -----------------------------------------------------------

    def onboard_all(self) -> None:
        result = self.run_cli("onboard", "--enable", ",".join(RUNNERS), "--default-runner", "ask")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def run_task(
        self,
        runner: str,
        mode: str | None = "read",
        session: str | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: str = "30",
        *extra: str,
    ) -> subprocess.CompletedProcess[str]:
        args = ["run", "--runner", runner, "--workspace", str(self.workspace), "--prompt", "inspect the fixture", "--timeout", timeout]
        if mode:
            args += ["--mode", mode]
        if session:
            args += ["--session", session]
        return self.run_cli(*args, *extra, extra_env=extra_env)

    def values(self, proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
        return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)

    def result_of(self, proc: subprocess.CompletedProcess[str]) -> dict:
        self.assertIn("result_path=", proc.stdout, msg=proc.stdout + proc.stderr)
        return json.loads(Path(self.values(proc)["result_path"]).read_text(encoding="utf-8"))

    def json_of(self, proc: subprocess.CompletedProcess[str]) -> dict:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"not JSON:\n{proc.stdout}\n{proc.stderr}")

    def record_of(self) -> dict:
        return json.loads((self.root / "record.json").read_text(encoding="utf-8"))

    @staticmethod
    def contains_run(sequence: list[str], flags: list[str]) -> bool:
        return any(sequence[index:index + len(flags)] == flags for index in range(len(sequence)))

    def make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable here: {exc}")

    def make_dir_link(self, link: Path, target: Path) -> str:
        """A Windows junction or a POSIX directory symlink; returns which one."""
        if IS_WINDOWS:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
            return "junction"
        self.make_symlink(link, target, directory=True)
        return "symlink"

    # -- session fixtures (from upstream) ------------------------------------

    def write_codex_index(self, rows: list) -> Path:
        path = self.root / "codex-home" / "session_index.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def write_rollout(self, sid: str, cwd: str, root: Path | None = None) -> Path:
        base = root or (self.root / "codex-home" / "sessions")
        path = base / "2026" / "11" / "03" / f"rollout-2026-11-03-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}
        # Invalid UTF-8 and JSON after the header must never be decoded or parsed.
        path.write_bytes(json.dumps(header).encode() + b"\n\xffnot conversation metadata\n")
        return path

    def write_anti_db(self, dirname: str, rows: list[tuple], *, wal: bool = False) -> sqlite3.Connection:
        path = self.root / dirname / "conversation_summaries.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, "
            "workspace_uris TEXT, last_modified_time, body TEXT DEFAULT 'private')"
        )
        connection.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, 'private')", rows)
        connection.commit()
        if wal:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return connection

    def sessions_payload(self, *args: str, extra_env: dict[str, str] | None = None) -> dict:
        proc = self.run_cli("sessions", *args, "--json", extra_env=extra_env)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["contract_version"], 1)
        self.assertEqual(payload["status"], "OK")
        self.assertIsInstance(payload["skipped"], dict)
        return payload

    @staticmethod
    def proto(*fields: tuple[int, object]) -> bytes:
        def varint(value: int) -> bytes:
            result = bytearray()
            while value > 127:
                result.append((value & 127) | 128)
                value >>= 7
            result.append(value)
            return bytes(result)

        result = bytearray()
        for number, value in fields:
            if isinstance(value, int):
                result += varint(number << 3) + varint(value)
            else:
                value = value.encode() if isinstance(value, str) else value
                result += varint((number << 3) | 2) + varint(len(value)) + value
        return bytes(result)

    def anti_step(self, kind: int, text: str | bytes, seconds: int) -> bytes:
        if kind == 14:
            body = self.proto((2, text), (3, self.proto((1, "duplicate user"))), (3, self.proto((2, b"\xff\x00image"))), (12, "private context"))
        else:
            body = self.proto((1, text), (3, "private thinking"), (8, text), (7, self.proto((1, "tool_name"), (3, '{"secret":"tool input"}'))))
        return self.proto((1, kind), (4, 3), (5, self.proto((1, self.proto((1, seconds), (2, 123000000))))), (19 if kind == 14 else 20, body), (200, "unknown field"))

    def write_anti_conversation(self, dirname: str, sid: str, steps: list[tuple]) -> Path:
        path = self.root / dirname / "conversations" / f"{sid}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_format INTEGER DEFAULT 0, step_payload BLOB)")
            connection.executemany("INSERT INTO steps(idx,step_type,step_payload) VALUES (?,?,?)", steps)
            connection.commit()
        return path

    def write_claude_metadata(self, sid: str, **overrides) -> Path:
        path = self.root / "claude-desktop" / "account" / "bucket" / f"local_{sid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"sessionId": f"desktop-{sid}", "cliSessionId": sid, "cwd": "/fixture/project", "title": "桌面标题", "lastActivityAt": 1793664000123, "isArchived": False, **overrides}
        path.write_text(json.dumps(row), encoding="utf-8")
        return path

    def write_claude_jsonl(self, sid: str, rows: list[dict], project: str = "-fixture-project") -> Path:
        path = self.root / "claude-home" / "projects" / project / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def export_payload(self, runner: str, sid: str, out: Path, *options: str) -> dict:
        result = self.run_cli("sessions", "export", "--runner", runner, "--session-id", sid, "--out", str(out), *options, "--json")
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        return json.loads(result.stdout)


def posix_tail(command: str, count: int) -> list[str]:
    return shlex.split(command)[-count:]
