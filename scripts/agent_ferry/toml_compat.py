"""Read TOML config files on Python 3.10, where tomllib does not exist.

tomllib (3.11+) is used when available. The fallback is a deliberately small,
read-only parser: tables, dotted keys, basic/literal strings, booleans and
numbers. Arrays, inline tables, multi-line strings and dates are skipped,
because this skill only needs a handful of scalar keys from ~/.codex/config.toml.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:  # pragma: no cover - depends on the interpreter
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10
    _tomllib = None

_SKIP = object()
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def load_toml(path: Path, *, minimal: bool = False) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if _tomllib is not None and not minimal:
        return _tomllib.loads(text)
    return parse_minimal(text)


def parse_minimal(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current: dict[str, Any] | None = root
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            current = None  # arrays of tables are not needed
            continue
        if line.startswith("["):
            header = _strip_comment(line)
            keys = _split_key(header[1:-1]) if header.endswith("]") else None
            current = _descend(root, keys) if keys else None
            continue
        key_text, value_text = _split_assignment(line)
        if value_text is None:
            continue
        keys = _split_key(key_text)
        value, extra = _parse_value(value_text, lines, index)
        index += extra
        if current is None or not keys or value is _SKIP:
            continue
        target = _descend(current, keys[:-1])
        if target is not None:
            target[keys[-1]] = value
    return root


def _descend(table: dict[str, Any], keys: list[str]) -> dict[str, Any] | None:
    for key in keys:
        nxt = table.setdefault(key, {})
        if not isinstance(nxt, dict):
            return None
        table = nxt
    return table


def _split_assignment(line: str) -> tuple[str, str | None]:
    quote = None
    for position, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "=":
            return line[:position], line[position + 1 :].strip()
    return line, None


def _split_key(text: str) -> list[str] | None:
    keys: list[str] = []
    rest = text.strip()
    while rest:
        if rest[0] in "\"'":
            end = rest.find(rest[0], 1)
            if end < 0:
                return None
            keys.append(rest[1:end])
            rest = rest[end + 1 :].strip()
        else:
            match = _BARE_KEY.match(rest)
            if not match:
                return None
            keys.append(match.group(0))
            rest = rest[match.end() :].strip()
        if rest.startswith("."):
            rest = rest[1:].strip()
        elif rest:
            return None
    return keys or None


def _strip_comment(text: str) -> str:
    quote = None
    escaped = False
    for position, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return text[:position].strip()
    return text.strip()


def _parse_value(text: str, lines: list[str], index: int) -> tuple[Any, int]:
    if text.startswith('"""') or text.startswith("'''"):
        delimiter = text[:3]
        if delimiter in text[3:]:
            return _SKIP, 0
        extra = 0
        while index + extra < len(lines):
            extra += 1
            if delimiter in lines[index + extra - 1]:
                break
        return _SKIP, extra
    if text.startswith('"'):
        return _basic_string(text), 0
    if text.startswith("'"):
        end = text.find("'", 1)
        return (text[1:end] if end > 0 else _SKIP), 0
    if text.startswith("["):
        depth = _bracket_depth(text)
        extra = 0
        while depth > 0 and index + extra < len(lines):
            depth += _bracket_depth(lines[index + extra])
            extra += 1
        return _SKIP, extra
    if text.startswith("{"):
        return _SKIP, 0
    value = _strip_comment(text)
    if value == "true":
        return True, 0
    if value == "false":
        return False, 0
    cleaned = value.replace("_", "")
    for convert in (int, float):
        try:
            return convert(cleaned), 0
        except ValueError:
            continue
    return _SKIP, 0


def _basic_string(text: str) -> Any:
    chars: list[str] = []
    position = 1
    while position < len(text):
        char = text[position]
        if char == '"':
            return "".join(chars)
        if char == "\\" and position + 1 < len(text):
            code = text[position + 1]
            if code in _ESCAPES:
                chars.append(_ESCAPES[code])
                position += 2
                continue
            if code in "uU":
                width = 4 if code == "u" else 8
                digits = text[position + 2 : position + 2 + width]
                try:
                    chars.append(chr(int(digits, 16)))
                except ValueError:
                    return _SKIP
                position += 2 + width
                continue
            return _SKIP
        chars.append(char)
        position += 1
    return _SKIP


def _bracket_depth(text: str) -> int:
    depth = 0
    quote = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            break
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    return depth
