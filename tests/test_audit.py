"""Audit summaries for delegated runs (Codex fully, Claude partially, others honestly unsupported)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from support import CliTestCase, audit


def codex_events(workspace: Path) -> list[dict]:
    outside = str(workspace.parent / "elsewhere.txt")
    return [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "pytest", "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "pytest", "exit_code": 0, "status": "completed"}},
        {"type": "item.started", "item": {"id": "c2", "type": "command_execution", "command": "sleep 99", "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "c3", "type": "command_execution", "command": "curl x", "status": "declined"}},
        {"type": "item.completed", "item": {"id": "c4", "type": "command_execution", "command": "make", "exit_code": 2, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "f1", "type": "file_change", "status": "completed",
                                            "changes": [{"path": "a.py", "kind": "update"}, {"path": outside, "kind": "add"}]}},
        {"type": "item.completed", "item": {"id": "f2", "type": "file_change", "status": "failed", "changes": [{"path": "b.py", "kind": "delete"}]}},
        {"type": "item.completed", "item": {"id": "w1", "type": "web_search", "query": "q1", "action": {"type": "search", "queries": ["q1", "q2", "q1"]}}},
        {"type": "item.completed", "item": {"id": "w2", "type": "web_search", "query": "q3"}},
        {"type": "item.completed", "item": {"id": "w3", "type": "web_search", "action": {"type": "open_page", "url": "https://example.org"}}},
        {"type": "item.completed", "item": {"id": "w4", "type": "web_search", "action": {"type": "find_in_page", "url": "https://example.org", "pattern": "x"}}},
        {"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call", "server": "s", "tool": "t", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "k1", "type": "collab_tool_call"}},
        {"type": "item.completed", "item": {"id": "td", "type": "todo_list", "items": [{"text": "a", "completed": True}, {"text": "b", "completed": False}]}},
        {"type": "item.completed", "item": {"id": "e1", "type": "error", "message": "warn"}},
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"type": "turn.failed", "error": {"message": "quota"}},
        {"type": "error", "message": "stream broke"},
    ]


class AuditUnitTests(CliTestCase):
    def test_codex_summary_covers_commands_files_web_and_errors(self) -> None:
        report = audit.audit_codex(codex_events(self.workspace), self.workspace)
        summary = report["summary"]
        self.assertIs(report["supported"], True)
        self.assertEqual(summary["commands"], 4)
        self.assertEqual(summary["failed_commands"], 1)
        self.assertEqual(summary["declined_commands"], 1)
        statuses = {item["command"]: item["status"] for item in report["commands"]}
        self.assertEqual(statuses, {"pytest": "completed", "sleep 99": "incomplete", "curl x": "declined", "make": "failed"})
        self.assertEqual(summary["file_changes"], 3)
        self.assertEqual(summary["failed_file_changes"], 1)
        self.assertEqual(report["outside_workspace"], [str(self.workspace.parent / "elsewhere.txt")])
        self.assertEqual(report["web"]["searches"], ["q1", "q2", "q3"])
        self.assertEqual(report["web"]["pages"], ["https://example.org"])
        self.assertEqual(report["web"]["find_in_page"], [{"url": "https://example.org", "pattern": "x"}])
        self.assertEqual((summary["mcp_calls"], summary["collab_calls"]), (1, 1))
        self.assertEqual(report["todo"], {"total": 2, "completed": 1})
        self.assertEqual(sorted(report["errors"]), ["quota", "stream broke", "warn"])
        self.assertEqual(report["usage"], {"input_tokens": 8, "output_tokens": 4})
        self.assertIn("4 commands (1 failed, 1 declined)", audit.audit_line(report))

    def test_real_sample_shape_web_search_only(self) -> None:
        # Shape seen in real `codex exec --json` research runs: started items carry no queries yet.
        events = [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.started", "item": {"id": "ws_1", "type": "web_search", "query": "", "action": {"type": "other"}}},
            {"type": "item.completed", "item": {"id": "ws_1", "type": "web_search", "query": "news today", "action": {"type": "search", "queries": ["news today"]}, "results": [{}]}},
            {"type": "item.completed", "item": {"id": "ws_2", "type": "web_search", "query": "", "action": {"type": "open_page", "url": "https://news.example"}, "results": []}},
            {"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "summary"}},
        ]
        report = audit.audit_codex(events, None)
        self.assertEqual(report["web"]["searches"], ["news today"])
        self.assertEqual(report["web"]["pages"], ["https://news.example"])
        self.assertEqual(report["summary"]["commands"], 0)

    def test_claude_partial_and_other_runners_unsupported(self) -> None:
        raw = json.dumps({"session_id": "s", "result": "ok", "num_turns": 2, "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}]})
        partial = audit.audit_output("claude", raw, None)
        self.assertEqual(partial["supported"], "partial")
        self.assertEqual(partial["permission_denials"][0]["tool"], "Bash")
        self.assertIn("partial", audit.audit_line(partial))
        for runner in ("opencode", "kimi", "dsh", "grok", "pi", "antigravity"):
            report = audit.audit_output(runner, "{}", None)
            self.assertIs(report["supported"], False)
            self.assertIn("not supported", audit.audit_line(report))


class AuditCliTests(CliTestCase):
    def test_run_records_audit_and_audit_command_reads_it(self) -> None:
        self.onboard_all()
        proc = self.run_task("codex", extra_env={"FAKE_CODEX_AUDIT": "1"})
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("audit=3 commands (1 failed, 1 declined)", proc.stdout)
        result = self.result_of(proc)
        summary = result["audit"]["summary"]
        self.assertEqual((summary["commands"], summary["declined_commands"], summary["failed_commands"]), (3, 1, 1))
        self.assertEqual(summary["outside_workspace_changes"], 1)
        self.assertEqual(result["audit"]["web"]["searches"], ["codex web search", "codex docs"])
        self.assertEqual(result["audit"]["web"]["pages"], ["https://example.org/a"])

        stored = self.json_of(self.run_cli("audit", "latest", "--json"))
        self.assertIn("raw events were not kept", stored["source"])
        self.assertEqual(stored["audit"]["summary"], summary)
        text = self.run_cli("audit")
        self.assertEqual(text.returncode, 0, msg=text.stderr)
        self.assertIn("OUTSIDE WORKSPACE", text.stdout)
        self.assertIn("page_opened=https://example.org/a", text.stdout)

        kept = self.result_of(self.run_task("codex", "read", None, {"FAKE_CODEX_AUDIT": "1"}, "30", "--keep-raw"))
        recomputed = self.json_of(self.run_cli("audit", kept["run_id"], "--json"))
        self.assertIn("recomputed", recomputed["source"])
        self.assertEqual(recomputed["audit"]["summary"]["commands"], 3)

    def test_audit_events_file_plain_and_gzip(self) -> None:
        events = self.root / "events.jsonl"
        events.write_text("\n".join(json.dumps(event) for event in codex_events(self.workspace)) + "\nnot json\n", encoding="utf-8")
        payload = self.json_of(self.run_cli("audit", str(events), "--workspace", str(self.workspace), "--json"))
        self.assertEqual(payload["audit"]["summary"]["commands"], 4)
        self.assertEqual(payload["audit"]["summary"]["outside_workspace_changes"], 1)
        packed = self.root / "events.jsonl.gz"
        with gzip.open(packed, "wt", encoding="utf-8") as handle:
            handle.write(events.read_text(encoding="utf-8"))
        again = self.json_of(self.run_cli("audit", str(packed), "--json"))
        self.assertEqual(again["audit"]["summary"]["web_searches"], 3)

    def test_claude_run_reports_permission_denials(self) -> None:
        self.onboard_all()
        result = self.result_of(self.run_task("claude", extra_env={"FAKE_CLAUDE_DENIALS": "1"}))
        self.assertEqual(result["audit"]["supported"], "partial")
        self.assertEqual(result["audit"]["summary"]["permission_denials"], 1)
