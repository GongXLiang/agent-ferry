"""The skill's own config.json, which always lives in the home directory."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, DelegateError, atomic_write_json, read_json

LOCATION_KINDS = ("records", "cache", "workspaces")
DEFAULT_RETENTION = {"days": 30, "max_total_mb": 500}
LEGACY_KEYS = ("enabled_runners", "default_runner", "runner_paths", "roles")


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "onboarded": False,
        "enabled_runners": [],
        "default_runner": None,
        "runner_paths": {},
        "roles": {},
        "locations_confirmed": False,
        "locations": {},
        "python": None,
        "keep_raw": False,
        "env_hints": True,
        "retention": dict(DEFAULT_RETENTION),
    }


def config_path(home: Path) -> Path:
    return home / "config.json"


def load_config(home: Path) -> dict[str, Any]:
    path = config_path(home)
    if not path.exists():
        return default_config()
    try:
        loaded = read_json(path)
    except (OSError, ValueError) as exc:
        raise DelegateError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise DelegateError(f"Config must be a JSON object: {path}")
    merged = {**default_config(), **loaded}
    for key, kind in (
        ("enabled_runners", list),
        ("runner_paths", dict),
        ("roles", dict),
        ("locations", dict),
        ("retention", dict),
    ):
        if not isinstance(merged.get(key), kind):
            raise DelegateError(f"config.{key} must be a {'list' if kind is list else 'JSON object'}: {path}")
    merged["retention"] = {**DEFAULT_RETENTION, **merged["retention"]}
    return merged


def save_config(home: Path, config: dict[str, Any]) -> None:
    atomic_write_json(config_path(home), config)


def is_onboarded(config: dict[str, Any]) -> bool:
    return bool(config.get("onboarded") and config.get("enabled_runners"))


def legacy_default_dir() -> Path:
    return Path.home() / ".oil-delegate"


def read_legacy_config(directory: Path) -> dict[str, Any]:
    """Read the upstream oil-delegate config without touching the directory."""
    path = directory / "config.json"
    if not path.is_file():
        raise DelegateError(f"No legacy oil-delegate config found at {path}")
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise DelegateError(f"Cannot read legacy config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DelegateError(f"Legacy config is not a JSON object: {path}")
    return {key: data[key] for key in LEGACY_KEYS if key in data}
