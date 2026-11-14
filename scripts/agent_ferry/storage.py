"""Where the skill keeps files, how they are reported, found and pruned.

Four locations:
- home:       config.json only (fixed so the bootstrap scripts can read a pinned Python)
- records:    one directory per run (task, reply, result.json with the audit summary)
- cache:      in-flight raw output of running delegations; removed when a run ends
- workspaces: isolated empty directories for tasks that are not about a project

Resolution order for records/cache/workspaces: command line -> environment ->
config.json -> inferred from the host layout -> platform default.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import toml_compat
from .common import (
    APP_NAME,
    IS_WINDOWS,
    DelegateError,
    env_name,
    env_value,
    human_size,
    is_link_like,
    now_utc,
    read_json,
)
from .config import LOCATION_KINDS

RUN_ID_RE = re.compile(r"^(\d{8}T\d{6})-([a-z]+)-([0-9a-f]{6})$")
KIND_ENV = {"records": "RECORDS_DIR", "cache": "CACHE_DIR", "workspaces": "WORKSPACE_ROOT"}
KIND_FLAG = {"records": "--records-dir", "cache": "--cache-dir", "workspaces": "--workspace-root"}
KIND_PURPOSE = {
    "home": "config.json (a few KB)",
    "records": "run records: task, final reply, result.json with the audit summary",
    "cache": "raw output while a run is in flight; removed when the run ends",
    "workspaces": "isolated empty directories for non-project tasks",
}
SLUG_RE = re.compile(r"[^a-z0-9]+")
INFLIGHT_DIR = "inflight"


def expand(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(Path(raw).expanduser()))


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def is_within(child: Path, parent: Path) -> bool:
    child_norm = os.path.normcase(os.path.abspath(child))
    parent_norm = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return child_norm == parent_norm or child_norm.startswith(parent_norm + os.sep)


# ---------------------------------------------------------------------------
# Platform defaults and host awareness


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def platform_home() -> Path:
    if IS_WINDOWS:
        return _local_appdata() / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_STATE_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".local" / "state") / APP_NAME


def platform_cache() -> Path:
    if IS_WINDOWS:
        return _local_appdata() / APP_NAME / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    base = os.environ.get("XDG_CACHE_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".cache") / APP_NAME


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", "").strip()
    return expand(raw) if raw else Path.home() / ".codex"


def claude_config_home() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return expand(raw) if raw else Path.home() / ".claude"


def read_codex_config() -> tuple[dict[str, Any], str | None]:
    """Return (config, error). A missing file is not an error."""
    path = codex_home() / "config.toml"
    if not path.is_file():
        return {}, None
    try:
        return toml_compat.load_toml(path), None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return {}, f"cannot parse {path}: {exc}"


def codex_settings(codex_config: dict[str, Any]) -> dict[str, Any]:
    """The user's own Codex defaults that this skill deliberately leaves alone."""
    keys = ("model", "model_provider", "model_reasoning_effort", "sandbox_mode", "approval_policy", "web_search")
    desktop = codex_config.get("desktop") if isinstance(codex_config.get("desktop"), dict) else {}
    found = {key: codex_config.get(key) for key in keys if not isinstance(codex_config.get(key), (dict, list))}
    found["desktop.projectlessWorkspaceRoot"] = desktop.get("projectlessWorkspaceRoot")
    return found


def projectless_root(codex_config: dict[str, Any]) -> Path | None:
    desktop = codex_config.get("desktop")
    value = desktop.get("projectlessWorkspaceRoot") if isinstance(desktop, dict) else None
    if isinstance(value, str) and value.strip() and os.path.isabs(os.path.expanduser(value.strip())):
        return expand(value.strip())
    return None


@dataclass
class Anchor:
    kind: str  # claude-tmpdir | codex-projectless | codex-home | claude-config
    path: Path
    origin: str


