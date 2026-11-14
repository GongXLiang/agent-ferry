"""Audit summaries: what the delegated agent ran, changed, searched and failed.

Codex is fully supported from `codex exec --json` (item types per
codex-rs/exec/src/exec_events.rs). Claude and WorkBuddy only expose a final
JSON object, so their summary is partial. Other runners are reported as
unsupported rather than guessed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .common import truncate
from .parsing import error_message, json_events, json_object

MAX_COMMANDS = 200
MAX_CHANGES = 500
MAX_WEB = 200
TEXT_LIMIT = 300


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _outside(path: str | None, workspace: Path | None) -> bool:
    if not path or workspace is None:
        return False
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        candidate = Path(os.path.normpath(candidate))
        root = Path(os.path.normpath(workspace))
    except (OSError, ValueError):
        return False
    norm_child = os.path.normcase(str(candidate))
    norm_root = os.path.normcase(str(root)).rstrip("\\/")
    return not (norm_child == norm_root or norm_child.startswith(norm_root + os.sep))


def _add_usage(total: dict[str, int] | None, usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return total
    total = dict(total or {})
    for key, value in usage.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
    return total


def audit_codex(events: list[dict[str, Any]], workspace: Path | None) -> dict[str, Any]:
    items: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    errors: list[str] = []
    usage: dict[str, int] | None = None
    for event in events:
        kind = event.get("type")
        if kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or f"anonymous-{len(order)}")
            if item_id not in items:
                order.append(item_id)
            merged = {**items.get(item_id, {}), **item}
            merged["_completed"] = kind == "item.completed" or items.get(item_id, {}).get("_completed", False)
            items[item_id] = merged
        elif kind == "turn.completed":
            usage = _add_usage(usage, event.get("usage"))
        elif kind == "turn.failed":
            errors.append(truncate(error_message(event.get("error")) or "turn failed", TEXT_LIMIT))
        elif kind == "error":
            errors.append(truncate(error_message(event.get("message") or event) or "stream error", TEXT_LIMIT))

    commands: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    searches: list[str] = []
    pages: list[str] = []
    finds: list[dict[str, Any]] = []
    mcp_calls: list[dict[str, Any]] = []
    collab_calls = 0
    todo: dict[str, int] | None = None
    messages = 0
    for item_id in order:
        item = items[item_id]
        kind = item.get("type")
        completed = item.get("_completed")
        if kind == "command_execution":
            status = item.get("status") or ("completed" if completed else "in_progress")
            if not completed and status == "in_progress":
                status = "incomplete"
            commands.append({
                "command": truncate(str(item.get("command") or ""), TEXT_LIMIT),
                "exit_code": item.get("exit_code"),
                "status": status,
            })
        elif kind == "file_change":
            for change in item.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                path = change.get("path")
                changes.append({
                    "path": path,
                    "kind": change.get("kind"),
                    "status": item.get("status"),
                    "outside_workspace": _outside(path, workspace),
                })
        elif kind == "web_search":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            action_type = action.get("type") or "search"
            if action_type == "open_page":
                if action.get("url"):
                    pages.append(str(action["url"]))
            elif action_type == "find_in_page":
                finds.append({"url": action.get("url"), "pattern": action.get("pattern")})
            else:
                queries = action.get("queries") if isinstance(action.get("queries"), list) else []
                if action.get("query"):
                    queries = [*queries, action["query"]]
                if not queries and item.get("query"):
                    queries = [item["query"]]
                searches.extend(str(query) for query in queries if query)
        elif kind == "mcp_tool_call":
            mcp_calls.append({
                "server": item.get("server"),
                "tool": item.get("tool"),
                "status": item.get("status"),
                "error": truncate(error_message(item.get("error")) or "", TEXT_LIMIT) or None,
            })
        elif kind == "collab_tool_call":
            collab_calls += 1
        elif kind == "todo_list":
            entries = item.get("items") if isinstance(item.get("items"), list) else []
            todo = {"total": len(entries), "completed": sum(1 for entry in entries if isinstance(entry, dict) and entry.get("completed"))}
        elif kind == "error":
            errors.append(truncate(str(item.get("message") or "error item"), TEXT_LIMIT))
        elif kind == "agent_message":
            messages += 1

    failed = [command for command in commands if command["status"] == "failed" or (isinstance(command["exit_code"], int) and command["exit_code"] != 0)]
    declined = [command for command in commands if command["status"] == "declined"]
    failed_changes = [change for change in changes if change["status"] == "failed"]
    outside = _unique([str(change["path"]) for change in changes if change["outside_workspace"]])
    searches = _unique(searches)
    pages = _unique(pages)
    return {
        "supported": True,
        "runner": "codex",
        "source": "codex exec --json events",
        "summary": {
            "commands": len(commands),
            "failed_commands": len(failed),
            "declined_commands": len(declined),
            "file_changes": len(changes),
            "failed_file_changes": len(failed_changes),
            "outside_workspace_changes": len(outside),
            "web_searches": len(searches),
            "pages_opened": len(pages),
            "mcp_calls": len(mcp_calls),
            "collab_calls": collab_calls,
            "agent_messages": messages,
            "errors": len(errors),
        },
        "commands": commands[:MAX_COMMANDS],
        "commands_truncated": len(commands) > MAX_COMMANDS,
        "failed_commands": failed[:MAX_COMMANDS],
        "declined_commands": declined[:MAX_COMMANDS],
        "file_changes": changes[:MAX_CHANGES],
        "outside_workspace": outside,
        "web": {"searches": searches[:MAX_WEB], "pages": pages[:MAX_WEB], "find_in_page": finds[:MAX_WEB]},
        "mcp_calls": mcp_calls[:MAX_COMMANDS],
        "todo": todo,
        "errors": errors,
        "usage": usage,
    }


def audit_final_json(runner: str, raw_stdout: str) -> dict[str, Any]:
    obj = json_object(raw_stdout)
    if not obj:
        return {"supported": False, "runner": runner, "reason": "no JSON result to inspect"}
    denials = obj.get("permission_denials") if isinstance(obj.get("permission_denials"), list) else []
    return {
        "supported": "partial",
        "runner": runner,
        "source": "final JSON result",
        "summary": {
            "turns": obj.get("num_turns"),
            "permission_denials": len(denials),
            "cost_usd": obj.get("total_cost_usd"),
        },
        "permission_denials": [
            {
                "tool": denial.get("tool_name"),
                "input": truncate(json.dumps(denial.get("tool_input"), ensure_ascii=False), TEXT_LIMIT),
            }
            for denial in denials[:100]
            if isinstance(denial, dict)
        ],
        "note": "This output has no per-tool events, so commands, edits and web pages are not listed; check the CLI's own session log.",
    }


def audit_output(runner: str, raw_stdout: str, workspace: Path | None) -> dict[str, Any]:
    if runner == "codex":
        return audit_codex(json_events(raw_stdout), workspace)
    if runner in ("claude", "workbuddy"):
        return audit_final_json(runner, raw_stdout)
    return {
        "supported": False,
        "runner": runner,
        "reason": "this runner's output is not parsed for tool-level events; check its own session log",
    }


def audit_line(audit: dict[str, Any] | None) -> str:
    if not audit:
        return "not available"
    if audit.get("supported") is False:
        return f"not supported for {audit.get('runner')}: {audit.get('reason')}"
    summary = audit.get("summary") or {}
    if audit.get("supported") == "partial":
        return f"partial ({audit.get('source')}): {summary.get('permission_denials', 0)} permission denials, {summary.get('turns')} turns"
    return (
        f"{summary.get('commands', 0)} commands ({summary.get('failed_commands', 0)} failed, "
        f"{summary.get('declined_commands', 0)} declined); {summary.get('file_changes', 0)} file changes "
        f"({summary.get('outside_workspace_changes', 0)} outside the workspace); "
        f"{summary.get('web_searches', 0)} web searches, {summary.get('pages_opened', 0)} pages opened; "
        f"{summary.get('errors', 0)} errors"
    )
