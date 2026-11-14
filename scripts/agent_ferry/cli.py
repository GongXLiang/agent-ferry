"""Command line interface for agent-ferry."""
from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from . import audit as audit_mod
from . import launch, parsing, pyfind, runners, sessions, storage
from .common import (
    APP_NAME,
    EXIT_NEEDS_ONBOARDING,
    EXIT_NEEDS_ROLE_CHOICE,
    EXIT_NEEDS_RUNNER_CHOICE,
    EXIT_USE_HOST,
    CONTRACT_VERSION,
    DelegateError,
    atomic_write_json,
    atomic_write_text,
    emit,
    env_name,
    env_value,
    human_size,
    note,
    now_iso,
    read_json,
    setup_stdio,
    truncate,
)
from .config import (
    LOCATION_KINDS,
    is_onboarded,
    legacy_default_dir,
    load_config,
    read_legacy_config,
    save_config,
)

ROLE_MODES = {"review": "read", "critique": "read", "diagnose": "read", "implement": "write"}
ROLE_DESCRIPTIONS = {
    "review": "independent code review",
    "critique": "independent critique of a plan before implementation",
    "diagnose": "two independent root-cause analyses of a stuck problem",
    "implement": "implementing an approved plan",
}
ROLE_SLOTS = {"diagnose": 2}
HOST_AGENT = "host"
DEFAULT_RUN_TIMEOUT = 3600
VERIFY_CHECKLIST = [
    "Compare the reply with the original request and its sources; do not repeat claims you have not checked.",
    "Re-run the real verification commands yourself instead of trusting the reported results.",
    "Check the workspace for out-of-scope edits (audit.outside_workspace, git status/diff).",
    "Read the audit summary: failed or declined commands, errors, web pages actually used.",
]


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.home = storage.resolve_home(args.home)
        self._config: dict[str, Any] | None = None
        self._codex: tuple[dict[str, Any], str | None] | None = None

    @property
    def config(self) -> dict[str, Any]:
        if self._config is None:
            self._config = load_config(self.home.path)
        return self._config

    @property
    def codex(self) -> tuple[dict[str, Any], str | None]:
        if self._codex is None:
            self._codex = storage.read_codex_config()
        return self._codex

    def locations(self) -> storage.Locations:
        cli_values = {
            "records": self.args.records_dir,
            "cache": self.args.cache_dir,
            "workspaces": self.args.workspace_root,
        }
        return storage.resolve_locations(self.home, cli_values, self.config, self.codex[0])

    def save(self) -> None:
        save_config(self.home.path, self.config)


# ---------------------------------------------------------------------------
# Runner pool, roles and onboarding


def validate_installed(names: list[str], config: dict[str, Any]) -> None:
    missing = [name for name in names if not runners.detect_binary(name, config)["installed"]]
    if missing:
        raise DelegateError(
            f"Cannot enable missing or non-executable runners: {', '.join(missing)}. "
            "Install them or set an executable path with config --set-bin first."
        )


def apply_enabled_runners(config: dict[str, Any], raw: str) -> None:
    enabled = runners.parse_runner_list(raw)
    validate_installed(enabled, config)
    config["enabled_runners"] = enabled
    config["onboarded"] = bool(enabled)
    if config.get("default_runner") not in enabled:
        config["default_runner"] = None
    config["roles"] = {
        role: choices
        for role, choices in (config.get("roles") or {}).items()
        if all(choice == HOST_AGENT or choice in enabled for choice in choices)
    }


def apply_default_runner(config: dict[str, Any], raw: str) -> None:
    value = raw.strip().lower()
    if value in ("ask", "none", "null", ""):
        config["default_runner"] = None
        return
    runner = runners.canonical_runner(value)
    validate_installed([runner], config)
    enabled = list(config.get("enabled_runners") or [])
    if runner not in enabled:
        enabled.append(runner)
    config["enabled_runners"] = enabled
    config["default_runner"] = runner
    config["onboarded"] = True


def canonical_role(value: str) -> str:
    role = value.strip().lower()
    if role not in ROLE_MODES:
        raise DelegateError(f"Unknown role '{value}'. Choose from: {', '.join(ROLE_MODES)}")
    return role


def role_capable(runner: str, role: str) -> bool:
    if ROLE_MODES[role] == "read":
        return "read" in runners.MODE_SUPPORT[runner]
    return True


def apply_role(config: dict[str, Any], assignment: str) -> list[str]:
    """Save ROLE=<runner|host|ask>; diagnose takes two different choices. Returns warnings."""
    if "=" not in assignment:
        raise DelegateError("--set-role expects ROLE=<runner|host|ask>, e.g. review=codex")
    role_raw, value_raw = assignment.split("=", 1)
    role = canonical_role(role_raw)
    roles = dict(config.get("roles") or {})
    value = value_raw.strip().lower()
    if value in ("ask", "none", "null", ""):
        roles.pop(role, None)
        config["roles"] = roles
        return []
    slots = ROLE_SLOTS.get(role, 1)
    choices = [
        HOST_AGENT if item.strip() == HOST_AGENT else runners.canonical_runner(item)
        for item in value.replace(";", ",").split(",")
        if item.strip()
    ]
    if len(choices) != slots or len(set(choices)) != slots:
        need = f"{slots} different choices" if slots > 1 else "exactly one choice"
        raise DelegateError(f"Role '{role}' needs {need}; got: {value_raw.strip()}")
    chosen = [choice for choice in choices if choice != HOST_AGENT]
    blocked = [runner for runner in chosen if not role_capable(runner, role)]
    if blocked:
        raise DelegateError(
            f"Role '{role}' runs read-only, which {', '.join(blocked)} cannot guarantee; choose another runner or host"
        )
    validate_installed(chosen, config)
    if chosen:
        enabled = list(config.get("enabled_runners") or [])
        enabled.extend(runner for runner in chosen if runner not in enabled)
        config["enabled_runners"] = enabled
        config["onboarded"] = True
    roles[role] = choices
    config["roles"] = roles
    warnings = role_warnings(roles)
    if role == "implement":
        warnings += [
            f"{runner} has no workspace-limited write mode; implement runs with it need --mode full and the user's explicit consent"
            for runner in chosen
            if "write" not in runners.MODE_SUPPORT[runner]
        ]
    return warnings


