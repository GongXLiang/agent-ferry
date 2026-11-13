#!/bin/sh
# agent-delegate-verify bootstrap for POSIX sh (Linux, macOS, Git Bash/MSYS on Windows).
# Finds Python 3.10+ without needing Python, then runs scripts/delegate.py with every argument.
# Order: AGENT_DELEGATE_VERIFY_PYTHON (must work) -> python pinned in config.json -> PATH
#        (python3, python, py -3) -> conda/mamba, uv, pyenv, asdf, mise -> common folders.
#        Each candidate is executed once; anything older than 3.10 is rejected.
# Usage:  sh <skill>/scripts/delegate.sh <subcommand> [options]
#         sh <skill>/scripts/delegate.sh --which    (show the choice and rejected candidates)

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
main="$here/delegate.py"
# No double quotes and no percent signs, so the same probe works everywhere.
probe="import sys;print('.'.join(map(str,sys.version_info[:3])));print(sys.executable)"
nl='
'
seen="$nl"
rejected=""
found=""
found_extra=""
found_version=""
found_exe=""
found_source=""

say() { printf '%s\n' "[agent-delegate-verify] $*" >&2; }

to_unix() {  # Windows paths from the environment, when running under Git Bash/MSYS/Cygwin
    case "$1" in
        [A-Za-z]:\\*|[A-Za-z]:/*)
            if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s\n' "$1"; fi ;;
        *) printf '%s\n' "$1" ;;
    esac
}

try_python() {  # $1 executable, $2 source, $3 optional extra argument (py -3)
    cand=$1
    src=$2
    extra=${3:-}
    [ -n "$cand" ] || return 1
    case "$seen" in *"$nl$cand $extra$nl"*) return 1 ;; esac
    seen="$seen$cand $extra$nl"
    case "$cand" in */*) [ -f "$cand" ] || return 1 ;; esac
    if [ -n "$extra" ]; then
        out=$("$cand" "$extra" -I -c "$probe" 2>/dev/null) || { rejected="$rejected$cand [$src]: failed to run$nl"; return 1; }
    else
        out=$("$cand" -I -c "$probe" 2>/dev/null) || { rejected="$rejected$cand [$src]: failed to run$nl"; return 1; }
    fi
    ver=$(printf '%s\n' "$out" | sed -n '1p' | tr -d '\r')
    exe=$(printf '%s\n' "$out" | sed -n '2p' | tr -d '\r')
    major=${ver%%.*}
    rest=${ver#*.}
    minor=${rest%%.*}
    case "$major" in ''|*[!0-9]*) rejected="$rejected$cand [$src]: unexpected version output$nl"; return 1 ;; esac
    case "$minor" in ''|*[!0-9]*) rejected="$rejected$cand [$src]: unexpected version output$nl"; return 1 ;; esac
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
        rejected="$rejected$cand [$src]: Python $ver is older than 3.10$nl"
        return 1
    fi
    found=$cand
    found_extra=$extra
    found_version=$ver
    found_exe=$exe
    found_source=$src
    return 0
}

try_prefix() {  # $1 environment or base prefix, $2 source
    try_python "$1/bin/python3" "$2" || try_python "$1/bin/python" "$2" || try_python "$1/python.exe" "$2"
}

try_glob() {  # $1 source, then paths (already expanded by the shell)
    src=$1
    shift
    for path in "$@"; do
        [ -e "$path" ] || continue
        try_python "$path" "$src" && return 0
    done
    return 1
}

default_home() {
    case "$(uname -s 2>/dev/null)" in
        Darwin) printf '%s\n' "$HOME/Library/Application Support/agent-delegate-verify" ;;
        MINGW*|MSYS*|CYGWIN*) printf '%s\n' "$(to_unix "${LOCALAPPDATA:-$HOME/AppData/Local}")/agent-delegate-verify" ;;
        *) printf '%s\n' "${XDG_STATE_HOME:-$HOME/.local/state}/agent-delegate-verify" ;;
    esac
}

