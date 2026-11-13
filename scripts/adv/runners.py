"""Runner registry: names, executables, command lines and resume commands.

Adapter knowledge for the nine runners comes from upstream oil-delegate (MIT);
permission levels, launch safety on Windows and resume quoting are new here.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import IS_WINDOWS, DelegateError, env_name

SUPPORTED_RUNNERS = ("claude", "workbuddy", "antigravity", "codex", "opencode", "dsh", "grok", "kimi", "pi")
RUNNER_LABELS = {
    "claude": "Claude Code (claude/cc)",
    "workbuddy": "WorkBuddy (codebuddy/cbc/wb)",
    "antigravity": "Antigravity (agy/anti)",
    "codex": "Codex (codex)",
    "opencode": "OpenCode (opencode)",
    "dsh": "DeepSeek Harness (dsh)",
    "grok": "Grok Build (grok)",
    "kimi": "Kimi Code (kimi)",
    "pi": "Pi Agent (pi)",
}
RUNNER_BINARIES = {
    "claude": ("claude",),
    "workbuddy": ("codebuddy", "cbc"),
    "antigravity": ("agy",),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "dsh": ("dsh",),
    "grok": ("grok",),
    "kimi": ("kimi",),
    "pi": ("pi",),
}
RUNNER_ALIASES = {
    "cc": "claude",
    "claude-code": "claude",
    "wb": "workbuddy",
    "codebuddy": "workbuddy",
    "cbc": "workbuddy",
    "agy": "antigravity",
    "anti": "antigravity",
    "deepseek": "dsh",
    "deepseek-harness": "dsh",
    "grok-build": "grok",
    "kimi-code": "kimi",
    "pi-agent": "pi",
    "pi-coding-agent": "pi",
}
RESUME_TEMPLATES = {
    "claude": ("--resume", "{sid}"),
    "workbuddy": ("--resume", "{sid}"),
    "antigravity": ("--conversation", "{sid}"),
    "codex": ("resume", "{sid}"),
    "opencode": ("--session", "{sid}"),
    "dsh": ("tui", "--resume", "{sid}"),
    "grok": ("--resume", "{sid}"),
    "kimi": ("-S", "{sid}"),
    "pi": ("--session", "{sid}"),
}

MODES = ("read", "write", "full")
MODE_SUPPORT = {
    "codex": MODES,
    "claude": MODES,
    "opencode": MODES,
    "dsh": MODES,
    "workbuddy": ("read", "full"),
    "antigravity": ("read", "full"),
    "grok": ("read", "full"),
    "pi": ("read", "full"),
    "kimi": ("full",),
}
MODE_GAPS = {
    "kimi": "Kimi Code's -p mode always runs with auto permissions, so read-only or workspace-only cannot be guaranteed",
    "workbuddy": "no verified way to confine writes to the workspace",
    "antigravity": "no verified way to confine writes to the workspace",
    "grok": "no verified way to confine writes to the workspace",
    "pi": "Pi has no path confinement, so writes cannot be limited to the workspace",
}
MODE_DESCRIPTIONS = {
    "read": "read-only: inspect, search and report; do not change files",
    "write": "workspace-write: may change files inside the workspace only",
    "full": "FULL ACCESS: sandbox and approval checks are bypassed",
}
PROMPT_VIA_STDIN = {"codex", "dsh", "pi", "claude"}
MODEL_FLAGS = {"codex", "claude"}
EFFORT_FLAGS = {"codex", "claude"}
WEB_SEARCH_MODES = ("disabled", "cached", "indexed", "live")
ARGV_LIMIT_WINDOWS = 30000  # CreateProcess allows 32767 characters in total
ARGV_LIMIT_POSIX = 100000  # Linux limits a single argument to 128 KiB
BATCH_UNSAFE = re.compile(r'["%\r\n]')
_NPM_SHIM = re.compile(r'"%(?:dp0%|~dp0)\\(?P<rel>[^"]+?\.(?:js|cjs|mjs))"\s+%\*', re.IGNORECASE)
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._:/\\=@+-]+$")


def canonical_runner(value: str) -> str:
    runner = value.strip().lower()
    runner = RUNNER_ALIASES.get(runner, runner)
    if runner not in SUPPORTED_RUNNERS:
        raise DelegateError(f"Unsupported runner '{value}'. Choose from: {', '.join(SUPPORTED_RUNNERS)}")
    return runner


def parse_runner_list(raw: str) -> list[str]:
    result: list[str] = []
    for item in raw.replace(";", ",").replace(" ", ",").split(","):
        if item.strip():
            runner = canonical_runner(item)
            if runner not in result:
                result.append(runner)
    return result


def check_mode(runner: str, mode: str) -> None:
    if mode not in MODE_SUPPORT[runner]:
        supported = ", ".join(MODE_SUPPORT[runner])
        raise DelegateError(
            f"{runner} cannot run with --mode {mode}: {MODE_GAPS.get(runner, 'not supported')}. "
            f"Supported modes: {supported}."
        )


# ---------------------------------------------------------------------------
# Executables


@dataclass
class Launcher:
    path: str  # what the user recognises (the .cmd shim, the .exe, the script)
    prefix: list[str]  # argv prefix that actually starts it
    kind: str  # native | python-script | npm-shim | batch
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "argv_prefix": self.prefix, "note": self.note}


def _names(binary: str) -> list[str]:
    return [f"{binary}{ext}" for ext in (".exe", ".cmd", ".bat")] if IS_WINDOWS else [binary]


def fallback_dirs() -> list[Path]:
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".kimi-code" / "bin"]
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(Path(appdata) / "npm")
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    return dirs


def _usable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    return IS_WINDOWS or path.suffix.lower() == ".py" or os.access(path, os.X_OK)


def detect_binary(runner: str, config: dict[str, Any]) -> dict[str, Any]:
    key = env_name(f"{runner.upper()}_BIN")
    from_env = os.environ.get(key, "").strip()
    configured = from_env or (config.get("runner_paths") or {}).get(runner)
    if configured:
        path = Path(os.path.abspath(Path(configured).expanduser()))
        usable = _usable_file(path)
        return {
            "installed": usable,
            "path": str(path),
            "source": "environment" if from_env else "config",
            "error": None if usable else f"{key} or the configured path is not an executable file: {path}",
        }
    for binary in RUNNER_BINARIES[runner]:
        found = shutil.which(binary)
        if found and Path(found).is_file():
            return {"installed": True, "path": found, "source": "PATH", "error": None}
    for directory in fallback_dirs():
        for binary in RUNNER_BINARIES[runner]:
            for name in _names(binary):
                candidate = directory / name
                if _usable_file(candidate):
                    return {"installed": True, "path": str(candidate), "source": "known-path", "error": None}
    if runner == "workbuddy" and sys.platform == "darwin":
        candidate = Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy")
        if _usable_file(candidate):
            return {"installed": True, "path": str(candidate), "source": "workbuddy-app", "error": None}
    return {
        "installed": False,
        "path": None,
        "source": None,
        "error": f"Command not found: {' or '.join(RUNNER_BINARIES[runner])}",
    }


def npm_shim_target(path: Path) -> Path | None:
    """The JavaScript entry of an npm-generated .cmd shim, if that is what this is."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return None
    match = _NPM_SHIM.search(text)
    if not match:
        return None
    script = Path(os.path.normpath(path.parent / match.group("rel").replace("\\", os.sep)))
    return script if script.is_file() else None


