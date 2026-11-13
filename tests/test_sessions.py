"""Session search and export, migrated from upstream oil-delegate, plus link/junction regressions."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path
from unittest import mock

from support import IS_WINDOWS, P, RUNNERS, CliTestCase, posix_tail, sessions


class SessionSearchTests(CliTestCase):
    def test_kimi_official_list_and_root_override_without_onboarding(self) -> None:
        fixture = self.root / "kimi-sessions.json"
        fixture.write_text(json.dumps([
            {"id": "kimi-1", "title": "项目 Plan", "workDir": "/fixture/project", "updatedAt": "1793664000123"},
            {"id": "kimi-2", "title": "Unrelated", "workDir": "/fixture/other", "updatedAt": "1793665000123", "firstPrompt": "项目 Plan"},
        ]), encoding="utf-8")
        log = self.root / "calls.jsonl"
        record = self.root / "record.json"
        payload = self.sessions_payload("--runner", "kimi-code", "--query", "项目 pLAN", extra_env={
            "FAKE_KIMI_SESSIONS": str(fixture), "FAKE_CALL_LOG": str(log), "FAKE_RECORD": str(record)})
        row, = payload["sessions"]
        self.assertEqual(set(row), {"runner", "session_id", "title", "workspace", "updated_at", "resume_command", "resume_commands", "source", "resumable"})
        self.assertIs(row["resumable"], True)
        self.assertEqual((row["runner"], row["session_id"], row["title"], row["workspace"], row["source"]),
                         ("kimi", "kimi-1", "项目 Plan", "/fixture/project", "official"))
        self.assertEqual(row["updated_at"], "2026-11-03T00:00:00.123000+00:00")
        self.assertEqual(posix_tail(row["resume_commands"]["posix"], 2), ["-S", "kimi-1"])
        self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["argv"], ["session", "list", "--all", "--json"])
        self.assertEqual(self.record_of()["kimi_home"], str(self.root / "kimi-home"))
        self.assertEqual((payload["unsupported"], payload["errors"], payload["skipped"]), ([], {}, {}))
        self.assertFalse(self.state_dir.exists())

    def test_kimi_null_titles_and_bad_rows_keep_valid_sessions(self) -> None:
        bad_rows = [None, [], {"title": "Missing ID"}, {"id": None, "title": "Null ID"}, {"id": 42, "title": "Numeric ID"},
                    {"id": "", "title": "Empty ID"}, {"id": "bad-title", "title": False},
                    {"id": "bad-time", "title": "Invalid time", "updatedAt": "invalid"}, {"id": "bad-cwd", "title": "Invalid workspace", "workDir": []}]
        fixture = self.root / "kimi-mixed.json"
        fixture.write_text(json.dumps([bad_rows[0], {"id": "named", "title": "Topic", "workDir": "/fixture/kimi", "updatedAt": "1793664000000"},
                                       {"id": "unnamed", "title": None, "updatedAt": "1793665000000"}, *bad_rows[1:], {"id": "missing-title"}]), encoding="utf-8")
        env = {"FAKE_KIMI_SESSIONS": str(fixture)}
        payload = self.sessions_payload("--runner", "kimi", extra_env=env)
        rows = {row["session_id"]: row for row in payload["sessions"]}
        self.assertEqual(set(rows), {"named", "unnamed", "missing-title"})
        self.assertEqual((rows["unnamed"]["title"], rows["missing-title"]["title"]), ("", ""))
        self.assertEqual((payload["errors"], payload["skipped"]), ({}, {"kimi": len(bad_rows)}))
        filtered = self.sessions_payload("--runner", "kimi", "--query", "topic", "--limit", "1", extra_env=env)
        self.assertEqual([row["session_id"] for row in filtered["sessions"]], ["named"])

    def test_codex_renames_use_last_line_and_read_only_matching_headers(self) -> None:
        self.write_codex_index([
            {"id": "same-id", "thread_name": "Old title", "updated_at": "2026-11-03T12:00:00Z"},
            {"id": "other-id", "thread_name": "Unrelated", "updated_at": "2026-11-03T13:00:00Z"},
            {"id": "same-id", "thread_name": "New title", "updated_at": "2026-11-03T11:00:00Z"},
        ])
        rollout = self.write_rollout("same-id", "/fixture/codex")
        self.write_rollout("other-id", "/fixture/unrelated").write_bytes(b"invalid header, must not be opened")
        before = rollout.read_bytes()
        payload = self.sessions_payload("--runner", "codex", "--query", "NEW TITLE")
        row, = payload["sessions"]
        self.assertEqual((row["session_id"], row["title"], row["workspace"], row["source"]), ("same-id", "New title", "/fixture/codex", "local-store"))
        self.assertEqual(payload["errors"], {})
        self.assertEqual(posix_tail(row["resume_commands"]["posix"], 2), ["resume", "same-id"])
        self.assertEqual(self.sessions_payload("--runner", "codex", "--query", "Old title")["sessions"], [])
        self.assertEqual(rollout.read_bytes(), before)

    def test_codex_hydrates_only_returned_results_after_limit(self) -> None:
        self.write_codex_index([{"id": "first", "thread_name": "Topic", "updated_at": "2026-11-03T12:00:00Z"},
                                {"id": "second", "thread_name": "Topic", "updated_at": "2026-11-03T11:00:00Z"}])
        self.write_rollout("first", "/fixture/first")
        self.write_rollout("second", "unused").write_bytes(b"unread invalid header")
        payload = self.sessions_payload("--runner", "codex", "--limit", "1")
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["first"])
        self.assertEqual(payload["errors"], {})

    def test_codex_bad_index_rows_do_not_discard_valid_rows(self) -> None:
        path = self.write_codex_index([{"id": "renamed", "thread_name": "Old title", "updated_at": None}, None, [], {"thread_name": "Missing ID"},
                                       {"id": None, "thread_name": "Null ID"}, {"id": 42, "thread_name": "Numeric ID"}, {"id": "", "thread_name": "Empty ID"},
                                       {"id": "bad-title", "thread_name": []}, {"id": "bad-time", "thread_name": "Invalid time", "updated_at": "invalid"}])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("invalid JSON\n\n")
            handle.write(json.dumps({"id": "renamed", "thread_name": "New title", "updated_at": None}) + "\n")
            handle.write(json.dumps({"id": "unnamed", "thread_name": None, "updated_at": None}) + "\n")
        payload = self.sessions_payload("--runner", "codex")
        self.assertEqual({row["session_id"]: row["title"] for row in payload["sessions"]}, {"renamed": "New title", "unnamed": ""})
        self.assertEqual((payload["errors"], payload["skipped"]), ({}, {"codex": 9}))

    def test_codex_without_rollout_or_archived_has_null_workspace(self) -> None:
        self.write_codex_index([{"id": sid, "thread_name": sid, "updated_at": None} for sid in ("active", "archived", "deleted")])
        self.write_rollout("active", "/fixture/active")
        self.write_rollout("deleted", "/fixture/deleted").unlink()
        archive = self.root / "codex-home" / "archived_sessions" / "rollout-any-archived.jsonl.zst"
        archive.parent.mkdir()
        archive.write_bytes(b"archive must not be opened or decompressed")
        payload = self.sessions_payload("--runner", "codex")
        self.assertEqual({row["session_id"]: row["workspace"] for row in payload["sessions"]}, {"active": "/fixture/active", "archived": None, "deleted": None})
        self.assertEqual((payload["errors"], payload["skipped"]), ({}, {}))

    def test_submillisecond_order_and_default_local_root(self) -> None:
        path = self.write_codex_index([{"id": "a-older", "thread_name": "Topic", "updated_at": "2026-11-03T12:00:00.000001Z"},
                                       {"id": "z-newer", "thread_name": "Topic", "updated_at": "2026-11-03T12:00:00.000002Z"}])
        default_root = self.home / ".codex"
        default_root.mkdir(parents=True)
        path.replace(default_root / path.name)
        proc = self.run_cli("sessions", "--runner", "codex", "--json", remove_env=["CODEX_HOME"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual([row["session_id"] for row in self.json_of(proc)["sessions"]], ["z-newer", "a-older"])

    def test_codex_data_through_junction_or_symlink(self) -> None:
        """Regression: CLI data moved to another drive via a junction/symlink keeps working."""
        moved = self.root / "other-drive" / "history" / "sessions"
        self.write_rollout("moved", "/fixture/moved", moved)
        self.write_codex_index([{"id": "moved", "thread_name": "Moved", "updated_at": None}])
        kind = self.make_dir_link(self.root / "codex-home" / "sessions", moved)
        row, = self.sessions_payload("--runner", "codex")["sessions"]
        self.assertEqual(row["workspace"], "/fixture/moved", msg=kind)
        whole = self.root / "codex-link"
        self.make_dir_link(whole, self.root / "codex-home")
        payload = self.sessions_payload("--runner", "codex", extra_env={"CODEX_HOME": str(whole)})
        self.assertEqual([(item["session_id"], item["workspace"]) for item in payload["sessions"]], [("moved", "/fixture/moved")])
        self.assertEqual(payload["errors"], {})

    def test_antigravity_both_roots_dedup_uri_and_anti_alias(self) -> None:
        for dirname, rows in (
            ("anti-cli-home", [("shared", "Old", '["file:///fixture/old"]', "2026-11-03T01:00:00Z")]),
            ("anti-home", [("shared", "New", '["https://example.invalid", "file://localhost/fixture/a%20b"]', "2026-11-03T03:00:00+01:00"),
                           ("unique", "Extra", "[]", 1793664000000)]),
        ):
            self.write_anti_db(dirname, rows).close()
        payload = self.sessions_payload("--runner", "ANTI")
        self.assertEqual((payload["unsupported"], payload["errors"]), ([], {}))
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["shared", "unique"])
        row = payload["sessions"][0]
        self.assertEqual((row["runner"], row["title"], row["workspace"], row["updated_at"], row["source"]),
                         ("antigravity", "New", "/fixture/a b", "2026-11-03T02:00:00.000000+00:00", "desktop-store"))
        self.assertIs(row["resumable"], False)
        self.assertIsNone(row["resume_command"])

    def test_antigravity_cli_is_resumable_desktop_is_not(self) -> None:
        self.write_anti_db("anti-cli-home", [("cli", "CLI", "[]", None)]).close()
        self.write_anti_db("anti-home", [("desktop", "IDE", "[]", None)]).close()
        rows = {row["session_id"]: row for row in self.sessions_payload("--runner", "agy")["sessions"]}
        self.assertIs(rows["cli"]["resumable"], True)
        self.assertEqual(rows["cli"]["source"], "local-store")
        self.assertEqual(posix_tail(rows["cli"]["resume_commands"]["posix"], 2), ["--conversation", "cli"])
        self.assertEqual((rows["desktop"]["resumable"], rows["desktop"]["resume_command"], rows["desktop"]["source"]), (False, None, "desktop-store"))

    def test_antigravity_uses_immutable_snapshot_without_writes(self) -> None:
        dirname = "anti cli #" if IS_WINDOWS else "anti cli ?#"
        connection = self.write_anti_db(dirname, [("checkpointed", "Saved", "[]", 1793664000)], wal=True)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO conversation_summaries VALUES ('wal-only', 'Recent', '[]', 1793665000, 'private')")
        connection.commit()
        root = self.root / dirname
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.iterdir()}
        connect = sqlite3.connect
        uris = []

        def immutable_connect(database: str, **kwargs):
            uris.append((database, kwargs))
            conn = connect(database, **kwargs)
            conn.set_authorizer(lambda op, table, column, *_: sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_READ and column == "body" else sqlite3.SQLITE_OK)
            return conn

        with self.patched_env(**{P + "ANTIGRAVITY_CLI_HOME": str(root)}), mock.patch.object(sessions.sqlite3, "connect", side_effect=immutable_connect):
            errors: dict = {}
            skipped: dict = {}
            rows = sessions.antigravity_sessions(errors, skipped)
        self.assertEqual((errors, skipped), ({}, {}))
        self.assertEqual([row["session_id"] for row in rows], ["checkpointed"])
        self.assertEqual(uris, [((root / "conversation_summaries.db").as_uri() + "?immutable=1", {"uri": True})])
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.iterdir()}, before)

    def test_antigravity_bad_rows_keep_valid_rows_from_both_databases(self) -> None:
        self.write_anti_db("anti-cli-home", [("first", "First topic", '["file:///fixture/first"]', 1793664000), (None, "Null ID", "[]", 1793664000),
                                             ("", "Empty ID", "[]", 1793664000), ("bad-json", "Invalid JSON", "not JSON", 1793664000), ("unnamed", None, "[]", 1793664000)]).close()
        self.write_anti_db("anti-home", [("bad-list", "Invalid URI list", "{}", 1793664000), ("bad-uri", "Invalid URI", "[42]", 1793664000),
                                         ("bad-time", "Invalid time", "[]", "invalid"), ("last", "Last topic", '["file:///fixture/last"]', 1793665000)]).close()
        payload = self.sessions_payload("--runner", "anti")
        self.assertEqual({row["session_id"]: row["title"] for row in payload["sessions"]}, {"first": "First topic", "last": "Last topic", "unnamed": ""})
        self.assertEqual((payload["errors"], payload["skipped"]), ({}, {"antigravity": 6}))

    def test_one_antigravity_database_failure_keeps_other_database(self) -> None:
        bad = self.root / "anti-cli-home" / "conversation_summaries.db"
        bad.parent.mkdir()
        bad.write_bytes(b"not a database")
        self.write_anti_db("anti-home", [("valid", "Good", "[]", 1793664000)]).close()
        payload = self.sessions_payload("--runner", "anti")
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["valid"])
        self.assertIn("antigravity-cli", payload["errors"]["antigravity"])

    def test_source_failures_do_not_block_other_runners(self) -> None:
        index = self.write_codex_index([])
        index.unlink()
        index.mkdir()
        self.write_anti_db("anti-home", [("valid", "Good", "[]", 1793664000)]).close()
        payload = self.sessions_payload(extra_env={"FAKE_KIMI_EXIT": "2"})
        self.assertEqual([row["runner"] for row in payload["sessions"]], ["antigravity"])
        self.assertEqual(set(payload["errors"]), {"codex", "kimi"})
        self.assertIn("exited 2", payload["errors"]["kimi"])
        self.assertEqual(payload["skipped"], {})
        self.assertEqual(payload["unsupported"], ["workbuddy", "opencode", "dsh", "grok", "pi"])
        self.assertFalse(self.state_dir.exists())

    def test_exact_title_first_then_updated_at_across_runners(self) -> None:
        self.write_codex_index([{"id": "exact-old", "thread_name": "Topic", "updated_at": "2026-11-01T00:00:00Z"}])
        self.write_rollout("exact-old", "/fixture/codex")
        self.write_anti_db("anti-cli-home", [("partial-new", "A Topic", "[]", "2026-11-03T12:00:00Z")]).close()
        fixture = self.root / "kimi.json"
        fixture.write_text(json.dumps([{"id": "exact-new", "title": "TOPIC", "workDir": "/fixture/kimi", "updatedAt": "1793664000000"},
                                       {"id": "partial-old", "title": "Topic extra", "workDir": None, "updatedAt": "1793577600000"}]), encoding="utf-8")
        payload = self.sessions_payload("--query", "topic", "--limit", "3", extra_env={"FAKE_KIMI_SESSIONS": str(fixture)})
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["exact-new", "exact-old", "partial-new"])
        self.assertEqual(payload["errors"], {})

    def test_skips_symlinked_rollouts_and_directories_inside_a_root(self) -> None:
        self.write_codex_index([{"id": "linked", "thread_name": "Linked", "updated_at": None}])
        external = self.root / "outside"
        external.mkdir()
        target = external / "rollout-any-linked.jsonl"
        target.write_bytes(b"must not be read")
        sessions_dir = self.root / "codex-home" / "sessions"
        sessions_dir.mkdir()
        self.make_symlink(sessions_dir / target.name, target)
        self.make_symlink(sessions_dir / "linked-directory", external, directory=True)
        payload = self.sessions_payload("--runner", "codex")
        self.assertIsNone(payload["sessions"][0]["workspace"])
        self.assertEqual(payload["errors"], {})

    def test_cloud_roots_are_rejected(self) -> None:
        for root in (self.root / "Library" / "CloudStorage" / "provider", self.root / "Screen Studio Archive"):
            with self.subTest(root=root):
                payload = self.sessions_payload("--runner", "codex", extra_env={"CODEX_HOME": str(root)})
                self.assertEqual(payload["sessions"], [])
                self.assertIn("codex", payload["errors"])

    def test_invalid_kimi_list_reports_source_error(self) -> None:
        fixture = self.root / "invalid-kimi.json"
        for content in ("not JSON", '{"sessions": "bad"}'):
            with self.subTest(content=content):
                fixture.write_text(content, encoding="utf-8")
                payload = self.sessions_payload("--runner", "kimi", extra_env={"FAKE_KIMI_SESSIONS": str(fixture)})
                self.assertEqual((payload["sessions"], payload["skipped"]), ([], {}))
                self.assertIn("kimi", payload["errors"])

    def test_missing_local_roots_are_empty(self) -> None:
        for runner in ("codex", "anti", "claude"):
            with self.subTest(runner=runner):
                payload = self.sessions_payload("--runner", runner)
                self.assertEqual((payload["sessions"], payload["errors"], payload["unsupported"]), ([], {}, []))

    def test_unsupported_runner_does_not_start_cli(self) -> None:
        log = self.root / "calls.jsonl"
        for runner, canonical in (("wb", "workbuddy"), ("opencode", "opencode"), ("dsh", "dsh"), ("grok-build", "grok"), ("pi", "pi")):
            with self.subTest(runner=runner):
                payload = self.sessions_payload("--runner", runner, extra_env={"FAKE_CALL_LOG": str(log)})
                self.assertEqual((payload["sessions"], payload["errors"], payload["unsupported"]), ([], {}, [canonical]))
        self.assertFalse(log.exists())

    def test_invalid_runner_and_limit_use_json_error_contract(self) -> None:
        for args in (("--runner", "unknown"), ("--limit", "0"), ("--limit", "-1")):
            with self.subTest(args=args):
                proc = self.run_cli("sessions", *args, "--json")
                self.assertEqual(proc.returncode, 1)
                payload = self.json_of(proc)
                self.assertEqual((payload["contract_version"], payload["status"]), (1, "ERROR"))

    def test_plain_output_is_readable(self) -> None:
        self.write_codex_index([{"id": "one", "thread_name": "Title", "updated_at": None}])
        proc = self.run_cli("sessions", "--runner", "codex")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("codex\tone\tTitle\t", proc.stdout)


class ClaudeDesktopTests(CliTestCase):
    def desktop_payload(self) -> dict:
        code, output = self.call_main("sessions", "--runner", "cc", "--json")
        self.assertEqual(code, 0)
        return json.loads(output)

    def test_desktop_ids_archived_and_transcript_existence(self) -> None:
        self.write_claude_metadata("available", cwd="/fixture/work space")
        self.write_claude_metadata("missing")
        self.write_claude_metadata("archived", isArchived=True)
        bad = self.write_claude_metadata("bad", cliSessionId=None)
        transcript = self.write_claude_jsonl("available", [], "-fixture-work-space")
        transcript.write_bytes(b"\xffnot-jsonl")
        before = (transcript.read_bytes(), transcript.stat().st_mtime_ns, bad.read_bytes())
        payload = self.desktop_payload()
        rows = {row["session_id"]: row for row in payload["sessions"]}
        self.assertEqual(set(rows), {"available", "missing"})
        self.assertEqual((rows["available"]["source"], rows["available"]["workspace"]), ("desktop-store", "/fixture/work space"))
        self.assertEqual(rows["available"]["updated_at"], "2026-11-03T00:00:00.123000+00:00")
        self.assertEqual(posix_tail(rows["available"]["resume_commands"]["posix"], 2), ["--resume", "available"])
        self.assertIs(rows["available"]["resumable"], True)
        self.assertEqual((rows["missing"]["resumable"], rows["missing"]["resume_command"]), (False, None))
        self.assertEqual((payload["skipped"], payload["errors"], payload["unsupported"]), ({"claude": 1}, {}, []))
        self.assertEqual((transcript.read_bytes(), transcript.stat().st_mtime_ns, bad.read_bytes()), before)
        self.assertFalse(self.state_dir.exists())

    def test_missing_desktop_folder_is_quietly_empty_on_every_platform(self) -> None:
        with self.patched_env(**{P + "CLAUDE_DESKTOP_HOME": str(self.root / "no-desktop")}):
            self.assertEqual(sessions.claude_sessions({}), [])
        self.assertTrue(str(sessions.claude_desktop_default()).endswith("claude-code-sessions"))

    def test_skips_symlinks_cloud_directories_and_placeholders(self) -> None:
        self.write_claude_metadata("good")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "local_bad.json").write_bytes(b"not JSON")
        bucket = self.root / "claude-desktop" / "account" / "bucket"
        self.make_symlink(bucket / "local_link.json", outside / "local_bad.json")
        self.make_symlink(self.root / "claude-desktop" / "linked-account", outside, directory=True)
        (self.root / "claude-desktop" / "CloudStorage").mkdir()
        self.assertEqual([row["session_id"] for row in self.desktop_payload()["sessions"]], ["good"])
        with mock.patch.object(Path, "lstat", return_value=mock.Mock(st_flags=0x40000000)), self.assertRaisesRegex(sessions.DelegateError, "placeholder"):
            sessions.data_root("", self.root / "placeholder")


class ExportTests(CliTestCase):
    def test_antigravity_both_stores_text_fields_only_and_recent_turns(self) -> None:
        for dirname in ("anti-cli-home", "anti-home"):
            with self.subTest(dirname=dirname):
                sid = dirname
                self.write_anti_db(dirname, [(sid, "交接标题", '["file:///fixture/a%20b"]', 1793664020)]).close()
                source = self.write_anti_conversation(dirname, sid, [
                    (0, 14, self.anti_step(14, "旧用户", 1793664000)), (1, 15, self.anti_step(15, "旧回复", 1793664001)),
                    (2, 132, b"\xffprivate tool output"), (3, 101, b"private checkpoint"), (4, 23, b"private summary"),
                    (5, 15, self.proto((1, 15), (20, self.proto((3, "thinking only"), (7, "tools only"))))),
                    (6, 14, self.anti_step(14, "新用户", 1793664010)), (7, 15, self.anti_step(15, "新回复", 1793664011)),
                ])
                before = (source.read_bytes(), source.stat().st_mtime_ns, sorted(path.name for path in source.parent.iterdir()))
                out = self.root / f"{sid}.md"
                payload = self.export_payload("ANTI", sid, out, "--max-turns", "2")
                self.assertEqual(payload, {"contract_version": 1, "status": "OK", "path": str(out), "title": "交接标题", "workspace": "/fixture/a b",
                                           "turns": 2, "omitted_turns": 2, "runner": "antigravity", "session_id": sid})
                text = out.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# 交接标题\n\n> 已省略较早的 2 轮"))
                self.assertLess(text.index("## 用户"), text.index("## Agent"))
                self.assertEqual((text.count("新用户"), text.count("新回复")), (1, 1))
                self.assertIn("2026-11-03T00:00:10.123000+00:00", text)
                for private in ("旧用户", "旧回复", "private", "thinking", "tools", "duplicate", "unknown", "image"):
                    self.assertNotIn(private, text)
                self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns, sorted(path.name for path in source.parent.iterdir())), before)
        self.assertFalse(self.state_dir.exists())

    def test_antigravity_immutable_wal_snapshot(self) -> None:
        path = self.write_anti_conversation("anti-home", "wal", [(0, 14, self.anti_step(14, "saved", 1793664000))])
        connection = sqlite3.connect(path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO steps VALUES(1,15,0,?)", (self.anti_step(15, "wal only", 1793664001),))
        connection.commit()
        before = {item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in path.parent.iterdir()}
        out = self.root / "wal.md"
        payload = self.export_payload("agy", "wal", out)
        self.assertEqual((payload["turns"], payload["omitted_turns"]), (1, 0))
        self.assertNotIn("wal only", out.read_text(encoding="utf-8"))
        self.assertEqual({item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in path.parent.iterdir()}, before)

    def test_antigravity_other_store_failure_does_not_block_local_source(self) -> None:
        self.write_anti_conversation("anti-home", "local", [(0, 14, self.anti_step(14, "local user", 1793664000))])
        out = self.root / "local.md"
        proc = self.run_cli("sessions", "export", "--runner", "anti", "--session-id", "local", "--out", str(out), "--json",
                            extra_env={P + "ANTIGRAVITY_CLI_HOME": str(self.root / "Library" / "CloudStorage" / "provider")})
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(self.json_of(proc)["turns"], 1)
        self.assertIn("local user", out.read_text(encoding="utf-8"))

    def test_antigravity_unknown_format_rejected_and_binary_text_skipped(self) -> None:
        path = self.write_anti_conversation("anti-home", "format", [(0, 14, self.anti_step(14, "text", 1793664000))])
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE steps SET step_format=1")
            connection.commit()
        out = self.root / "format.md"
        proc = self.run_cli("sessions", "export", "--runner", "anti", "--session-id", "format", "--out", str(out), "--json")
        self.assertEqual((proc.returncode, self.json_of(proc)["status"]), (1, "ERROR"))
        self.assertIn("step_format", self.json_of(proc)["error"])
        self.assertFalse(out.exists())
        self.write_anti_conversation("anti-home", "binary", [(0, 14, self.anti_step(14, b"\xff\x00", 1793664000))])
        self.assertEqual(self.export_payload("anti", "binary", out)["turns"], 0)

    def test_claude_jsonl_text_filter_chronology_titles_and_default_limit(self) -> None:
        rows = [{"type": "ai-title", "aiTitle": "AI 标题"}, {"type": "custom-title", "customTitle": "用户标题"},
                {"type": "system", "message": {"content": "system secret"}},
                {"type": "assistant", "isSidechain": True, "message": {"content": "sidechain secret"}},
                {"type": "user", "isMeta": True, "message": {"content": "meta secret"}}]
        for index in reversed(range(32)):
            rows.append({"type": "user" if index % 2 == 0 else "assistant", "cwd": "/fixture/project", "uuid": f"message-{index}",
                         "timestamp": f"2026-11-03T00:00:{index:02d}Z",
                         "message": {"content": [{"type": "text", "text": f"正文 {index:02d}"}, {"type": "tool_use", "input": {"text": "tool secret"}},
                                                 {"type": "tool_result", "content": "result secret"}, {"type": "image", "source": {"data": "image secret"}},
                                                 {"type": "thinking", "thinking": "thinking secret"}]}})
        rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "tool only"}]}})
        source = self.write_claude_jsonl("cli", rows)
        before = (source.read_bytes(), source.stat().st_mtime_ns)
        out = self.root / "claude.md"
        result = self.export_payload("cc", "cli", out)
        self.assertEqual((result["title"], result["workspace"], result["turns"], result["omitted_turns"]), ("用户标题", "/fixture/project", 30, 2))
        text = out.read_text(encoding="utf-8")
        self.assertLess(text.index("正文 02"), text.index("正文 31"))
        for absent in ("正文 00", "正文 01", "secret", "tool only"):
            self.assertNotIn(absent, text)
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)

    def test_claude_desktop_uses_cli_id_and_metadata(self) -> None:
        self.write_claude_metadata("cli-id")
        self.write_claude_jsonl("cli-id", [{"type": "user", "message": {"content": "用户正文"}},
                                           {"type": "assistant", "message": {"content": [{"type": "text", "text": "最终回复"}]}}])
        out = self.root / "desktop.md"
        code, output = self.call_main("sessions", "export", "--runner", "claude", "--session-id", "cli-id", "--out", str(out), "--json")
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual((result["session_id"], result["title"], result["turns"]), ("cli-id", "桌面标题", 2))
        self.assertIn("最终回复", out.read_text(encoding="utf-8"))

    def test_force_and_plain_output(self) -> None:
        self.write_claude_jsonl("replace", [{"type": "user", "message": {"content": "message"}}])
        out = self.root / "handoff.md"
        out.write_text("keep me", encoding="utf-8")
        result = self.run_cli("sessions", "export", "--runner", "cc", "--session-id", "replace", "--out", str(out), "--json")
        self.assertEqual((result.returncode, self.json_of(result)["status"]), (1, "ERROR"))
        self.assertIn("--force", self.json_of(result)["error"])
        self.assertEqual(out.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(self.export_payload("cc", "replace", out, "--force")["turns"], 1)
        plain = self.run_cli("sessions", "export", "--runner", "cc", "--session-id", "replace", "--out", str(out), "--force")
        self.assertEqual(plain.returncode, 0, msg=plain.stderr)
        self.assertIn("turns=1", plain.stdout)
        self.assertNotIn("message", plain.stdout)

    def test_errors_are_json_and_do_not_create_output_or_start_cli(self) -> None:
        out = self.root / "error.md"
        log = self.root / "cli-calls.jsonl"
        cases = [(runner, "sid", []) for runner in RUNNERS if runner not in {"claude", "antigravity"}] + [
            ("unknown", "sid", []), ("cc", "missing", []), ("anti", "missing", []), ("cc", "../escape", []), ("anti", "a/b", []), ("cc", "sid", ["--max-turns", "0"])]
        for runner, sid, options in cases:
            with self.subTest(runner=runner, sid=sid, options=options):
                proc = self.run_cli("sessions", "export", "--runner", runner, "--session-id", sid, "--out", str(out), *options, "--json",
                                    extra_env={"FAKE_CALL_LOG": str(log)})
                self.assertEqual(proc.returncode, 1, msg=proc.stderr)
                payload = self.json_of(proc)
                self.assertEqual((payload["status"], payload["contract_version"]), ("ERROR", 1))
                self.assertTrue(payload["error"])
                self.assertFalse(out.exists())
        self.assertFalse(log.exists())

    def test_rejects_corrupt_payloads_jsonl_and_unsafe_paths(self) -> None:
        out = self.root / "error.md"
        for payload in (b"\x08\x80", b"\x00", b"\x9a\x01\xff", self.proto((1, 15), (19, b""))):
            path = self.write_anti_conversation("anti-home", "bad", [(0, 14, payload)])
            proc = self.run_cli("sessions", "export", "--runner", "anti", "--session-id", "bad", "--out", str(out), "--json")
            self.assertEqual((proc.returncode, self.json_of(proc)["status"]), (1, "ERROR"))
            self.assertFalse(out.exists())
            path.unlink()
        source = self.write_claude_jsonl("safe", [{"type": "user", "message": {"content": "safe text"}}])
        for destination in (source, source.parent / "new.md"):
            proc = self.run_cli("sessions", "export", "--runner", "cc", "--session-id", "safe", "--out", str(destination), "--force", "--json")
            self.assertEqual((proc.returncode, self.json_of(proc)["status"]), (1, "ERROR"))
            self.assertNotIn("#", source.read_text(encoding="utf-8"))
        source.write_bytes(b"not JSON\n")
        proc = self.run_cli("sessions", "export", "--runner", "cc", "--session-id", "safe", "--out", str(out), "--json")
        self.assertEqual((proc.returncode, self.json_of(proc)["status"]), (1, "ERROR"))
        self.assertFalse(out.exists())

    def test_rejects_symlinked_output(self) -> None:
        source = self.write_claude_jsonl("safe", [{"type": "user", "message": {"content": "safe text"}}])
        linked = self.root / "linked.md"
        self.make_symlink(linked, source)
        proc = self.run_cli("sessions", "export", "--runner", "cc", "--session-id", "safe", "--out", str(linked), "--force", "--json")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("#", source.read_text(encoding="utf-8"))