find_python() {
    if [ -n "${AGENT_DELEGATE_VERIFY_PYTHON:-}" ]; then
        if try_python "$(to_unix "$AGENT_DELEGATE_VERIFY_PYTHON")" "AGENT_DELEGATE_VERIFY_PYTHON"; then return 0; fi
        say "AGENT_DELEGATE_VERIFY_PYTHON is set but not usable: $AGENT_DELEGATE_VERIFY_PYTHON"
        printf '%s' "$rejected" >&2
        exit 9
    fi

    home_dir=${AGENT_DELEGATE_VERIFY_HOME:-}
    if [ -n "$home_dir" ]; then home_dir=$(to_unix "$home_dir"); else home_dir=$(default_home); fi
    config_file="$home_dir/config.json"
    if [ -f "$config_file" ]; then
        pinned=$(sed -n 's/^[[:space:]]*"python":[[:space:]]*"\(.*\)",\{0,1\}[[:space:]]*$/\1/p' "$config_file" | head -n 1 | sed 's/\\\\/\\/g')
        if [ -n "$pinned" ]; then
            if try_python "$(to_unix "$pinned")" "config.json (pinned)"; then return 0; fi
            say "pinned Python is not usable: $pinned; searching instead (update it with: config --set-python)"
        fi
    fi

    for name in python3 python; do
        path=$(command -v "$name" 2>/dev/null) || continue
        try_python "$path" "PATH ($name)" && return 0
    done
    if path=$(command -v py 2>/dev/null); then
        try_python "$path" "PATH (py -3)" "-3" && return 0
    fi

    if [ -n "${CONDA_PREFIX:-}" ]; then
        try_prefix "$(to_unix "$CONDA_PREFIX")" "CONDA_PREFIX (active conda environment)" && return 0
    fi
    bases=""
    for variable in CONDA_EXE MAMBA_EXE; do
        eval "value=\${$variable:-}"
        if [ -n "$value" ]; then
            value=$(to_unix "$value")
            bases="$bases$(dirname "$(dirname "$value")")|$variable$nl"
        fi
    done
    if [ -n "${MAMBA_ROOT_PREFIX:-}" ]; then bases="$bases$(to_unix "$MAMBA_ROOT_PREFIX")|MAMBA_ROOT_PREFIX$nl"; fi
    if conda_cmd=$(command -v conda 2>/dev/null); then
        base=$("$conda_cmd" info --base 2>/dev/null | head -n 1 | tr -d '\r')
        if [ -n "$base" ]; then bases="$bases$(to_unix "$base")|conda info --base$nl"; fi
    fi
    old_ifs=$IFS
    IFS=$nl
    for entry in $bases; do
        IFS=$old_ifs
        base=${entry%|*}
        origin=${entry##*|}
        try_prefix "$base" "conda base ($origin)" && return 0
        for env_dir in "$base"/envs/*; do
            [ -d "$env_dir" ] || continue
            try_prefix "$env_dir" "conda env $(basename "$env_dir") ($origin)" && return 0
        done
        IFS=$nl
    done
    IFS=$old_ifs

    if uv_cmd=$(command -v uv 2>/dev/null); then
        path=$(UV_PYTHON_DOWNLOADS=never "$uv_cmd" python find 2>/dev/null | head -n 1 | tr -d '\r')
        if [ -n "$path" ]; then try_python "$(to_unix "$path")" "uv python find" && return 0; fi
    fi

    pyenv_root=${PYENV_ROOT:-$HOME/.pyenv}
    try_glob "pyenv" "$pyenv_root"/versions/*/bin/python3 && return 0
    try_glob "asdf" "${ASDF_DATA_DIR:-$HOME/.asdf}"/installs/python/*/bin/python3 && return 0
    try_glob "mise" "${MISE_DATA_DIR:-$HOME/.local/share/mise}"/installs/python/*/bin/python3 && return 0

    try_glob "common install folder" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 && return 0
    try_glob "python.org framework" /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 && return 0
    for root in "$HOME" /opt; do
        for name in miniconda3 anaconda3 miniforge3 mambaforge micromamba conda; do
            try_prefix "$root/$name" "conda default folder $name" && return 0
        done
    done
    return 1
}

if ! find_python; then
    say "No Python 3.10+ found. Rejected candidates:"
    printf '%s' "$rejected" >&2
    say "Install Python 3.10+ (python.org, conda, uv, pyenv ...) or set AGENT_DELEGATE_VERIFY_PYTHON to its full path."
    exit 9
fi

if [ "${1:-}" = "--which" ]; then
    if [ -n "$found_extra" ]; then printf 'python=%s %s\n' "$found" "$found_extra"; else printf 'python=%s\n' "$found"; fi
    printf 'version=%s\nexecutable=%s\nsource=%s\n' "$found_version" "$found_exe" "$found_source"
    printf '%s' "$rejected" | while IFS= read -r line; do [ -n "$line" ] && printf 'rejected=%s\n' "$line"; done
    exit 0
fi

AGENT_DELEGATE_VERIFY_PYTHON_SOURCE=$found_source
export AGENT_DELEGATE_VERIFY_PYTHON_SOURCE
if [ -n "$found_extra" ]; then
    exec "$found" "$found_extra" "$main" "$@"
fi
exec "$found" "$main" "$@"