def role_warnings(roles: dict[str, list[str]]) -> list[str]:
    warnings: list[str] = []
    implementer = (roles.get("implement") or [None])[0]
    if implementer and implementer != HOST_AGENT:
        for role in ("review", "critique"):
            if (roles.get(role) or [None])[0] == implementer:
                warnings.append(
                    f"'{role}' uses the same runner as 'implement' ({implementer}); an independent review works best with a different agent"
                )
    return warnings


def resolve_role(args: argparse.Namespace, config: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    role = canonical_role(args.role)
    role_mode = ROLE_MODES[role]
    if role_mode == "read":
        if args.mode and args.mode != "read":
            raise DelegateError(f"Role '{role}' always runs in read mode; drop --mode {args.mode}")
        args.mode = "read"
    else:
        if args.mode == "read":
            raise DelegateError(f"Role '{role}' changes files; use --mode write (default) or, with explicit consent, --mode full")
        args.mode = args.mode or role_mode
    slots = ROLE_SLOTS.get(role, 1)
    if not 1 <= args.role_slot <= slots:
        raise DelegateError(f"--role-slot for '{role}' must be between 1 and {slots}")
    args.resolved_role = role
    if not args.runner:
        choices = (config.get("roles") or {}).get(role) or []
        if len(choices) != slots:
            pool = (
                config.get("enabled_runners", [])
                if is_onboarded(config)
                else [name for name in runners.SUPPORTED_RUNNERS if runners.detect_binary(name, config)["installed"]]
            )
            candidates = [HOST_AGENT] + [name for name in pool if role_capable(name, role)]
            return EXIT_NEEDS_ROLE_CHOICE, {
                "status": "NEEDS_ROLE_CHOICE",
                "role": role,
                "role_description": ROLE_DESCRIPTIONS[role],
                "mode": args.mode,
                "slots": slots,
                "candidates": candidates,
                "candidate_modes": {name: list(runners.MODE_SUPPORT[name]) for name in candidates if name != HOST_AGENT},
                "message": (
                    f"Ask the user once which agent should handle this role ({HOST_AGENT} = the current agent's own "
                    f"fresh subagent). Save it with config --set-role {role}=<choice>, or pass --runner for this run only."
                ),
            }
        choice = choices[args.role_slot - 1]
        if choice == HOST_AGENT:
            return EXIT_USE_HOST, {
                "status": "USE_HOST",
                "role": role,
                "role_description": ROLE_DESCRIPTIONS[role],
                "mode": args.mode,
                "role_slot": args.role_slot,
                "message": (
                    "The user prefers the host agent for this role: run it in a fresh subagent of the current agent "
                    f"({args.mode} only) instead of an external CLI."
                ),
            }
        args.runner = choice
    if role_mode == "read" and not role_capable(runners.canonical_runner(args.runner), role):
        raise DelegateError(f"Role '{role}' runs read-only, which {args.runner} cannot guarantee")
    return None


def scan_runners(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    enabled = config.get("enabled_runners") or []
    result: dict[str, dict[str, Any]] = {}
    for name in runners.SUPPORTED_RUNNERS:
        info: dict[str, Any] = {
            "runner": name,
            "label": runners.RUNNER_LABELS[name],
            "aliases": sorted(alias for alias, target in runners.RUNNER_ALIASES.items() if target == name),
            **runners.detect_binary(name, config),
            "enabled": name in enabled,
            "is_default": name == config.get("default_runner"),
            "modes": list(runners.MODE_SUPPORT[name]),
            "prompt_delivery": "stdin" if name in runners.PROMPT_VIA_STDIN else "argument",
            "version": None,
            "probe_error": None,
        }
        if info["installed"]:
            launcher = runners.make_launcher(info["path"])
            info["launcher"] = launcher.kind
            info.update(runners.probe_version(launcher))
            if name == "dsh":
                info["capabilities"] = runners.probe_dsh_capabilities(launcher)
        result[name] = info
    return result


def onboarding_payload(ctx: Context, *, needs: list[str] | None = None) -> dict[str, Any]:
    config = ctx.config
    locations = ctx.locations()
    detected = scan_runners(config)
    needs = list(needs or [])
    if not needs:
        if not locations.confirmed:
            needs.append("locations")
        if not is_onboarded(config):
            needs.append("runners")
    legacy = legacy_default_dir()
    return {
        "status": "NEEDS_ONBOARDING" if needs else "ONBOARDED",
        "needs": needs,
        "onboarded": is_onboarded(config),
        "home": str(ctx.home.path),
        "enabled_runners": config.get("enabled_runners", []),
        "default_runner": config.get("default_runner"),
        "roles": config.get("roles", {}),
        "installed_runners": [name for name, info in detected.items() if info["installed"]],
        "detected_runners": detected,
        "locations": {
            "confirmed": locations.confirmed,
            "current": locations.as_dict(),
            "choices": storage.location_choices(ctx.home.path, ctx.codex[0]),
            "never_copied": "Delegated CLIs keep their own conversation logs; this skill stores only the task, the final reply and a compact result with pointers.",
        },
        "legacy": {"path": str(legacy), "found": (legacy / "config.json").is_file()},
        "python": {"executable": sys.executable, "version": ".".join(map(str, sys.version_info[:3])), "pinned": config.get("python")},
        "next_step_hint": (
            "Onboarding complete; delegate with run."
            if not needs
            else "Tell the user where files will go (locations.choices: recommended vs default) and which runners are "
            "installed (installed is not the same as logged in). After they answer, run: onboard --locations "
            "<recommended|default> --enable <runners> --default-runner <runner|ask>."
        ),
    }


def config_view(ctx: Context, warnings: list[str] | None = None) -> dict[str, Any]:
    config = ctx.config
    payload = {
        "status": "ONBOARDED" if is_onboarded(config) else "NEEDS_ONBOARDING",
        "onboarded": is_onboarded(config),
        "home": str(ctx.home.path),
        "enabled_runners": config.get("enabled_runners", []),
        "default_runner": config.get("default_runner"),
        "runner_paths": config.get("runner_paths", {}),
        "roles": config.get("roles", {}),
        "role_names": list(ROLE_MODES),
        "locations": ctx.locations().as_dict(),
        "python": config.get("python"),
        "keep_raw": bool(config.get("keep_raw")),
        "env_hints": bool(config.get("env_hints", True)),
        "retention": config.get("retention"),
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


def apply_locations_choice(ctx: Context, choice: str) -> None:
    choices = storage.location_choices(ctx.home.path, ctx.codex[0])
    picked = choices["recommended" if choice == "recommended" else "default"]
    ctx.config["locations"] = {kind: picked[kind]["path"] for kind in LOCATION_KINDS}
    ctx.config["locations_confirmed"] = True


def apply_legacy(ctx: Context, raw: str) -> list[str]:
    directory = legacy_default_dir() if raw == "__default__" else storage.expand(raw)
    legacy = read_legacy_config(directory)
    config = ctx.config
    notes: list[str] = []
    for name, path in (legacy.get("runner_paths") or {}).items():
        try:
            runner = runners.canonical_runner(str(name))
        except DelegateError:
            notes.append(f"skipped unknown runner path: {name}")
            continue
        if Path(str(path)).is_file():
            config.setdefault("runner_paths", {})[runner] = str(path)
        else:
            notes.append(f"skipped missing executable for {runner}: {path}")
    enabled = []
    for name in legacy.get("enabled_runners") or []:
        try:
            runner = runners.canonical_runner(str(name))
        except DelegateError:
            continue
        if runners.detect_binary(runner, config)["installed"]:
            enabled.append(runner)
        else:
            notes.append(f"not enabled because it is not installed: {runner}")
    if enabled:
        config["enabled_runners"] = list(dict.fromkeys([*(config.get("enabled_runners") or []), *enabled]))
        config["onboarded"] = True
    default = legacy.get("default_runner")
    if isinstance(default, str) and default in config.get("enabled_runners", []):
        config["default_runner"] = default
    for role, choices in (legacy.get("roles") or {}).items():
        if not isinstance(choices, list):
            continue
        try:
            notes.extend(apply_role(config, f"{role}={','.join(map(str, choices))}"))
        except DelegateError as exc:
            notes.append(f"skipped role {role}: {exc}")
    notes.append(f"imported settings from {directory}; the legacy folder was not modified")
    return notes


# ---------------------------------------------------------------------------
# Subcommands: onboard, config


def cmd_onboard(args: argparse.Namespace, ctx: Context) -> int:
    changed = any(value is not None for value in (args.enable, args.default_runner, args.locations, args.import_legacy))
    if args.scan and changed:
        raise DelegateError("--scan is read-only and cannot be combined with configuration changes")
    notes: list[str] = []
    if args.import_legacy is not None:
        notes += apply_legacy(ctx, args.import_legacy)
    if args.enable is not None:
        apply_enabled_runners(ctx.config, args.enable)
    if args.default_runner is not None:
        apply_default_runner(ctx.config, args.default_runner)
    if args.locations is not None:
        apply_locations_choice(ctx, args.locations)
    if changed:
        ctx.save()
    payload = onboarding_payload(ctx)
    if notes:
        payload["notes"] = notes
    emit(payload)
    return 0


def parse_assignment(value: str, flag: str) -> tuple[str, str]:
    if "=" not in value:
        raise DelegateError(f"{flag} expects NAME=VALUE")
    name, raw = value.split("=", 1)
    if not raw.strip():
        raise DelegateError(f"{flag} requires a non-empty value")
    return name.strip(), raw.strip()


def on_off(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("on", "true", "yes", "1"):
        return True
    if lowered in ("off", "false", "no", "0"):
        return False
    raise DelegateError(f"Expected on or off, got: {value}")


def cmd_config(args: argparse.Namespace, ctx: Context) -> int:
    config = ctx.config
    changed = any((
        args.set_enabled_runners is not None,
        args.set_default_runner is not None,
        bool(args.set_bin),
        bool(args.clear_bin),
        bool(args.set_role),
        bool(args.set_location),
        args.set_python is not None,
        args.keep_raw is not None,
        args.env_hints is not None,
        args.retention_days is not None,
        args.max_total_mb is not None,
        args.import_legacy is not None,
    ))
    warnings: list[str] = []
    if args.import_legacy is not None:
        warnings += apply_legacy(ctx, args.import_legacy)
    for assignment in args.set_bin or []:
        name, raw_path = parse_assignment(assignment, "--set-bin")
        runner = runners.canonical_runner(name)
        path = storage.expand(raw_path)
        if not path.is_file():
            raise DelegateError(f"Not an executable file: {path}")
        config.setdefault("runner_paths", {})[runner] = str(path)
    for name in args.clear_bin or []:
        (config.get("runner_paths") or {}).pop(runners.canonical_runner(name), None)
    if args.set_enabled_runners is not None:
        apply_enabled_runners(config, args.set_enabled_runners)
    if args.set_default_runner is not None:
        apply_default_runner(config, args.set_default_runner)
    role_notes: list[str] = []
    for assignment in args.set_role or []:
        role_notes = apply_role(config, assignment)  # the last call sees every saved role
    warnings += role_notes
    for assignment in args.set_location or []:
        kind, raw_path = parse_assignment(assignment, "--set-location")
        if kind not in LOCATION_KINDS:
            raise DelegateError(f"--set-location kind must be one of {', '.join(LOCATION_KINDS)}")
        locations = dict(config.get("locations") or {})
        if raw_path.lower() == "default":
            locations.pop(kind, None)
        else:
            path = storage.expand(raw_path)
            if not path.is_absolute():
                raise DelegateError(f"--set-location needs an absolute path: {raw_path}")
            locations[kind] = str(path)
        config["locations"] = locations
        config["locations_confirmed"] = True
    if args.set_python is not None:
        value = args.set_python.strip()
        if value.lower() == "clear":
            config["python"] = None
        else:
            target = sys.executable if value.lower() == "current" else str(storage.expand(value))
            candidate = pyfind.check_interpreter(target)
            config["python"] = target
            warnings.append(f"pinned Python {candidate.version} at {target}")
    if args.keep_raw is not None:
        config["keep_raw"] = on_off(args.keep_raw)
    if args.env_hints is not None:
        config["env_hints"] = on_off(args.env_hints)
    if args.retention_days is not None or args.max_total_mb is not None:
        retention = dict(config.get("retention") or {})
        if args.retention_days is not None:
            if args.retention_days < 1:
                raise DelegateError("--retention-days must be at least 1")
            retention["days"] = args.retention_days
        if args.max_total_mb is not None:
            if args.max_total_mb < 1:
                raise DelegateError("--max-total-mb must be at least 1")
            retention["max_total_mb"] = args.max_total_mb
        config["retention"] = retention
    if changed:
        ctx.save()
    emit(config_view(ctx, warnings))
    return 0


# ---------------------------------------------------------------------------
# run


def read_prompt(args: argparse.Namespace) -> str:
    given = [value for value in (args.prompt, args.prompt_file) if value is not None]
    if len(given) != 1:
        raise DelegateError("Give exactly one of --prompt TEXT, --prompt - (read stdin) or --prompt-file PATH")
    if args.prompt_file is not None:
        path = storage.expand(args.prompt_file)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DelegateError(f"--prompt-file must be UTF-8 text: {path}") from exc
        except OSError as exc:
            raise DelegateError(f"Cannot read --prompt-file {path}: {exc}") from exc
    elif args.prompt == "-":
        try:
            text = sys.stdin.read()
        except UnicodeDecodeError as exc:
            raise DelegateError("The prompt on stdin must be UTF-8; use --prompt-file with a UTF-8 file instead") from exc
    else:
        text = args.prompt
    if not text.strip():
        raise DelegateError("The prompt is empty")
    return text


def resolve_files(raw_files: list[str] | None, base: Path) -> list[str]:
    resolved: list[str] = []
    for raw in raw_files or []:
        path = Path(raw).expanduser()
        full = path.resolve() if path.is_absolute() else (base / path).resolve()
        if not full.is_file():
            raise DelegateError(f"Entry file does not exist: {full}")
        resolved.append(str(full))
    return resolved


def compose_prompt(prompt: str, workspace: Path, mode: str, files: list[str], *, isolated: bool = False, hints: dict[str, Any] | None = None) -> str:
    lines = [prompt.strip(), "", f"[{APP_NAME} runtime context]"]
    lines.append(f"Workspace: {workspace}" + (" (an empty folder created for this task)" if isolated else ""))
    lines.append(f"Permission: {runners.MODE_DESCRIPTIONS[mode]}")
    if files:
        lines.append("Entry files to inspect first: " + ", ".join(files))
    if mode == "read":
        lines.append("Read only: do not create, edit or delete files; avoid commands with side effects.")
    elif mode == "write":
        lines.append("Keep every change inside the workspace and within the requested scope.")
    else:
        lines.append("Full access was granted explicitly; still keep changes within the requested scope.")
    if hints:
        hint = f"Environment hints from the caller: Python {hints.get('python_version')} at {hints.get('python')}"
        if hints.get("conda_base"):
            hint += f"; conda base at {hints['conda_base']}"
        if hints.get("conda_active_env"):
            hint += f"; active conda environment {hints['conda_active_env']}"
        lines.append(hint + ". Check before assuming other interpreters exist.")
    lines.append(
        "Final report: summarize findings or changes, list the files involved, give the exact verification commands "
        "you ran and their results, cite sources for factual claims, and mark anything unverified or blocked."
    )
    return "\n".join(lines)


def resolve_workspace(args: argparse.Namespace, runner: str, locations: storage.Locations, *, create: bool) -> tuple[Path, bool]:
    if args.workspace:
        workspace = storage.expand(args.workspace)
        if not workspace.is_dir():
            raise DelegateError(f"Workspace is not a directory: {workspace}")
        return workspace, False
    if args.session:
        found = storage.workspace_for_session(locations.records.path, runner, args.session)
        if found and found[0].is_dir():
            return found
        raise DelegateError(
            "Pass --workspace: no earlier run of this session is recorded here, so its working directory is unknown "
            "(some runners, like DSH, must resume in the original directory)."
        )
    slug = storage.slugify(args.slug or "", f"{runner}-task")
    root = locations.workspaces.path
    if create:
        return storage.create_isolated_workspace(root, slug), True
    return storage.planned_isolated_workspace(root, slug), True


def needs_runner_choice(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "NEEDS_RUNNER_CHOICE",
        "enabled_runners": config.get("enabled_runners", []),
        "available_runners": {name: runners.detect_binary(name, config) for name in config.get("enabled_runners", [])},
        "message": "Choose a runner for this task (--runner) or save a default with config --set-default-runner.",
    }


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    config = ctx.config
    if args.role:
        pending = resolve_role(args, config)
        if pending:
            emit(pending[1])
            return pending[0]
    args.mode = args.mode or "read"
    if args.effort and not re.fullmatch(r"[a-z]+", args.effort):
        raise DelegateError("--effort must be a single lowercase word, e.g. low, medium, high")
    locations = ctx.locations()
    needs = []
    if not locations.confirmed and not args.dry_run:
        needs.append("locations")
    if not args.runner and not is_onboarded(config):
        needs.append("runners")
    if needs:
        payload = onboarding_payload(ctx, needs=needs)
        payload["how_to_onboard"] = "delegate onboard --locations <recommended|default> --enable <runners> --default-runner <runner|ask>"
        emit(payload)
        return EXIT_NEEDS_ONBOARDING
    if not args.runner and not config.get("default_runner"):
        emit(needs_runner_choice(config))
        return EXIT_NEEDS_RUNNER_CHOICE
    runner = runners.canonical_runner(args.runner or config["default_runner"])
    if is_onboarded(config) and runner not in config.get("enabled_runners", []):
        raise DelegateError(f"Runner '{runner}' is not enabled; enable it with onboard or config --set-enabled-runners")
    if args.timeout <= 0:
        raise DelegateError("--timeout must be greater than zero")
    runners.check_mode(runner, args.mode)
    prompt_text = read_prompt(args)
    binary = runners.detect_binary(runner, config)
    if not binary["installed"]:
        raise DelegateError(binary["error"] or f"Runner not installed: {runner}")
    launcher = runners.make_launcher(binary["path"])
    options = runners.RunOptions(model=args.model, effort=args.effort, web_search=args.web_search)
    hints = pyfind.environment_hints() if (config.get("env_hints", True) and not args.no_env_hints) else None
    capabilities = runners.probe_dsh_capabilities(launcher) if runner == "dsh" else None

    def prepare(workspace: Path, isolated: bool) -> tuple[str, list[str], str | None, dict[str, str]]:
        files = resolve_files(args.file, Path.cwd() if isolated else workspace)
        prompt = compose_prompt(prompt_text, workspace, args.mode, files, isolated=isolated, hints=hints)
        argv, stdin_text, env_updates = runners.build_invocation(
            runner, launcher, workspace, prompt, args.session, args.mode, options, capabilities
        )
        runners.check_launch_safety(launcher, argv, prompt, stdin_text)
        return prompt, argv, stdin_text, env_updates

    workspace, isolated = resolve_workspace(args, runner, locations, create=False)
    prompt, argv, stdin_text, env_updates = prepare(workspace, isolated)
    danger = args.mode == "full"
    role = getattr(args, "resolved_role", None)
    if args.dry_run:
        emit({
            "status": "DRY_RUN",
            "runner": runner,
            "role": role,
            "mode": args.mode,
            "permission": runners.MODE_DESCRIPTIONS[args.mode],
            "danger": danger,
            "workspace": {"path": str(workspace), "isolated": isolated, "would_create": isolated and not workspace.exists()},
            "session_id": args.session,
            "command": runners.redact(argv, prompt),
            "stdin": stdin_text is not None,
            "environment_overrides": env_updates,
            "launcher": launcher.as_dict(),
            "prompt_chars": len(prompt),
            "env_hints": bool(hints),
            "locations": locations.as_dict(),
        })
        return 0

    if isolated and not workspace.exists():
        # Validation used the planned path; create the folder only now that the run will start.
        workspace, _ = resolve_workspace(args, runner, locations, create=True)
        prompt, argv, stdin_text, env_updates = prepare(workspace, isolated)
    return execute_run(args, ctx, locations, runner, role, launcher, workspace, isolated, prompt, argv, stdin_text, env_updates, options, bool(hints))


def _gzip_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as reader, gzip.open(target, "wb") as writer:
        shutil.copyfileobj(reader, writer)


def execute_run(
    args: argparse.Namespace,
    ctx: Context,
    locations: storage.Locations,
    runner: str,
    role: str | None,
    launcher: runners.Launcher,
    workspace: Path,
    isolated: bool,
    prompt: str,
    argv: list[str],
    stdin_text: str | None,
    env_updates: dict[str, str],
    options: runners.RunOptions,
    hints_used: bool,
) -> int:
    config = ctx.config
    records = locations.records.path
    for _ in range(5):
        run_id = storage.new_run_id(runner)
        run_dir = records / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise DelegateError(f"Cannot create a run directory under {records}")
    inflight = locations.cache.path / storage.INFLIGHT_DIR / run_id
    prompt_path = run_dir / "prompt.md"
    reply_path = run_dir / "reply.md"
    result_path = run_dir / "result.json"
    atomic_write_text(prompt_path, prompt)
    safe_argv = runners.redact(argv, prompt)
    danger = args.mode == "full"
    started_at = now_iso()
    base = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "runner": runner,
        "role": role,
        "mode": args.mode,
        "permission": runners.MODE_DESCRIPTIONS[args.mode],
        "danger": danger,
        "workspace": {"path": str(workspace), "isolated": isolated},
        "run_dir": str(run_dir),
        "prompt_path": str(prompt_path),
        "reply_path": str(reply_path),
        "result_path": str(result_path),
        "command": safe_argv,
        "environment_overrides": env_updates,
        "launcher": launcher.as_dict(),
        "options": {"model": options.model, "effort": options.effort, "web_search": options.web_search, "env_hints": hints_used},
        "started_at": started_at,
    }
    atomic_write_json(result_path, {**base, "status": "running", "inflight_dir": str(inflight)})
    note(f"run {run_id} started; run_dir={run_dir}; workspace={workspace}{' (isolated)' if isolated else ''}")
    if danger:
        note("WARNING: FULL ACCESS - the delegated agent runs without sandbox or approvals (--mode full).")

    env = os.environ.copy()
    env.update(env_updates)
    started = time.monotonic()
    stdout_file = inflight / "stdout.log"
    stderr_file = inflight / "stderr.log"
    exit_code, timed_out = launch.run_captured(argv, workspace, env, stdin_text, stdout_file, stderr_file, args.timeout)
    elapsed = time.monotonic() - started
    raw_out = stdout_file.read_text(encoding="utf-8", errors="replace") if stdout_file.exists() else ""
    raw_err = stderr_file.read_text(encoding="utf-8", errors="replace") if stderr_file.exists() else ""
    parsed = parsing.parse_runner_output(runner, raw_out, raw_err, exit_code, args.session)
    if timed_out:
        parsed["status"] = "failed"
        if "Timeout" not in (parsed["error"] or ""):
            parsed["error"] = f"Timeout expired after {args.timeout}s\n{parsed['error'] or ''}".strip()
    report = audit_mod.audit_output(runner, raw_out, workspace)
    cli_session = storage.cli_session_file(runner, parsed["session_id"], workspace, storage.run_started(run_id))

    keep_raw = bool(args.keep_raw or config.get("keep_raw"))
    raw_paths: list[str] = []
    raw_reason = "not kept: the delegated CLI keeps its own session log (see cli_session_file)"
    if keep_raw or parsed["status"] != "completed":
        raw_reason = "kept (gzip) because --keep-raw/keep_raw is on" if keep_raw else "kept (gzip) because the run failed"
        for source, name in ((stdout_file, "stdout.log.gz"), (stderr_file, "stderr.log.gz")):
            if source.exists():
                target = run_dir / "raw" / name
                _gzip_copy(source, target)
                raw_paths.append(str(target))
    storage.remove_inflight(inflight, locations.cache.path)

    reply = f"## Reply from {runner} (self-reported, not yet verified by the caller)\n\n{parsed['response']}\n"
    if parsed["error"]:
        reply += f"\n## Error\n\n```text\n{parsed['error']}\n```\n"
    atomic_write_text(reply_path, reply)
    commands = runners.resume_commands(runner, launcher.path, parsed["session_id"])
    result = {
        **base,
        "status": parsed["status"],
        "session_id": parsed["session_id"],
        "response": parsed["response"],
        "error": parsed["error"],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stderr_tail": raw_err.strip()[-2000:] or None,
        "cli_session_file": cli_session,
        "raw_events": {"paths": raw_paths or None, "policy": raw_reason},
        "self_report": {"reply_path": str(reply_path), "chars": len(parsed["response"]), "excerpt": truncate(parsed["response"], 600)},
        "caller_verification": {"status": "pending", "checklist": VERIFY_CHECKLIST, "note": None, "checks": [], "recorded_at": None},
        "audit": report,
        "resume_commands": commands,
        "resume_command": runners.preferred_resume(commands),
        "finished_at": now_iso(),
        "elapsed_seconds": round(elapsed, 3),
    }
    atomic_write_json(result_path, result)
    return_code = 0 if parsed["status"] == "completed" else (exit_code if 0 < exit_code < 256 else 1)
    if args.json:
        emit(result)
        return return_code
    print(f"status={result['status']}")
    print(f"runner={runner}")
    print(f"mode={args.mode} ({runners.MODE_DESCRIPTIONS[args.mode]})")
    if danger:
        print("WARNING=FULL ACCESS was used for this run")
    print(f"session_id={parsed['session_id'] or ''}")
    print(f"run_dir={run_dir}")
    print(f"reply_path={reply_path}")
    print(f"result_path={result_path}")
    print(f"workspace={workspace}{' (isolated)' if isolated else ''}")
    print(f"cli_session_file={cli_session.get('path') or 'unknown: ' + str(cli_session.get('note'))}")
    print(f"raw_events={raw_reason}")
    print(f"audit={audit_mod.audit_line(report)}")
    if result["resume_command"]:
        print(f"resume_command={result['resume_command']}")
    if parsed["error"]:
        print(f"error={parsed['error'].splitlines()[0]}")
    print(f"elapsed={elapsed:.1f}s")
    return return_code


# ---------------------------------------------------------------------------
# show, verify, audit


def load_result(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "result.json"
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise DelegateError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DelegateError(f"Not a run result: {path}")
    return data


def cmd_show(args: argparse.Namespace, ctx: Context) -> int:
    run_dir = storage.find_run(ctx.locations().records.path, args.run)
    result = load_result(run_dir)
    summary = {
        "status": "OK",
        "run": {
            "run_id": result.get("run_id") or run_dir.name,
            "status": result.get("status"),
            "runner": result.get("runner"),
            "mode": result.get("mode"),
            "danger": result.get("danger"),
            "session_id": result.get("session_id"),
            "workspace": result.get("workspace"),
            "run_dir": str(run_dir),
            "reply_path": result.get("reply_path"),
            "cli_session_file": result.get("cli_session_file"),
            "caller_verification": (result.get("caller_verification") or {}).get("status"),
            "audit": audit_mod.audit_line(result.get("audit")),
            "resume_command": result.get("resume_command"),
            "error": result.get("error"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
        },
    }
    if args.json:
        emit(summary)
    else:
        for key, value in summary["run"].items():
            if isinstance(value, dict):
                value = value.get("path")
            print(f"{key}={'' if value is None else value}")
    return 0


def cmd_verify(args: argparse.Namespace, ctx: Context) -> int:
    run_dir = storage.find_run(ctx.locations().records.path, args.run)
    result = load_result(run_dir)
    result["caller_verification"] = {
        "status": args.status,
        "note": args.note,
        "checks": args.check or [],
        "checklist": VERIFY_CHECKLIST,
        "recorded_at": now_iso(),
    }
    atomic_write_json(run_dir / "result.json", result)
    payload = {"status": "OK", "run_id": run_dir.name, "caller_verification": result["caller_verification"]}
    if args.json:
        emit(payload)
    else:
        print(f"run_id={run_dir.name}\ncaller_verification={args.status}")
    return 0


def _read_events_file(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8", errors="replace")


def cmd_audit(args: argparse.Namespace, ctx: Context) -> int:
    target = Path(args.target).expanduser()
    if target.is_file():
        runner = runners.canonical_runner(args.runner or "codex")
        workspace = storage.expand(args.workspace) if args.workspace else None
        report = audit_mod.audit_output(runner, _read_events_file(target), workspace)
        source = str(target)
    else:
        run_dir = storage.find_run(ctx.locations().records.path, args.target)
        result = load_result(run_dir)
        runner = str(result.get("runner"))
        raw = run_dir / "raw" / "stdout.log.gz"
        workspace_info = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
        if raw.is_file():
            report = audit_mod.audit_output(runner, _read_events_file(raw), Path(workspace_info["path"]) if workspace_info.get("path") else None)
            source = f"{raw} (recomputed)"
        else:
            report = result.get("audit") or {"supported": False, "runner": runner, "reason": "no audit stored"}
            source = "result.json (raw events were not kept; the CLI's own session log is the full record)"
    payload = {"status": "OK", "source": source, "audit": report}
    if args.json:
        emit(payload)
    else:
        print(f"source={source}")
        print(f"audit={audit_mod.audit_line(report)}")
        for command in (report.get("commands") or [])[:50]:
            print(f"command[{command.get('status')}, exit {command.get('exit_code')}]={command.get('command')}")
        for change in (report.get("file_changes") or [])[:50]:
            flag = " OUTSIDE WORKSPACE" if change.get("outside_workspace") else ""
            print(f"file_change[{change.get('kind')}]={change.get('path')}{flag}")
        web = report.get("web") or {}
        for query in web.get("searches", [])[:50]:
            print(f"web_search={query}")
        for page in web.get("pages", [])[:50]:
            print(f"page_opened={page}")
        for error in report.get("errors", [])[:20]:
            print(f"error={error}")
    return 0


# ---------------------------------------------------------------------------
# locations, prune, doctor


def cmd_locations(args: argparse.Namespace, ctx: Context) -> int:
    report = storage.locations_report(ctx.locations())
    if args.json:
        emit({"status": "OK", **report})
        return 0
    print(f"confirmed={'yes' if report['confirmed'] else 'no (run onboard --locations recommended|default)'}")
    for kind in ("home", *LOCATION_KINDS):
        item = report[kind]
        detail = f" - {item['detail']}" if item.get("detail") else ""
        print(f"{kind}={item['path']} [{item['source']}{detail}]")
    records = report["records"]
    print(f"records.usage={records['runs']} runs, {records['size']}, oldest {records['oldest'] or '-'}, newest {records['newest'] or '-'}")
    print(f"cache.usage={report['cache']['files']} files, {report['cache']['size']}, {report['cache']['inflight_runs']} in flight")
    print(f"workspaces.usage={report['workspaces']['created_by_this_skill']} created by this skill, {report['workspaces']['size']}")
    codex_sessions = report["external"]["codex_sessions"]
    target = f" -> {codex_sessions['link_target']}" if codex_sessions.get("link_target") else ""
    print(f"external.codex_sessions={codex_sessions['path']}{target}")
    print(f"external.claude_projects={report['external']['claude_projects']['path']}")
    legacy = report["legacy"]
    if legacy["exists"]:
        print(f"legacy={legacy['path']} ({legacy['files']} files, {legacy['size']}; read-only, never modified)")
    return 0


def cmd_prune(args: argparse.Namespace, ctx: Context) -> int:
    locations = ctx.locations()
    retention = ctx.config.get("retention") or {}
    days = args.older_than if args.older_than is not None else int(retention.get("days", 30))
    max_mb = args.max_total_mb if args.max_total_mb is not None else int(retention.get("max_total_mb", 500))
    if days < 1 or max_mb < 1:
        raise DelegateError("--older-than and --max-total-mb must be at least 1")
    plan = storage.prune_plan(locations.records.path, locations.cache.path, days, max_mb)
    if not args.apply:
        payload = {
            "status": "DRY_RUN",
            **plan,
            "how_to_apply": "Show this list (paths, count, size, reasons) to the user. Only after an explicit yes, run the same command with --apply.",
        }
    else:
        deleted, failed = [], []
        roots = [locations.records.path, locations.cache.path / storage.INFLIGHT_DIR]
        for candidate in plan["candidates"]:
            try:
                storage.remove_run_tree(Path(candidate["path"]), roots)
                deleted.append(candidate)
            except (DelegateError, OSError) as exc:
                failed.append({**candidate, "error": str(exc)})
        payload = {
            "status": "APPLIED",
            "deleted": deleted,
            "failed": failed,
            "freed": human_size(sum(item["bytes"] for item in deleted)),
            "rules": plan["rules"],
            "never_deleted": plan["never_deleted"],
        }
    if args.json:
        emit(payload)
    else:
        print(f"status={payload['status']}")
        items = payload.get("candidates") if not args.apply else payload.get("deleted")
        for item in items or []:
            print(f"{'delete' if args.apply else 'would_delete'}={item['path']} ({item['files']} files, {item['size']}; {item['reason']})")
        for item in payload.get("failed", []):
            print(f"failed={item['path']}: {item['error']}")
        print(f"total={payload.get('size') or payload.get('freed')}")
        if not args.apply:
            print("next=show this list to the user; run again with --apply only after an explicit yes")
    return 0


def cmd_doctor(args: argparse.Namespace, ctx: Context) -> int:
    config = ctx.config
    conda = pyfind.conda_info()
    hint = [Path(conda["base"])] if conda.get("base") else None
    candidates = pyfind.discover(config, conda_base_hint=hint)
    accepted = [candidate for candidate in candidates if candidate.accepted]
    uv = shutil.which("uv")
    codex_config, codex_error = ctx.codex
    locations = ctx.locations()
    report = storage.locations_report(locations)
    warnings: list[str] = []
    if not accepted:
        warnings.append("No usable Python 3.10+ was found by the probe list; set " + env_name("PYTHON"))
    if any("Microsoft Store" in (candidate.reason or "") for candidate in candidates):
        warnings.append("A Microsoft Store placeholder python.exe is on PATH; it is skipped automatically")
    if not locations.confirmed:
        warnings.append("Storage locations are not confirmed yet; show them to the user and run onboard --locations")
    if codex_error:
        warnings.append(codex_error)
    runner_report = {}
    for name in runners.SUPPORTED_RUNNERS:
        info = runners.detect_binary(name, config)
        entry: dict[str, Any] = {**info, "modes": list(runners.MODE_SUPPORT[name])}
        if info["installed"]:
            launcher = runners.make_launcher(info["path"])
            entry["launcher"] = launcher.as_dict()
            entry.update(runners.probe_version(launcher))
        runner_report[name] = entry
    payload = {
        "status": "OK",
        "version": __version__,
        "platform": sys.platform,
        "python": {
            "running": {"executable": sys.executable, "version": ".".join(map(str, sys.version_info[:3]))},
            "bootstrap_source": env_value("PYTHON_SOURCE"),
            "override": env_value("PYTHON"),
            "pinned": config.get("python"),
            "selected": accepted[0].as_dict() if accepted else None,
            "candidates": [candidate.as_dict() for candidate in candidates],
        },
        "package_managers": {
            "conda": conda,
            "uv": {"executable": uv},
            "scoop_roots": [str(root) for root, _ in pyfind._scoop_roots()] if os.name == "nt" else [],
        },
        "runners": runner_report,
        "locations": report,
        "hosts": {
            "codex": {
                "home": str(storage.codex_home()),
                "config": str(storage.codex_home() / "config.toml"),
                "config_error": codex_error,
                "settings": storage.codex_settings(codex_config),
                "note": "this skill does not override model, provider, effort or web_search unless asked",
            },
            "claude": {
                "config_dir": str(storage.claude_config_home()),
                "CLAUDE_CODE_TMPDIR": os.environ.get("CLAUDE_CODE_TMPDIR"),
            },
        },
        "warnings": warnings,
    }
    if args.json:
        emit(payload)
        return 0
    running = payload["python"]["running"]
    print(f"python.running={running['executable']} ({running['version']})")
    if payload["python"]["selected"]:
        selected = payload["python"]["selected"]
        print(f"python.first_usable={' '.join(selected['command'])} ({selected['version']}, {selected['source']})")
    for candidate in candidates:
        mark = "ok" if candidate.accepted else "rejected"
        print(f"python.candidate[{mark}]={' '.join(candidate.command)} ({candidate.version or '-'}; {candidate.source}){'' if candidate.accepted else ' - ' + str(candidate.reason)}")
    if conda.get("executable"):
        print(f"conda={conda.get('executable')} base={conda.get('base')} envs={len(conda.get('envs') or [])}")
    print(f"uv={uv or 'not found'}")
    for name, entry in runner_report.items():
        if entry["installed"]:
            print(f"runner.{name}={entry['path']} [{entry['launcher']['kind']}] {entry.get('version') or entry.get('probe_error') or ''} modes={','.join(entry['modes'])}")
        else:
            print(f"runner.{name}=not found")
    for kind in ("home", *LOCATION_KINDS):
        item = report[kind]
        print(f"location.{kind}={item['path']} [{item['source']}]")
    settings = payload["hosts"]["codex"]["settings"]
    print("codex.settings=" + ", ".join(f"{key}={value}" for key, value in settings.items() if value is not None))
    for warning in warnings:
        print(f"warning={warning}")
    return 0


# ---------------------------------------------------------------------------
# Parser and entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delegate",
        description="Delegate one bounded task to an external CLI agent, then audit and verify the result.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--home", help="Folder holding config.json (default: platform data folder)")
    parser.add_argument("--records-dir", help="Override where run records are written")
    parser.add_argument("--cache-dir", help="Override where in-flight raw output is written")
    parser.add_argument("--workspace-root", help="Override where isolated workspaces are created")
    sub = parser.add_subparsers(dest="command", required=True)

    onboard = sub.add_parser("onboard", help="Scan runners and confirm locations (first use)")
    onboard.add_argument("--scan", action="store_true", help="Read-only scan")
    onboard.add_argument("--enable", help="Comma-separated runner names or aliases")
    onboard.add_argument("--default-runner", help="Default runner, or ask")
    onboard.add_argument("--locations", choices=("recommended", "default"), help="Confirm where files go")
    onboard.add_argument("--import-legacy", nargs="?", const="__default__", help="Import settings from ~/.oil-delegate (read-only)")

    config = sub.add_parser("config", help="Read or change configuration")
    config.add_argument("--set-enabled-runners")
    config.add_argument("--set-default-runner")
    config.add_argument("--set-bin", action="append", help="RUNNER=/path/to/executable")
    config.add_argument("--clear-bin", action="append", help="Runner whose configured path is cleared")
    config.add_argument("--set-role", action="append", help="ROLE=<runner|host|ask>; diagnose takes two choices")
    config.add_argument("--set-location", action="append", help="KIND=PATH|default; KIND is records, cache or workspaces")
    config.add_argument("--set-python", help="Pin the interpreter for the bootstrap scripts: PATH, current or clear")
    config.add_argument("--keep-raw", help="on|off: keep compressed raw event streams of successful runs")
    config.add_argument("--env-hints", help="on|off: add interpreter/conda paths to delegated prompts")
    config.add_argument("--retention-days", type=int, help="prune: delete runs older than N days")
    config.add_argument("--max-total-mb", type=int, help="prune: keep run records under N MB")
    config.add_argument("--import-legacy", nargs="?", const="__default__", help="Import settings from ~/.oil-delegate (read-only)")

    run = sub.add_parser("run", help="Run or resume one delegated task")
    run.add_argument("--prompt", help="Task text, or - to read it from stdin")
    run.add_argument("--prompt-file", help="UTF-8 file with the task text (avoids shell quoting)")
    run.add_argument("--runner")
    run.add_argument("--workspace", help="Project folder; omit for an isolated empty folder")
    run.add_argument("--slug", help="Name for the isolated workspace folder")
    run.add_argument("--mode", choices=runners.MODES, help="read (default), write, or full (explicit consent only)")
    run.add_argument("--role", help="review, critique, diagnose or implement")
    run.add_argument("--role-slot", type=int, default=1, help="Which saved choice to use for diagnose (1 or 2)")
    run.add_argument("--file", action="append", help="Entry file to point the agent at (repeatable)")
    run.add_argument("--session", help="Resume this session of the same runner")
    run.add_argument("--timeout", type=int, default=DEFAULT_RUN_TIMEOUT)
    run.add_argument("--model", help="Override the model (codex, claude) only when asked")
    run.add_argument("--effort", help="Override reasoning effort (codex, claude) only when asked")
    run.add_argument("--web-search", choices=runners.WEB_SEARCH_MODES, help="Codex web search mode for this run")
    run.add_argument("--keep-raw", action="store_true", help="Keep the compressed raw event stream")
    run.add_argument("--no-env-hints", action="store_true", help="Do not add interpreter/conda paths to the prompt")
    run.add_argument("--dry-run", action="store_true", help="Show the command without running or writing anything")
    run.add_argument("--json", action="store_true", help="Print one JSON object")

    show = sub.add_parser("show", help="Show one run (status, files, verification)")
    show.add_argument("run", nargs="?", default="latest", help="Run id, run folder or latest")
    show.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify", help="Record the caller's verification of a run (runs no checks)")
    verify.add_argument("run", nargs="?", default="latest", help="Run id, run folder or latest")
    verify.add_argument("--status", required=True, choices=("verified", "partial", "rejected"))
    verify.add_argument("--note", help="What was checked and what was not")
    verify.add_argument("--check", action="append", help="One check that was performed (repeatable)")
    verify.add_argument("--json", action="store_true")

    audit = sub.add_parser("audit", help="Audit summary for a run or an events file")
    audit.add_argument("target", nargs="?", default="latest", help="Run id, run folder, latest, or a JSONL events file")
    audit.add_argument("--runner", help="Runner of an events file (default codex)")
    audit.add_argument("--workspace", help="Workspace used to flag outside-workspace edits for an events file")
    audit.add_argument("--json", action="store_true")

    locations = sub.add_parser("locations", help="Where files go, their sources and sizes")
    locations.add_argument("--json", action="store_true")

    prune = sub.add_parser("prune", help="List (default) or delete old run records")
    prune.add_argument("--older-than", type=int, help="Days to keep (default from config, 30)")
    prune.add_argument("--max-total-mb", type=int, help="Size cap for records (default from config, 500)")
    prune.add_argument("--apply", action="store_true", help="Actually delete; only after the user confirmed the list")
    prune.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="Diagnose Python, conda/uv, runners, locations and host config")
    doctor.add_argument("--json", action="store_true")

    sessions_parser = sub.add_parser("sessions", help="Search local session titles without running an agent")
    sessions_parser.add_argument("--runner", help="Runner name or alias; defaults to all")
    sessions_parser.add_argument("--query", help="Case-insensitive title substring")
    sessions_parser.add_argument("--limit", type=int, default=20)
    sessions_parser.add_argument("--json", action="store_true")
    actions = sessions_parser.add_subparsers(dest="sessions_action")
    export = actions.add_parser("export", help="Export user/agent text of one session to Markdown")
    export.add_argument("--runner", required=True)
    export.add_argument("--session-id", required=True)
    export.add_argument("--out", required=True, help="Output Markdown file; parent folder must exist")
    export.add_argument("--max-turns", type=int, default=30)
    export.add_argument("--force", action="store_true")
    export.add_argument("--json", action="store_true")
    return parser


COMMANDS = {
    "onboard": cmd_onboard,
    "config": cmd_config,
    "run": cmd_run,
    "show": cmd_show,
    "verify": cmd_verify,
    "audit": cmd_audit,
    "locations": cmd_locations,
    "prune": cmd_prune,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = Context(args)
        if args.command == "sessions":
            if args.sessions_action == "export":
                return sessions.export_session(args)
            return sessions.list_sessions(args, ctx.config)
        return COMMANDS[args.command](args, ctx)
    except (DelegateError, OSError) as exc:
        message = str(exc) if isinstance(exc, DelegateError) else f"File system error: {exc}"
        if getattr(args, "json", False):
            emit({"status": "ERROR", "error": message})
        else:
            sys.stderr.write(f"[ERROR] {message}\n")
        return 1