def host_anchors(codex_config: dict[str, Any]) -> list[Anchor]:
    anchors: list[Anchor] = []
    tmpdir = os.environ.get("CLAUDE_CODE_TMPDIR", "").strip()
    if tmpdir and os.path.isabs(os.path.expanduser(tmpdir)):
        anchors.append(Anchor("claude-tmpdir", expand(tmpdir), "CLAUDE_CODE_TMPDIR"))
    root = projectless_root(codex_config)
    if root:
        anchors.append(Anchor("codex-projectless", root, "Codex [desktop].projectlessWorkspaceRoot"))
    for variable, kind in (("CODEX_HOME", "codex-home"), ("CLAUDE_CONFIG_DIR", "claude-config")):
        value = os.environ.get(variable, "").strip()
        if value and os.path.isabs(os.path.expanduser(value)):
            anchors.append(Anchor(kind, expand(value), variable))
    return anchors


def infer_locations(anchors: list[Anchor]) -> dict[str, tuple[Path, str]]:
    inferred: dict[str, tuple[Path, str]] = {}
    for anchor in anchors:
        if anchor.kind == "codex-projectless" and "workspaces" not in inferred:
            inferred["workspaces"] = (anchor.path, f"follows {anchor.origin} (<root>/<date>/<slug> naming)")
        if anchor.kind == "claude-tmpdir" and "cache" not in inferred:
            inferred["cache"] = (anchor.path / APP_NAME, f"follows {anchor.origin}")
    home = Path.home()
    for anchor in anchors:
        # Only a host folder the user moved out of the home directory says where data should go.
        if is_within(anchor.path, home):
            continue
        app_root = anchor.path.parent if anchor.kind in ("claude-tmpdir", "codex-projectless") else anchor.path
        base = app_root.parent
        if same_path(base, Path(base.anchor)) or same_path(base, home):
            continue
        inferred["records"] = (base / APP_NAME / "runs", f"next to the host data folder {app_root} ({anchor.origin})")
        break
    return inferred


# ---------------------------------------------------------------------------
# Resolution


@dataclass
class Location:
    kind: str
    path: Path
    source: str  # cli | env | config | inferred | default
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "source": self.source, "detail": self.detail, "purpose": KIND_PURPOSE[self.kind]}


@dataclass
class Locations:
    home: Location
    records: Location
    cache: Location
    workspaces: Location
    confirmed: bool

    def get(self, kind: str) -> Location:
        return getattr(self, kind)

    def as_dict(self) -> dict[str, Any]:
        data = {kind: self.get(kind).as_dict() for kind in ("home", *LOCATION_KINDS)}
        data["confirmed"] = self.confirmed
        return data


def resolve_home(cli_value: str | None) -> Location:
    if cli_value:
        return Location("home", expand(cli_value), "cli", "--home")
    value = env_value("HOME")
    if value:
        return Location("home", expand(value), "env", env_name("HOME"))
    return Location("home", platform_home(), "default", "platform default")


def default_locations(home: Path) -> dict[str, Path]:
    return {"records": home / "runs", "cache": platform_cache(), "workspaces": home / "workspaces"}


def resolve_locations(
    home: Location,
    cli_values: dict[str, str | None],
    config: dict[str, Any],
    codex_config: dict[str, Any],
) -> Locations:
    inferred = infer_locations(host_anchors(codex_config))
    defaults = default_locations(home.path)
    configured = config.get("locations") or {}
    resolved: dict[str, Location] = {}
    for kind in LOCATION_KINDS:
        env_key = KIND_ENV[kind]
        if cli_values.get(kind):
            resolved[kind] = Location(kind, expand(cli_values[kind]), "cli", KIND_FLAG[kind])
        elif env_value(env_key):
            resolved[kind] = Location(kind, expand(env_value(env_key)), "env", env_name(env_key))
        elif isinstance(configured.get(kind), str) and configured[kind].strip():
            resolved[kind] = Location(kind, expand(configured[kind].strip()), "config", "config.json")
        elif kind in inferred:
            resolved[kind] = Location(kind, inferred[kind][0], "inferred", inferred[kind][1])
        else:
            resolved[kind] = Location(kind, defaults[kind], "default", "platform default")
    explicit = all(resolved[kind].source in ("cli", "env") for kind in LOCATION_KINDS)
    confirmed = bool(config.get("locations_confirmed")) or explicit
    return Locations(home, resolved["records"], resolved["cache"], resolved["workspaces"], confirmed)