def make_launcher(path_text: str) -> Launcher:
    path = Path(path_text)
    suffix = path.suffix.lower()
    if suffix == ".py":
        return Launcher(str(path), [sys.executable, str(path)], "python-script", "run with the current Python")
    if IS_WINDOWS and suffix in (".cmd", ".bat"):
        script = npm_shim_target(path)
        if script:
            local_node = path.parent / "node.exe"
            node = str(local_node) if local_node.is_file() else shutil.which("node")
            if node:
                return Launcher(str(path), [node, str(script)], "npm-shim", f"npm shim: starts node {script.name} directly, no cmd.exe")
        return Launcher(str(path), [str(path)], "batch", "batch file: runs through cmd.exe, so arguments are checked")
    return Launcher(str(path), [str(path)], "native")


def probe_version(launcher: Launcher) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [*launcher.prefix, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"version": None, "probe_error": str(exc)}
    output = (proc.stdout or proc.stderr or "").strip()
    return {
        "version": output.splitlines()[0][:200] if output else None,
        "probe_error": None if proc.returncode == 0 else f"--version exited {proc.returncode}",
    }


def probe_dsh_capabilities(launcher: Launcher) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [*launcher.prefix, "--profile", "headless", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"json": False, "resume": False, "permission_flag": False, "approval_flag": False, "probe_error": str(exc)}
    text = f"{proc.stdout}\n{proc.stderr}"
    return {
        "json": "--json" in text,
        "resume": "--session-id" in text,
        "permission_flag": "--permission-mode" in text,
        "approval_flag": "--approval-policy" in text,
        "probe_error": None if proc.returncode == 0 else f"headless --help exited {proc.returncode}",
    }


# ---------------------------------------------------------------------------
# Command lines


@dataclass
class RunOptions:
    model: str | None = None
    effort: str | None = None
    web_search: str | None = None


