"""Parse each runner's machine-readable output (adapted from upstream oil-delegate)."""
from __future__ import annotations

import json
from typing import Any


def json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def json_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def nested_session_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("sessionID", "sessionId", "session_id", "conversation_id", "thread_id"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    for key in ("part", "session", "final", "result", "init"):
        nested = nested_session_id(value.get(key))
        if nested:
            return nested
    return None


def error_message(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("message", "error", "reason", "content"):
            found = error_message(value.get(key))
            if found:
                return found
        found = error_message(value.get("data"))
        if found:
            return found
    return str(value)


def parse_runner_output(runner: str, raw_out: str, raw_err: str, exit_code: int, previous_session: str | None) -> dict[str, Any]:
    session_id = previous_session
    response = ""
    error: str | None = None
    protocol_ok = True

    if runner in ("claude", "workbuddy", "antigravity"):
        obj = json_object(raw_out)
        if not obj:
            protocol_ok = False
            error = "CLI did not return a JSON result"
        elif runner == "antigravity":
            session_id = obj.get("conversation_id") or session_id
            response = str(obj.get("response") or "")
            if obj.get("status") != "SUCCESS":
                error = error_message(obj.get("error") or obj.get("status"))
        else:
            session_id = obj.get("session_id") or session_id
            response = str(obj.get("result") or "")
            if obj.get("is_error"):
                error = response or "CLI reported an error"

    elif runner == "grok":
        obj = json_object(raw_out)
        if not obj:
            protocol_ok = False
            error = "CLI did not return a JSON result"
        else:
            session_id = obj.get("sessionId") or nested_session_id(obj) or session_id
            response = str(obj.get("text") or "")
            if obj.get("type") == "error":
                error = error_message(obj.get("message") or obj)
            elif obj.get("stopReason") in ("cancelled", "refusal", "error", "error_during_execution"):
                error = error_message(obj.get("message") or obj.get("stopReason"))
            protocol_ok = bool(session_id and (response or error))

    elif runner == "pi":
        settled = False
        for event in json_events(raw_out):
            event_type = event.get("type")
            if event_type == "session":
                session_id = event.get("id") or session_id
            elif event_type == "message_end":
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                reason = message.get("stopReason")
                error = None
                if reason in ("error", "aborted"):
                    error = error_message(message.get("errorMessage") or reason)
                response = "".join(
                    block["text"]
                    for block in message.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                )
            elif event_type == "agent_settled":
                settled = True
                if event.get("aborted"):
                    error = error or "Pi run was aborted"
        protocol_ok = bool(session_id and settled)

    elif runner == "kimi":
        chunks: list[str] = []
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            if event.get("type") == "session.resume_hint" and event.get("session_id"):
                session_id = str(event["session_id"])
            if event.get("role") == "assistant" and event.get("content"):
                content = event["content"]
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    chunks.extend(
                        str(block["text"])
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
                    )
            elif event.get("type") == "error" or event.get("role") == "error":
                error = error_message(event.get("content") or event.get("message") or event)
        response = "\n".join(chunks).strip()
        protocol_ok = bool(session_id and (chunks or error))

    elif runner == "codex":
        messages: list[str] = []
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    messages.append(str(item["text"]))
            elif event_type in ("turn.failed", "error"):
                error = error_message(event.get("error") or event.get("message")) or "Codex turn failed"
        response = messages[-1] if messages else ""
        protocol_ok = bool(session_id and (messages or error))

    elif runner == "opencode":
        chunks = []
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            event_type = event.get("type")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if event_type == "text" and part.get("text"):
                chunks.append(str(part["text"]))
            elif event_type == "error":
                error = error_message(event.get("error") or event.get("message") or event)
        response = "".join(chunks)
        protocol_ok = bool(session_id and (chunks or error))

    elif runner == "dsh":
        chunks = []
        final: dict[str, Any] | None = None
        turn_reason: Any = None
        for event in json_events(raw_out):
            session_id = nested_session_id(event) or session_id
            event_type = event.get("type")
            if event_type == "text" and event.get("text"):
                chunks.append(str(event["text"]))
            elif event_type == "status" and event.get("phase") == "turn_end":
                turn_reason = event.get("reason")
            elif event_type == "final":
                final = event
            elif event_type == "error":
                error = error_message(event.get("message") or event.get("error") or event)
        if final:
            response = str(final.get("text") or "")
            reason = final.get("reason") or turn_reason
            if reason is not None and (not isinstance(reason, dict) or reason.get("kind") != "completed"):
                error = error_message(reason) or "DSH final reason is not completed"
            elif reason is None and exit_code != 0 and not error:
                error = "DSH run did not complete"
        else:
            response = "".join(chunks)
            if not error:
                error = "DSH stream ended without a final event"
        protocol_ok = bool(session_id and final and not error)

    status = "completed" if exit_code == 0 and protocol_ok and not error and session_id and response.strip() else "failed"
    if status == "failed":
        if not error:
            if not protocol_ok:
                error = "Runner returned an invalid or incomplete response"
            elif not session_id:
                error = "Runner did not return a session ID"
            elif not response.strip():
                error = "Runner returned an empty response"
            elif exit_code != 0:
                error = f"Runner exited with code {exit_code}"
            else:
                error = "Runner failed"
        stderr_detail = raw_err.strip()[-2000:]
        if stderr_detail and error not in stderr_detail:
            error = f"{error}\n{stderr_detail}"
    return {"status": status, "session_id": session_id, "response": response, "error": error}
