"""Find Python 3.10+ interpreters (used by doctor and config --set-python).

The order mirrors scripts/delegate.ps1 and scripts/delegate.sh:
override -> pinned -> PATH -> environments and version managers -> common
install folders -> Windows registry (PEP 514). Every candidate is executed
once; nothing is trusted by its name alone.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .common import IS_WINDOWS, DelegateError, env_name, env_value

MIN_VERSION = (3, 10)
# No double quotes and no percent signs: safe through PowerShell 5.1 and cmd.exe shims.
PROBE = "import sys;print('.'.join(map(str,sys.version_info[:3])));print(sys.executable)"
MAX_PROBES = 40
CONDA_DIR_NAMES = ("miniconda3", "anaconda3", "miniforge3", "mambaforge", "micromamba", "miniforge")


@dataclass
class Candidate:
    command: list[str]
    source: str
    version: str | None = None
    executable: str | None = None
    accepted: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _probe_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)
    return env


def evaluate(candidate: Candidate, timeout: float = 15.0) -> Candidate:
    try:
        proc = subprocess.run(
            [*candidate.command, "-I", "-c", PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_probe_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        candidate.reason = _failure_reason(candidate, str(exc))
        return candidate
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or len(lines) < 2 or not version_tuple(lines[0]):
        detail = (proc.stderr.strip().splitlines() or [f"exit code {proc.returncode}"])[0][:200]
        candidate.reason = _failure_reason(candidate, detail)
        return candidate
    candidate.version, candidate.executable = lines[0], lines[1]
    if version_tuple(lines[0]) < MIN_VERSION:
        candidate.reason = f"Python {lines[0]} is older than 3.10"
        return candidate
    candidate.accepted = True
    return candidate


def _failure_reason(candidate: Candidate, detail: str) -> str:
    if IS_WINDOWS and "\\windowsapps\\" in candidate.command[0].lower():
        return "Microsoft Store placeholder (App Execution Alias), not an installed Python"
    return f"failed to run: {detail}"


def check_interpreter(path: str) -> Candidate:
    candidate = evaluate(Candidate([path], "requested"))
    if not candidate.accepted:
        raise DelegateError(f"Not a usable Python 3.10+ interpreter: {path} ({candidate.reason})")
    return candidate


# ---------------------------------------------------------------------------
# Candidate generation


def which_all(name: str) -> list[str]:
    found: list[str] = []
    extensions = [""]
    if IS_WINDOWS:
        extensions = [ext.lower() for ext in os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(";") if ext]
        extensions = [ext for ext in extensions if ext in (".exe", ".bat", ".cmd")]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for ext in extensions:
            candidate = Path(directory) / f"{name}{ext}"
            if candidate.is_file() and (IS_WINDOWS or os.access(candidate, os.X_OK)):
                found.append(str(candidate))
    return found


def env_python(prefix: Path) -> Path:
    if IS_WINDOWS and (prefix / "python.exe").exists():
        return prefix / "python.exe"
    for name in ("python3", "python"):
        if (prefix / "bin" / name).exists():
            return prefix / "bin" / name
    return prefix / ("python.exe" if IS_WINDOWS else "bin/python3")


def _version_key(path: str) -> tuple[int, ...]:
    for part in reversed(re.split(r"[\\/]", path)[:-1]):
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", part)
        if match:
            return tuple(int(group) for group in match.groups() if group is not None)
        match = re.search(r"(?i)python(\d)(\d+)", part)  # e.g. Python312
        if match:
            return (int(match.group(1)), int(match.group(2)))
    return ()


def _by_version_desc(paths: list[str]) -> list[str]:
    return sorted(paths, key=_version_key, reverse=True)


def conda_bases() -> list[tuple[Path, str]]:
    bases: list[tuple[Path, str]] = []
    for variable in ("CONDA_EXE", "MAMBA_EXE"):
        value = os.environ.get(variable, "").strip()
        if value:
            bases.append((Path(value).parent.parent, variable))
    root = os.environ.get("MAMBA_ROOT_PREFIX", "").strip()
    if root:
        bases.append((Path(root), "MAMBA_ROOT_PREFIX"))
    conda = shutil.which("conda")
    if conda:
        # Prefer the path as found (keeps stable links such as Scoop's `current`); resolve only if needed.
        for base in (Path(conda).parent.parent, Path(conda).resolve().parent.parent):
            if (base / "conda-meta").is_dir():
                bases.append((base, "conda on PATH"))
                break
    return [(base, origin) for base, origin in bases if (base / "conda-meta").is_dir() or (base / "envs").is_dir()]


def conda_info() -> dict[str, Any]:
    """Ask conda itself (doctor only; can take a few seconds)."""
    executable = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if not executable:
        return {"executable": None}
    try:
        proc = subprocess.run(
            [executable, "info", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"executable": executable, "error": str(exc)}
    if not isinstance(data, dict):
        data = {}
    return {
        "executable": executable,
        "version": data.get("conda_version"),
        "base": data.get("root_prefix"),
        "envs": data.get("envs") if isinstance(data.get("envs"), list) else [],
        "active_prefix": data.get("active_prefix"),
        "error": None if data else f"conda info exited {proc.returncode}",
    }


def manager_candidates(extra_conda_bases: list[Path] | None = None) -> list[tuple[list[str], str]]:
    out: list[tuple[list[str], str]] = []
    prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if prefix:
        out.append(([str(env_python(Path(prefix)))], "CONDA_PREFIX (active conda environment)"))
    bases = conda_bases() + [(base, "conda info --base") for base in extra_conda_bases or []]
    for base, origin in bases:
        out.append(([str(env_python(base))], f"conda base ({origin})"))
        for env in sorted(glob.glob(str(base / "envs" / "*"))):
            out.append(([str(env_python(Path(env)))], f"conda env {Path(env).name} ({origin})"))
    uv = shutil.which("uv")
    if uv:
        found = _uv_python_find(uv)
        if found:
            out.append(([found], "uv python find"))
    home = Path.home()
    if IS_WINDOWS:
        pyenv = os.environ.get("PYENV_ROOT") or os.environ.get("PYENV") or str(home / ".pyenv" / "pyenv-win")
        for path in _by_version_desc(glob.glob(str(Path(pyenv) / "versions" / "*" / "python.exe"))):
            out.append(([path], "pyenv-win"))
        for scoop_root, origin in _scoop_roots():
            for path in _by_version_desc(glob.glob(str(scoop_root / "apps" / "python*" / "current" / "python.exe"))):
                out.append(([path], f"Scoop ({origin})"))
            for name in ("miniconda3", "anaconda3", "miniforge", "mambaforge"):
                path = scoop_root / "apps" / name / "current" / "python.exe"
                if path.exists():
                    out.append(([str(path)], f"Scoop {name} ({origin})"))
    else:
        pyenv = os.environ.get("PYENV_ROOT") or str(home / ".pyenv")
        for path in _by_version_desc(glob.glob(str(Path(pyenv) / "versions" / "*" / "bin" / "python3"))):
            out.append(([path], "pyenv"))
        asdf = os.environ.get("ASDF_DATA_DIR") or str(home / ".asdf")
        for path in _by_version_desc(glob.glob(str(Path(asdf) / "installs" / "python" / "*" / "bin" / "python3"))):
            out.append(([path], "asdf"))
        mise = os.environ.get("MISE_DATA_DIR") or str(home / ".local" / "share" / "mise")
        for path in _by_version_desc(glob.glob(str(Path(mise) / "installs" / "python" / "*" / "bin" / "python3"))):
            out.append(([path], "mise"))
    return out


def _uv_python_find(uv: str) -> str | None:
    env = dict(os.environ, UV_PYTHON_DOWNLOADS="never")
    try:
        proc = subprocess.run(
            [uv, "python", "find"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else ""
    return line or None


def _scoop_roots() -> list[tuple[Path, str]]:
    roots = []
    if os.environ.get("SCOOP"):
        roots.append((Path(os.environ["SCOOP"]), "SCOOP"))
    roots.append((Path.home() / "scoop", "default root"))
    if os.environ.get("SCOOP_GLOBAL"):
        roots.append((Path(os.environ["SCOOP_GLOBAL"]), "SCOOP_GLOBAL"))
    program_data = os.environ.get("ProgramData")
    if program_data:
        roots.append((Path(program_data) / "scoop", "global default"))
    return [(root, origin) for root, origin in roots if root.is_dir()]


def common_dir_candidates() -> list[tuple[list[str], str]]:
    out: list[tuple[list[str], str]] = []
    home = Path.home()
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        patterns = [
            str(local / "Programs" / "Python" / "Python3*" / "python.exe"),
            str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python3*" / "python.exe"),
            r"C:\Python3*\python.exe",
        ]
        for pattern in patterns:
            for path in _by_version_desc(glob.glob(pattern)):
                out.append(([path], "common install folder"))
        roots = [home, local, Path(os.environ.get("ProgramData", r"C:\ProgramData"))]
        for root in roots:
            for name in CONDA_DIR_NAMES:
                path = root / name / "python.exe"
                if path.exists():
                    out.append(([str(path)], f"conda default folder {name}"))
    else:
        for path in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
            if os.path.exists(path):
                out.append(([path], "common install folder"))
        for path in _by_version_desc(glob.glob("/Library/Frameworks/Python.framework/Versions/3.*/bin/python3")):
            out.append(([path], "python.org framework"))
        for root in (home, Path("/opt")):
            for name in (*CONDA_DIR_NAMES, "conda"):
                path = root / name / "bin" / "python"
                if path.exists():
                    out.append(([str(path)], f"conda default folder {name}"))
    return out


def registry_candidates() -> list[tuple[list[str], str]]:
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []
    out: list[tuple[list[str], str]] = []
    views = [
        (winreg.HKEY_CURRENT_USER, 0, "HKCU"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "HKLM"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "HKLM (32-bit)"),
    ]
    for hive, view, label in views:
        try:
            root = winreg.OpenKey(hive, r"Software\Python", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            for company in _subkeys(winreg, root):
                if company == "PyLauncher":
                    continue
                try:
                    company_key = winreg.OpenKey(root, company)
                except OSError:
                    continue
                with company_key:
                    for tag in _subkeys(winreg, company_key):
                        path = _install_path(winreg, company_key, tag)
                        if path:
                            out.append(([path], f"registry {label}\\{company}\\{tag}"))
    return out


def _subkeys(winreg: Any, key: Any) -> list[str]:
    names = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def _install_path(winreg: Any, company_key: Any, tag: str) -> str | None:
    try:
        with winreg.OpenKey(company_key, tag + r"\InstallPath") as install:
            try:
                executable, _ = winreg.QueryValueEx(install, "ExecutablePath")
                if executable:
                    return str(executable)
            except OSError:
                pass
            folder, _ = winreg.QueryValueEx(install, "")
            return str(Path(folder) / "python.exe") if folder else None
    except OSError:
        return None


def candidate_commands(config: dict[str, Any], *, conda_base_hint: list[Path] | None = None) -> list[tuple[list[str], str]]:
    out: list[tuple[list[str], str]] = []
    override = env_value("PYTHON")
    if override:
        out.append(([override], f"{env_name('PYTHON')} (override)"))
    pinned = config.get("python")
    if isinstance(pinned, str) and pinned.strip():
        out.append(([pinned.strip()], "config.json (pinned with config --set-python)"))
    names = ("python", "python3") if IS_WINDOWS else ("python3", "python")
    for name in names:
        for path in which_all(name):
            out.append(([path], f"PATH ({name})"))
    if IS_WINDOWS:
        for launcher in which_all("py"):
            out.append(([launcher, "-3"], "PATH (py -3 launcher)"))
    out.extend(manager_candidates(conda_base_hint))
    out.extend(common_dir_candidates())
    out.extend(registry_candidates())
    return out


def discover(config: dict[str, Any], *, conda_base_hint: list[Path] | None = None, limit: int = MAX_PROBES) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen_commands: set[str] = set()
    seen_executables: dict[str, str] = {}
    for command, source in candidate_commands(config, conda_base_hint=conda_base_hint):
        key = os.path.normcase(os.path.abspath(command[0])) + "|" + " ".join(command[1:])
        if key in seen_commands:
            continue
        seen_commands.add(key)
        if len(candidates) >= limit:
            break
        if not command[1:] and not Path(command[0]).exists() and os.sep in command[0]:
            candidates.append(Candidate(command, source, reason="path does not exist"))
            continue
        candidate = evaluate(Candidate(command, source))
        if candidate.accepted and candidate.executable:
            real = os.path.normcase(os.path.realpath(candidate.executable))
            if real in seen_executables:
                candidate.accepted = False
                candidate.reason = f"same interpreter as {seen_executables[real]}"
            else:
                seen_executables[real] = " ".join(command)
        candidates.append(candidate)
    return candidates


def environment_hints() -> dict[str, Any]:
    """Cheap facts for the delegated agent (no subprocess): paths and versions only."""
    hints: dict[str, Any] = {
        "python": sys.executable,
        "python_version": ".".join(map(str, sys.version_info[:3])),
    }
    bases = conda_bases()
    prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if prefix:
        hints["conda_active_env"] = prefix
    if bases:
        hints["conda_base"] = str(bases[0][0])
    return hints