def location_choices(home: Path, codex_config: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    """What onboarding shows: the recommendation (host layout) and the platform default."""
    inferred = infer_locations(host_anchors(codex_config))
    defaults = default_locations(home)
    recommended = {}
    for kind in LOCATION_KINDS:
        path, why = inferred.get(kind, (defaults[kind], "no host layout hint, so the platform default"))
        recommended[kind] = {"path": str(path), "why": why}
    default = {kind: {"path": str(defaults[kind]), "why": "platform default"} for kind in LOCATION_KINDS}
    return {"recommended": recommended, "default": default}


# ---------------------------------------------------------------------------
# Run records and isolated workspaces


def new_run_id(runner: str) -> str:
    return f"{now_utc().strftime('%Y%m%dT%H%M%S')}-{runner}-{uuid.uuid4().hex[:6]}"


def run_started(run_id: str) -> dt.datetime | None:
    match = RUN_ID_RE.match(run_id)
    if not match:
        return None
    return dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)


def slugify(text: str, fallback: str) -> str:
    slug = SLUG_RE.sub("-", text.lower()).strip("-")[:40].strip("-")
    return slug or fallback


def create_isolated_workspace(root: Path, slug: str, today: dt.date | None = None) -> Path:
    """<root>/<YYYY-MM-DD>/<slug>, then <slug>-2, <slug>-3 ... like Codex's projectless chats."""
    day = root / (today or dt.date.today()).isoformat()
    day.mkdir(parents=True, exist_ok=True)
    for number in range(1, 1000):
        candidate = day / (slug if number == 1 else f"{slug}-{number}")
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise DelegateError(f"Cannot create an isolated workspace under {day}")


def planned_isolated_workspace(root: Path, slug: str, today: dt.date | None = None) -> Path:
    day = root / (today or dt.date.today()).isoformat()
    for number in range(1, 1000):
        candidate = day / (slug if number == 1 else f"{slug}-{number}")
        if not candidate.exists():
            return candidate
    return day / slug


