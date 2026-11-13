"""Locations: resolution order, host inference, first-use confirmation, run records, prune."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from pathlib import Path
from unittest import mock

from support import P, CliTestCase, config, storage, toml_compat

CODEX_TOML = """
# user settings
model = "gpt-test"
sandbox_mode = "workspace-write"
approval_policy = "on-request"   # interactive default
web_search = "indexed"
model_reasoning_effort = 'high'
notify = ["a", "b"]
multi = \"\"\"
ignored
\"\"\"
[[profiles]]
name = "skip"

[desktop]
projectlessWorkspaceRoot = 'E:\\Data\\Codex\\session'
"git-worktree-root" = "E:\\\\Data\\\\Codex\\\\worktrees"
"""


class TomlTests(CliTestCase):
    def test_minimal_parser_reads_needed_keys(self) -> None:
        data = toml_compat.parse_minimal(CODEX_TOML)
        self.assertEqual(data["model"], "gpt-test")
        self.assertEqual(data["approval_policy"], "on-request")
        self.assertEqual(data["web_search"], "indexed")
        self.assertEqual(data["model_reasoning_effort"], "high")
        self.assertNotIn("notify", data)
        self.assertNotIn("multi", data)
        self.assertEqual(data["desktop"]["projectlessWorkspaceRoot"], "E:\\Data\\Codex\\session")
        self.assertEqual(data["desktop"]["git-worktree-root"], "E:\\Data\\Codex\\worktrees")
        self.assertNotIn("name", data)

    def test_minimal_parser_matches_tomllib_on_scalars(self) -> None:
        path = self.root / "config.toml"
        path.write_text(CODEX_TOML, encoding="utf-8")
        minimal = toml_compat.load_toml(path, minimal=True)
        full = toml_compat.load_toml(path)
        for key in ("model", "sandbox_mode", "approval_policy", "web_search", "model_reasoning_effort"):
            self.assertEqual(minimal[key], full[key])
        self.assertEqual(minimal["desktop"]["projectlessWorkspaceRoot"], full["desktop"]["projectlessWorkspaceRoot"])


class ResolutionTests(CliTestCase):
    def resolve(self, cli_values: dict | None = None, cfg: dict | None = None, codex: dict | None = None) -> storage.Locations:
        home = storage.resolve_home(None)
        return storage.resolve_locations(home, cli_values or {}, cfg or config.default_config(), codex or {})

    def test_priority_cli_env_config_inferred_default(self) -> None:
        outside = self.root / "D-drive" / "Claude" / "temp"
        with self.patched_env(CLAUDE_CODE_TMPDIR=str(outside)):
            os.environ.pop(P + "RECORDS_DIR")
            os.environ.pop(P + "CACHE_DIR")
            os.environ.pop(P + "WORKSPACE_ROOT")
            inferred = self.resolve()
            self.assertEqual(inferred.home.path, self.state_dir)
            self.assertEqual(inferred.cache.path, outside / "agent-delegate-verify")
            self.assertEqual(inferred.cache.source, "inferred")
            self.assertEqual(inferred.records.path, self.root / "D-drive" / "agent-delegate-verify" / "runs")
            self.assertEqual(inferred.workspaces.source, "default")
            self.assertFalse(inferred.confirmed)

            cfg = {**config.default_config(), "locations": {"records": str(self.root / "cfg-records")}, "locations_confirmed": True}
            configured = self.resolve(cfg=cfg)
            self.assertEqual((configured.records.path, configured.records.source), (self.root / "cfg-records", "config"))
            self.assertTrue(configured.confirmed)

            os.environ[P + "RECORDS_DIR"] = str(self.root / "env-records")
            from_env = self.resolve(cfg=cfg)
            self.assertEqual((from_env.records.path, from_env.records.source), (self.root / "env-records", "env"))

            from_cli = self.resolve({"records": str(self.root / "cli-records")}, cfg)
            self.assertEqual((from_cli.records.path, from_cli.records.source), (self.root / "cli-records", "cli"))

    def test_codex_projectless_root_sets_isolated_workspace_parent(self) -> None:
        root = self.root / "E" / "Codex" / "session"
        with self.patched_env():
            os.environ.pop(P + "WORKSPACE_ROOT")
            locations = self.resolve(codex={"desktop": {"projectlessWorkspaceRoot": str(root)}})
        self.assertEqual((locations.workspaces.path, locations.workspaces.source), (root, "inferred"))
        self.assertIn("projectlessWorkspaceRoot", locations.workspaces.detail)

    def test_host_folders_inside_home_do_not_move_records(self) -> None:
        inside = self.home / "AppData" / "Local" / "Temp" / "claude"
        with self.patched_env(CLAUDE_CODE_TMPDIR=str(inside), CODEX_HOME=str(self.home / ".codex"), CLAUDE_CONFIG_DIR=str(self.home / ".claude")):
            for key in ("RECORDS_DIR", "CACHE_DIR", "WORKSPACE_ROOT"):
                os.environ.pop(P + key)
            locations = self.resolve()
        self.assertEqual(locations.records.source, "default")
        self.assertEqual(locations.records.path, self.state_dir / "runs")
        self.assertEqual(locations.cache.path, inside / "agent-delegate-verify")  # temp still follows the host

    def test_drive_root_anchor_is_not_used_for_records(self) -> None:
        anchors = [storage.Anchor("codex-home", Path(Path.cwd().anchor) / "codex", "CODEX_HOME")]
        with self.patched_env():
            self.assertNotIn("records", storage.infer_locations(anchors))

    def test_platform_defaults(self) -> None:
        with self.patched_env():
            os.environ.pop(P + "HOME")
            home = storage.platform_home()
            cache = storage.platform_cache()
        if os.name == "nt":
            self.assertEqual(home, self.home / "AppData" / "Local" / "agent-delegate-verify")
            self.assertEqual(cache, home / "cache")
        else:
            self.assertTrue(str(home).startswith(str(self.home)))
            self.assertTrue(str(cache).startswith(str(self.home)))

    def test_onboard_recommended_saves_inferred_paths(self) -> None:
        outside = self.root / "D-drive" / "Claude" / "temp"
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            "[desktop]\nprojectlessWorkspaceRoot = '" + str(self.root / "D-drive" / "Codex" / "session") + "'\n", encoding="utf-8")
        proc = self.run_cli("onboard", "--locations", "recommended", extra_env={"CLAUDE_CODE_TMPDIR": str(outside)}, locations=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = self.json_of(proc)
        self.assertTrue(payload["locations"]["confirmed"])
        stored = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(stored["locations"]["cache"]), outside / "agent-delegate-verify")
        self.assertEqual(Path(stored["locations"]["records"]), self.root / "D-drive" / "agent-delegate-verify" / "runs")
        self.assertEqual(Path(stored["locations"]["workspaces"]), self.root / "D-drive" / "Codex" / "session")

    def test_set_location_and_set_python(self) -> None:
        target = self.root / "custom-records"
        proc = self.run_cli("config", "--set-location", f"records={target}", locations=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = self.json_of(proc)
        self.assertEqual(Path(payload["locations"]["records"]["path"]), target)
        self.assertEqual(payload["locations"]["records"]["source"], "config")
        reset = self.json_of(self.run_cli("config", "--set-location", "records=default", locations=False))
        self.assertNotEqual(reset["locations"]["records"]["source"], "config")
        self.assertEqual(self.run_cli("config", "--set-location", "nowhere=x").returncode, 1)
        pinned = self.run_cli("config", "--set-python", "current")
        self.assertEqual(pinned.returncode, 0, msg=pinned.stderr)
        self.assertTrue(json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))["python"])
        bad = self.run_cli("config", "--set-python", str(self.root / "no-python.exe"))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("Not a usable Python", bad.stderr)
        cleared = self.run_cli("config", "--set-python", "clear")
        self.assertIsNone(self.json_of(cleared)["python"])


class RunRecordTests(CliTestCase):
    def test_isolated_workspace_naming_and_resume_reuse(self) -> None:
        self.onboard_all()
        first = self.run_cli("run", "--runner", "codex", "--prompt", "research only")
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        result = self.result_of(first)
        today = dt.date.today().isoformat()
        self.assertEqual(Path(result["workspace"]["path"]), self.workspaces / today / "codex-task")
        self.assertTrue(result["workspace"]["isolated"])
        self.assertIn("(isolated)", first.stdout)
        second = self.result_of(self.run_cli("run", "--runner", "codex", "--prompt", "again"))
        self.assertEqual(Path(second["workspace"]["path"]), self.workspaces / today / "codex-task-2")
        named = self.result_of(self.run_cli("run", "--runner", "codex", "--prompt", "x", "--slug", "News Check!"))
        self.assertEqual(Path(named["workspace"]["path"]).name, "news-check")

        record = self.root / "record.json"
        resumed = self.run_cli("run", "--runner", "codex", "--session", result["session_id"], "--prompt", "follow up",
                               extra_env={"FAKE_RECORD": str(record)})
        self.assertEqual(resumed.returncode, 0, msg=resumed.stderr)
        self.assertEqual(Path(self.record_of()["cwd"]), Path(named["workspace"]["path"]))  # the latest run of that session
        unknown = self.run_cli("run", "--runner", "dsh", "--session", "never-seen", "--prompt", "x")
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("--workspace", unknown.stderr)

    def test_records_raw_policy_and_inflight_cleanup(self) -> None:
        self.onboard_all()
        ok = self.run_task("codex")
        self.assertEqual(ok.returncode, 0, msg=ok.stderr)
        result = self.result_of(ok)
        run_dir = Path(result["run_dir"])
        self.assertEqual(sorted(path.name for path in run_dir.iterdir()), ["prompt.md", "reply.md", "result.json"])
        self.assertIsNone(result["raw_events"]["paths"])
        self.assertIn("own session log", result["raw_events"]["policy"])
        self.assertIn("self-reported", (run_dir / "reply.md").read_text(encoding="utf-8"))
        self.assertEqual(list((self.cache / "inflight").iterdir()), [])
        self.assertEqual(result["status"], "completed")
        for key in ("run_dir", "reply_path", "result_path", "cli_session_file", "audit", "caller_verification", "self_report"):
            self.assertIn(key, result)
        self.assertIn("run_dir=", ok.stderr)

        kept = self.result_of(self.run_task("codex", "read", None, None, "30", "--keep-raw"))
        raw = Path(kept["raw_events"]["paths"][0])
        with gzip.open(raw, "rt", encoding="utf-8") as handle:
            self.assertIn("thread.started", handle.read())

    def test_cli_session_file_pointers(self) -> None:
        sessions_root = self.root / "codex-home" / "sessions"
        rollout = self.write_rollout("thread-1", "/fixture", sessions_root)
        with self.patched_env():
            started = dt.datetime(2026, 11, 3, 12, tzinfo=dt.timezone.utc)
            found = storage.cli_session_file("codex", "thread-1", self.workspace, started)
            self.assertEqual(Path(found["path"]), rollout)
            self.assertTrue(found["exists"])
            elsewhere = storage.cli_session_file("codex", "thread-1", self.workspace, None)  # full walk
            self.assertEqual(Path(elsewhere["path"]), rollout)
            missing = storage.cli_session_file("codex", "nope", self.workspace, started)
            self.assertFalse(missing["exists"])
            claude = storage.cli_session_file("claude", "sid", Path("/fixture/work space"), None)
            self.assertTrue(claude["path"].endswith(os.path.join("projects", "-fixture-work-space", "sid.jsonl")))
            other = storage.cli_session_file("grok", "sid", self.workspace, None)
            self.assertIsNone(other["path"])
            self.assertIn("not wired", other["note"])

    def test_codex_rollout_lookup_through_a_linked_sessions_folder(self) -> None:
        real = self.root / "other-drive" / "history" / "sessions"
        rollout = self.write_rollout("linked-thread", "/fixture", real)
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        kind = self.make_dir_link(codex_home / "sessions", real)
        with self.patched_env():
            found = storage.cli_session_file("codex", "linked-thread", self.workspace, None)
        self.assertTrue(found["exists"], msg=kind)
        self.assertEqual(Path(os.path.realpath(found["path"])), Path(os.path.realpath(rollout)))

    def test_show_and_verify(self) -> None:
        self.onboard_all()
        run = self.result_of(self.run_task("codex"))
        shown = self.json_of(self.run_cli("show", "latest", "--json"))
        self.assertEqual(shown["run"]["run_id"], run["run_id"])
        self.assertEqual(shown["run"]["caller_verification"], "pending")
        verified = self.run_cli("verify", run["run_id"], "--status", "verified", "--note", "re-ran pytest", "--check", "pytest -q", "--json")
        self.assertEqual(verified.returncode, 0, msg=verified.stderr)
        stored = json.loads(Path(run["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["caller_verification"]["status"], "verified")
        self.assertEqual(stored["caller_verification"]["checks"], ["pytest -q"])
        self.assertEqual(self.json_of(self.run_cli("show", "--json"))["run"]["caller_verification"], "verified")
        self.assertEqual(self.run_cli("show", "20990101T000000-codex-abcdef").returncode, 1)

    def test_locations_report(self) -> None:
        self.onboard_all()
        self.run_cli("run", "--runner", "codex", "--prompt", "x")
        legacy = self.home / ".oil-delegate" / "runs" / "old"
        legacy.mkdir(parents=True)
        (legacy / "output.md").write_text("old", encoding="utf-8")
        payload = self.json_of(self.run_cli("locations", "--json"))
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["records"]["runs"], 1)
        self.assertEqual(payload["records"]["source"], "env")
        self.assertEqual(payload["workspaces"]["created_by_this_skill"], 1)
        self.assertEqual(payload["legacy"]["files"], 1)
        self.assertIn("never deletes", payload["workspaces"]["note"])
        text = self.run_cli("locations")
        self.assertEqual(text.returncode, 0, msg=text.stderr)
        self.assertIn("records.usage=1 runs", text.stdout)


class PruneTests(CliTestCase):
    def fake_run(self, run_id: str, size: int = 10, status: str = "completed") -> Path:
        run_dir = self.records / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({"status": status, "runner": "codex"}), encoding="utf-8")
        (run_dir / "reply.md").write_bytes(b"x" * size)
        return run_dir

    def test_prune_lists_by_default_and_deletes_only_with_apply(self) -> None:
        old = self.fake_run("20200101T000000-codex-aaaaaa")
        recent_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-codex-bbbbbb"
        recent = self.fake_run(recent_id)
        stray = self.records / "notes"
        stray.mkdir()
        orphan = self.cache / "inflight" / "20200101T000000-codex-cccccc"
        orphan.mkdir(parents=True)
        (orphan / "stdout.log").write_text("partial", encoding="utf-8")
        workspace_file = self.workspaces / "2020-01-01" / "codex-task" / "report.md"
        workspace_file.parent.mkdir(parents=True)
        workspace_file.write_text("keep", encoding="utf-8")

        preview = self.run_cli("prune", "--json")
        self.assertEqual(preview.returncode, 0, msg=preview.stderr)
        payload = self.json_of(preview)
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertEqual({Path(item["path"]) for item in payload["candidates"]}, {old, orphan})
        self.assertIn("explicit yes", payload["how_to_apply"])
        self.assertTrue(old.exists() and orphan.exists())

        applied = self.json_of(self.run_cli("prune", "--apply", "--json"))
        self.assertEqual(applied["status"], "APPLIED")
        self.assertEqual(len(applied["deleted"]), 2)
        self.assertFalse(old.exists() or orphan.exists())
        self.assertTrue(recent.exists() and stray.exists() and workspace_file.exists())

    def test_prune_size_cap_removes_oldest_first(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        ids = [(now - dt.timedelta(hours=hours)).strftime("%Y%m%dT%H%M%S") + f"-codex-{index:06x}" for index, hours in enumerate((3, 2, 1))]
        for run_id in ids:
            self.fake_run(run_id, size=700 * 1024)
        payload = self.json_of(self.run_cli("prune", "--max-total-mb", "1", "--json"))
        self.assertEqual([Path(item["path"]).name for item in payload["candidates"]], ids[:2])

    def test_prune_refuses_links_and_foreign_paths(self) -> None:
        run_dir = self.fake_run("20200101T000000-codex-dddddd")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        self.make_symlink(run_dir / "link", outside, directory=True)
        with self.assertRaisesRegex(Exception, "contains a link"):
            storage.remove_run_tree(run_dir, [self.records])
        with self.assertRaisesRegex(Exception, "not a run directory"):
            storage.remove_run_tree(outside, [self.records])
        self.assertTrue((outside / "precious.txt").exists())

    def test_retention_is_configurable(self) -> None:
        proc = self.run_cli("config", "--retention-days", "7", "--max-total-mb", "50")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(self.json_of(proc)["retention"], {"days": 7, "max_total_mb": 50})
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).strftime("%Y%m%dT%H%M%S") + "-codex-eeeeee"
        self.fake_run(old)
        payload = self.json_of(self.run_cli("prune", "--json"))
        self.assertEqual(payload["rules"], {"days": 7, "max_total_mb": 50})
        self.assertEqual(len(payload["candidates"]), 1)


class LegacyTests(CliTestCase):
    def test_import_legacy_config_is_read_only(self) -> None:
        legacy = self.home / ".oil-delegate"
        legacy.mkdir()
        content = json.dumps({"enabled_runners": ["codex", "kimi"], "default_runner": "codex", "runner_paths": {"bogus": "/x"},
                              "roles": {"review": ["codex"], "critique": ["kimi"]}})
        (legacy / "config.json").write_text(content, encoding="utf-8")
        proc = self.run_cli("config", "--import-legacy")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = self.json_of(proc)
        self.assertEqual(payload["enabled_runners"], ["codex", "kimi"])
        self.assertEqual(payload["default_runner"], "codex")
        self.assertEqual(payload["roles"], {"review": ["codex"]})
        self.assertTrue(any("critique" in warning for warning in payload["warnings"]))
        self.assertEqual((legacy / "config.json").read_text(encoding="utf-8"), content)
        missing = self.run_cli("config", "--import-legacy", str(self.root / "none"))
        self.assertEqual(missing.returncode, 1)