def build_invocation(
    runner: str,
    launcher: Launcher,
    workspace: Path,
    prompt: str,
    session_id: str | None,
    mode: str,
    options: RunOptions,
    dsh_capabilities: dict[str, Any] | None = None,
) -> tuple[list[str], str | None, dict[str, str]]:
    """Return (argv, stdin text or None, environment overrides)."""
    check_mode(runner, mode)
    if options.model and runner not in MODEL_FLAGS:
        raise DelegateError(f"--model is only wired for {', '.join(sorted(MODEL_FLAGS))}; {runner} keeps its own configured model")
    if options.effort and runner not in EFFORT_FLAGS:
        raise DelegateError(f"--effort is only wired for {', '.join(sorted(EFFORT_FLAGS))}; {runner} keeps its own setting")
    if options.web_search and runner != "codex":
        raise DelegateError("--web-search only applies to codex; other runners keep their own web settings")
    base = list(launcher.prefix)
    env: dict[str, str] = {}

    if runner == "codex":
        if session_id:
            permission = {
                "read": ["-c", "sandbox_mode=read-only", "-c", "approval_policy=never"],
                "write": ["-c", "sandbox_mode=workspace-write", "-c", "approval_policy=never"],
                "full": ["--dangerously-bypass-approvals-and-sandbox"],
            }[mode]
        else:
            permission = {
                "read": ["--sandbox", "read-only", "-c", "approval_policy=never"],
                "write": ["--sandbox", "workspace-write", "-c", "approval_policy=never"],
                "full": ["--dangerously-bypass-approvals-and-sandbox"],
            }[mode]
        extra: list[str] = []
        if options.model:
            extra += ["-m", options.model]
        if options.effort:
            extra += ["-c", f"model_reasoning_effort={options.effort}"]
        if options.web_search:
            extra += ["-c", f"web_search={options.web_search}"]
        if session_id:
            argv = [*base, "exec", "resume", "--json", "--skip-git-repo-check", *permission, *extra, session_id, "-"]
        else:
            argv = [*base, "exec", "--cd", str(workspace), "--json", "--skip-git-repo-check", *permission, *extra, "-"]
        return argv, prompt, env

    if runner == "claude":
        argv = [*base]
        if session_id:
            argv += ["--resume", session_id]
        argv += ["-p", "--output-format", "json"]
        argv += {
            "read": ["--permission-mode", "plan", "--permission-prompts", "none"],
            "write": ["--permission-mode", "acceptEdits", "--permission-prompts", "none"],
            "full": ["--dangerously-skip-permissions"],
        }[mode]
        if options.model:
            argv += ["--model", options.model]
        if options.effort:
            argv += ["--effort", options.effort]
        return argv, prompt, env  # claude -p reads the prompt from stdin when no prompt argument is given

    if runner == "workbuddy":
        argv = [*base]
        if session_id:
            argv += ["--resume", session_id]
        argv += ["-p", prompt, "--output-format", "json"]
        argv += ["--permission-mode", "plan", "-y"] if mode == "read" else ["--permission-mode", "bypassPermissions", "-y"]
        env["CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        if mode == "full":
            env["CODEBUDDY_IS_SANDBOX"] = "1"
        return argv, None, env

    if runner == "antigravity":
        argv = [*base]
        if session_id:
            argv += ["--conversation", session_id]
        argv += ["-p", prompt, "--output-format", "json"]
        argv += ["--mode", "plan"] if mode == "read" else ["--dangerously-skip-permissions"]
        return argv, None, env

    if runner == "grok":
        argv = [*base, "--cwd", str(workspace)]
        if session_id:
            argv += ["--resume", session_id]
        argv += ["-p", prompt, "--output-format", "json"]
        argv += ["--permission-mode", "plan"] if mode == "read" else ["--always-approve"]
        return argv, None, env

    if runner == "pi":
        argv = [*base, "--mode", "json"]
        if session_id:
            try:
                session_id = str(uuid.UUID(session_id))
            except ValueError as exc:
                raise DelegateError("Pi requires a session UUID") from exc
            argv += ["--session", session_id]
        if mode == "read":
            argv += ["--no-extensions", "--no-mcp", "--tools", "read,grep,find,ls"]
        return argv, prompt, env

    if runner == "kimi":
        argv = [*base]
        if session_id:
            argv += ["-S", session_id]
        argv += ["-p", prompt, "--output-format", "stream-json"]
        return argv, None, env

    if runner == "opencode":
        argv = [*base, "run", "--format", "json", "--dir", str(workspace)]
        if session_id:
            argv += ["--session", session_id]
        if mode == "read":
            argv += ["--agent", "plan"]
        argv.append(prompt)
        permission = {
            "*": "allow",
            "edit": "deny" if mode == "read" else "allow",
            "bash": "allow" if mode == "full" else "deny",
            "external_directory": "allow" if mode == "full" else "deny",
            "question": "deny",
        }
        env["OPENCODE_PERMISSION"] = json.dumps(permission, separators=(",", ":"))
        return argv, None, env

    if runner == "dsh":
        capabilities = dsh_capabilities or {}
        if not capabilities.get("json"):
            raise DelegateError("This DSH headless installation lacks --json; upgrade DSH to delegate to it")
        if session_id and not capabilities.get("resume"):
            raise DelegateError("This DSH headless installation lacks --session-id; upgrade DSH to resume sessions")
        argv = [*base, "--profile", "headless"]
        permission = {"read": "read-only", "write": "workspace-write", "full": "danger-full-access"}[mode]
        if capabilities.get("permission_flag"):
            argv += ["--permission-mode", permission]
        else:
            env["DSH_PERMISSION_MODE"] = permission
        if capabilities.get("approval_flag"):
            argv += ["--approval-policy", "never"]
        if session_id:
            argv += ["--session-id", session_id]
        argv += ["--json", "-"]
        return argv, prompt, env

    raise DelegateError(f"Unsupported runner: {runner}")


def check_launch_safety(launcher: Launcher, argv: list[str], prompt: str, stdin_text: str | None) -> None:
    """Refuse command lines that Windows batch files or argv limits would corrupt."""
    prompt_in_argv = stdin_text is None and prompt in argv
    if launcher.kind == "batch":
        if prompt_in_argv:
            raise DelegateError(
                f"{launcher.path} is a batch file that runs through cmd.exe, which would rewrite or cut the "
                "prompt (quotes, % and line breaks). Point config --set-bin at the real executable or the "
                "node script behind it."
            )
        unsafe = [arg for arg in argv[len(launcher.prefix):] if BATCH_UNSAFE.search(arg)]
        if unsafe:
            raise DelegateError(
                f"{launcher.path} runs through cmd.exe; these arguments contain quotes, % or line breaks "
                f"that cmd.exe would reinterpret: {unsafe[:3]}. Use paths without them or set the real executable."
            )
    if prompt_in_argv:
        if IS_WINDOWS and len(subprocess.list2cmdline(argv)) > ARGV_LIMIT_WINDOWS:
            raise DelegateError(
                "The prompt is too long for a Windows command line with this runner (it is passed as an argument). "
                "Shorten it and attach details with --file, or use codex/claude/dsh/pi, which read the prompt from stdin."
            )
        if not IS_WINDOWS and len(prompt.encode("utf-8")) > ARGV_LIMIT_POSIX:
            raise DelegateError("The prompt is too long to pass as one argument; shorten it and attach details with --file.")


def redact(argv: list[str], prompt: str) -> list[str]:
    return ["<prompt>" if part == prompt else part for part in argv]


# ---------------------------------------------------------------------------
# Resume commands for people


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ps_arg(text: str) -> str:
    return text if _SAFE_ARG.match(text) else _ps_quote(text)


def _cmd_arg(text: str) -> str:
    return text if _SAFE_ARG.match(text) else '"' + text.replace('"', '""') + '"'


def resume_commands(runner: str, binary_path: str | None, session_id: str | None) -> dict[str, str] | None:
    if not session_id:
        return None
    path = Path(binary_path) if binary_path else Path(RUNNER_BINARIES[runner][0])
    tail = [part.format(sid=session_id) for part in RESUME_TEMPLATES[runner]]
    short = path.stem if path.suffix.lower() in (".exe", ".cmd", ".bat") else path.name
    found = shutil.which(short) if binary_path else short
    on_path = bool(found) and (not binary_path or os.path.normcase(os.path.abspath(found)) == os.path.normcase(os.path.abspath(path)))
    if path.suffix.lower() == ".py":
        posix_head = [sys.executable, str(path)]
        ps_head = "& " + " ".join(_ps_quote(item) for item in posix_head)
        cmd_head = " ".join(_cmd_arg(item) for item in posix_head)
    elif on_path:
        posix_head, ps_head, cmd_head = [short], short, short
    else:
        posix_head = [str(path)]
        ps_head = "& " + _ps_quote(str(path))
        cmd_head = '"' + str(path) + '"'
    return {
        "posix": shlex.join([*posix_head, *tail]),
        "powershell": " ".join([ps_head, *(_ps_arg(part) for part in tail)]),
        "cmd": " ".join([cmd_head, *(_cmd_arg(part) for part in tail)]),
    }


def preferred_resume(commands: dict[str, str] | None) -> str | None:
    if not commands:
        return None
    return commands["powershell"] if IS_WINDOWS else commands["posix"]