def list_runs(records: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not records.is_dir():
        return runs
    for entry in sorted(records.iterdir(), key=lambda item: item.name):
        if not RUN_ID_RE.match(entry.name) or is_link_like(entry) or not entry.is_dir():
            continue
        data = None
        result = entry / "result.json"
        updated = 0
        if result.is_file():
            try:
                updated = result.stat().st_mtime_ns
                data = read_json(result)
            except (OSError, ValueError):
                data = None
        runs.append({"run_id": entry.name, "path": entry, "result": data if isinstance(data, dict) else None, "updated_ns": updated})
    return runs


def _newest_first(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Run ids only have one-second resolution; the result file's mtime orders runs within a second.
    return sorted(runs, key=lambda run: (run["updated_ns"], run["run_id"]), reverse=True)


def find_run(records: Path, ref: str) -> Path:
    if ref == "latest":
        runs = list_runs(records)
        if not runs:
            raise DelegateError(f"No runs recorded yet in {records}")
        return _newest_first(runs)[0]["path"]
    if RUN_ID_RE.match(ref) and (records / ref).is_dir():
        return records / ref
    candidate = expand(ref)
    if candidate.is_dir() and (candidate / "result.json").is_file():
        return candidate
    raise DelegateError(f"Run not found: {ref} (looked in {records}; pass a run id, 'latest' or a run directory)")


def workspace_for_session(records: Path, runner: str, session_id: str) -> tuple[Path, bool] | None:
    """The workspace an earlier run of this session used, and whether it was isolated."""
    for run in _newest_first(list_runs(records)):
        result = run["result"] or {}
        workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
        if result.get("runner") == runner and result.get("session_id") == session_id and workspace.get("path"):
            return Path(workspace["path"]), bool(workspace.get("isolated"))
    return None


def isolated_workspaces(records: Path) -> list[Path]:
    seen: dict[str, Path] = {}
    for run in list_runs(records):
        workspace = (run["result"] or {}).get("workspace")
        if isinstance(workspace, dict) and workspace.get("isolated") and workspace.get("path"):
            path = Path(workspace["path"])
            seen[os.path.normcase(str(path))] = path
    return [path for path in seen.values() if path.is_dir()]


# ---------------------------------------------------------------------------
# Pointers to the delegated CLI's own session files


def claude_project_dir_name(workspace: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "-", workspace)


def find_codex_rollout(sessions_root: Path, session_id: str, started: dt.datetime | None) -> Path | None:
    if not sessions_root.exists() or not re.fullmatch(r"[0-9A-Za-z-]+", session_id):
        return None
    suffix = f"-{session_id}.jsonl"
    days = []
    if started:
        local = started.astimezone()
        days = [(local + dt.timedelta(days=offset)).date() for offset in (0, -1, 1)]
    for day in days:
        folder = sessions_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if folder.is_dir():
            for entry in folder.iterdir():
                if entry.name.startswith("rollout-") and entry.name.endswith(suffix) and entry.is_file():
                    return entry
    for directory, dirs, files in os.walk(sessions_root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not is_link_like(Path(directory) / name))
        for name in files:
            if name.startswith("rollout-") and name.endswith(suffix):
                return Path(directory) / name
    return None


def cli_session_file(runner: str, session_id: str | None, workspace: Path, started: dt.datetime | None) -> dict[str, Any]:
    """Where the delegated CLI keeps this conversation. The skill never copies it."""
    if not session_id:
        return {"path": None, "exists": False, "note": "the runner returned no session id"}
    if runner == "codex":
        root = codex_home() / "sessions"
        path = find_codex_rollout(root, session_id, started)
        return {
            "path": str(path) if path else None,
            "exists": bool(path),
            "root": str(root),
            "note": None if path else "rollout file not found under the Codex sessions folder",
        }
    if runner == "claude":
        path = claude_config_home() / "projects" / claude_project_dir_name(str(workspace)) / f"{session_id}.jsonl"
        return {"path": str(path), "exists": path.is_file(), "note": None}
    return {"path": None, "exists": False, "note": f"{runner} keeps its own session log; its location is not wired into this skill"}


# ---------------------------------------------------------------------------
# Sizes, the locations report and prune


def _iso_from_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(microsecond=0).isoformat()


def dir_stats(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "files": 0, "bytes": 0, "size": "0 B", "oldest": None, "newest": None}
    files = 0
    total = 0
    oldest: float | None = None
    newest: float | None = None
    for directory, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not is_link_like(Path(directory) / name)]
        for name in names:
            try:
                info = os.lstat(Path(directory) / name)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            files += 1
            total += info.st_size
            oldest = info.st_mtime if oldest is None else min(oldest, info.st_mtime)
            newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
    return {
        "exists": True,
        "files": files,
        "bytes": total,
        "size": human_size(total),
        "oldest": _iso_from_timestamp(oldest),
        "newest": _iso_from_timestamp(newest),
    }


def link_target(path: Path) -> str | None:
    return os.path.realpath(path) if path.exists() and is_link_like(path) else None


def locations_report(locations: Locations) -> dict[str, Any]:
    report: dict[str, Any] = {"confirmed": locations.confirmed}
    home = locations.home
    report["home"] = {**home.as_dict(), "config_exists": (home.path / "config.json").is_file()}
    records = locations.records
    runs = list_runs(records.path)
    report["records"] = {**records.as_dict(), **dir_stats(records.path), "runs": len(runs)}
    cache = locations.cache
    inflight = cache.path / INFLIGHT_DIR
    report["cache"] = {**cache.as_dict(), **dir_stats(cache.path), "inflight_runs": len(_inflight_dirs(inflight))}
    workspaces = locations.workspaces
    created = isolated_workspaces(records.path)
    sizes = [dir_stats(path) for path in created]
    report["workspaces"] = {
        **workspaces.as_dict(),
        "exists": workspaces.path.exists(),
        "created_by_this_skill": len(created),
        "bytes": sum(item["bytes"] for item in sizes),
        "size": human_size(sum(item["bytes"] for item in sizes)),
        "note": "only isolated workspaces recorded in runs are counted; prune never deletes workspaces",
    }
    codex_sessions = codex_home() / "sessions"
    report["external"] = {
        "codex_sessions": {"path": str(codex_sessions), "exists": codex_sessions.exists(), "link_target": link_target(codex_sessions)},
        "claude_projects": {"path": str(claude_config_home() / "projects"), "exists": (claude_config_home() / "projects").exists()},
        "note": "delegated CLIs keep their own conversations here; this skill only points to them and never deletes them",
    }
    legacy = Path.home() / ".oil-delegate"
    report["legacy"] = {"path": str(legacy), **dir_stats(legacy), "note": "upstream oil-delegate data; read-only, never modified"}
    return report


def _inflight_dirs(inflight: Path) -> list[Path]:
    if not inflight.is_dir():
        return []
    return [entry for entry in inflight.iterdir() if RUN_ID_RE.match(entry.name) and entry.is_dir() and not is_link_like(entry)]


def prune_plan(records: Path, cache: Path, days: int, max_total_mb: int, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    cutoff = now - dt.timedelta(days=days)
    candidates: list[dict[str, Any]] = []
    kept: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in list_runs(records):
        stats = dir_stats(run["path"])
        started = run_started(run["run_id"])
        status = (run["result"] or {}).get("status")
        if status == "running" and started and started > now - dt.timedelta(days=1):
            continue  # possibly still running
        if started and started < cutoff:
            candidates.append(_candidate(run["path"], stats, f"older than {days} days", "run"))
        else:
            kept.append((run, stats))
    limit = max_total_mb * 1024 * 1024
    total = sum(stats["bytes"] for _, stats in kept)
    for run, stats in kept:  # oldest first
        if total <= limit:
            break
        candidates.append(_candidate(run["path"], stats, f"records exceed {max_total_mb} MB; removing oldest first", "run"))
        total -= stats["bytes"]
    for entry in _inflight_dirs(cache / INFLIGHT_DIR):
        started = run_started(entry.name)
        if started and started < now - dt.timedelta(hours=24):
            candidates.append(_candidate(entry, dir_stats(entry), "orphaned in-flight cache older than 24 hours", "cache"))
    return {
        "candidates": candidates,
        "count": len(candidates),
        "bytes": sum(item["bytes"] for item in candidates),
        "size": human_size(sum(item["bytes"] for item in candidates)),
        "rules": {"days": days, "max_total_mb": max_total_mb},
        "never_deleted": [
            "the delegated CLIs' own session files",
            "any workspace, including isolated workspaces",
            "config.json",
        ],
    }


def _candidate(path: Path, stats: dict[str, Any], reason: str, kind: str) -> dict[str, Any]:
    return {"path": str(path), "kind": kind, "files": stats["files"], "bytes": stats["bytes"], "size": stats["size"], "reason": reason}


def remove_run_tree(path: Path, allowed_roots: list[Path]) -> None:
    """Delete one run or in-flight directory after strict checks."""
    if not RUN_ID_RE.match(path.name):
        raise DelegateError(f"Refusing to delete {path}: not a run directory name")
    if is_link_like(path) or not path.is_dir():
        raise DelegateError(f"Refusing to delete {path}: not a plain directory")
    if not any(is_within(path, root) and not same_path(path, root) for root in allowed_roots):
        raise DelegateError(f"Refusing to delete {path}: outside the records/cache folders")
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files]:
            if is_link_like(Path(directory) / name):
                raise DelegateError(f"Refusing to delete {path}: it contains a link ({name})")

    def make_writable(function: Any, target: str, _info: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:  # pragma: no cover - exercised on 3.10/3.11 in CI
        shutil.rmtree(path, onerror=make_writable)


def remove_inflight(directory: Path, cache_root: Path) -> None:
    """Remove this run's own in-flight files (created by the same process)."""
    if not directory.is_dir() or not is_within(directory, cache_root / INFLIGHT_DIR):
        return
    for name in ("stdout.log", "stderr.log"):
        (directory / name).unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass
