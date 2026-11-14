"""Onboarding, runner selection, permission levels, roles and the run contract.

Most cases are migrated from upstream oil-delegate's test_delegate.py and
adapted to the new safe defaults (read by default, explicit full access).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from support import P, PI_SESSION, RUNNERS, SCRIPT_PATH, CliTestCase, cli, common, posix_tail

READ_FLAGS = {
    "claude": ["--permission-mode", "plan", "--permission-prompts", "none"],
    "workbuddy": ["--permission-mode", "plan", "-y"],
    "antigravity": ["--mode", "plan"],
    "codex": ["--sandbox", "read-only", "-c", "approval_policy=never"],
    "opencode": ["--agent", "plan"],
    "dsh": ["--permission-mode", "read-only"],
    "grok": ["--permission-mode", "plan"],
    "pi": ["--no-extensions", "--no-mcp", "--tools", "read,grep,find,ls"],
}
WRITE_FLAGS = {
    "claude": ["--permission-mode", "acceptEdits", "--permission-prompts", "none"],
    "codex": ["--sandbox", "workspace-write", "-c", "approval_policy=never"],
    "dsh": ["--permission-mode", "workspace-write"],
    "opencode": [],
}
FULL_FLAGS = {
    "claude": ["--dangerously-skip-permissions"],
    "workbuddy": ["--permission-mode", "bypassPermissions", "-y"],
    "antigravity": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "opencode": [],
    "dsh": ["--permission-mode", "danger-full-access"],
    "grok": ["--always-approve"],
    "kimi": [],
    "pi": [],
}
RESUME_FLAGS = {
    "claude": "--resume",
    "workbuddy": "--resume",
    "antigravity": "--conversation",
    "codex": "resume",
    "opencode": "--session",
    "dsh": "--session-id",
    "grok": "--resume",
    "kimi": "-S",
    "pi": "--session",
}
STDIN_RUNNERS = {"codex", "dsh", "pi", "claude"}


def first_mode(runner: str) -> str:
    return "full" if runner == "kimi" else "read"


class OnboardingTests(CliTestCase):
    def test_scan_is_read_only_and_reports_nine_runners(self) -> None:
        result = self.run_cli("onboard", "--scan")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = self.json_of(result)
        self.assertEqual(payload["status"], "NEEDS_ONBOARDING")
        self.assertEqual(payload["needs"], ["runners"])
        self.assertEqual(set(payload["installed_runners"]), set(RUNNERS))
        self.assertEqual(len(RUNNERS), 9)
        capabilities = payload["detected_runners"]["dsh"]["capabilities"]
        self.assertTrue(capabilities["json"] and capabilities["resume"] and capabilities["permission_flag"])
        self.assertEqual(payload["detected_runners"]["codex"]["prompt_delivery"], "stdin")
        self.assertFalse((self.state_dir / "config.json").exists())

    def test_first_run_shows_locations_before_writing_anything(self) -> None:
        record = self.root / "record.json"
        before = self.run_cli("run", "--runner", "cc", "--workspace", str(self.workspace), "--prompt", "inspect",
                              extra_env={"FAKE_RECORD": str(record)}, locations=False)
        self.assertEqual(before.returncode, common.EXIT_NEEDS_ONBOARDING, msg=before.stderr)
        payload = self.json_of(before)
        self.assertEqual(payload["status"], "NEEDS_ONBOARDING")
        self.assertEqual(payload["needs"], ["locations"])
        choices = payload["locations"]["choices"]
        self.assertEqual(set(choices), {"recommended", "default"})
        self.assertEqual(set(choices["default"]), {"records", "cache", "workspaces"})
        self.assertIn("never_copied", payload["locations"])
        self.assertFalse(record.exists())
        self.assertFalse(self.state_dir.exists())

        # A dry run writes nothing, so it does not need confirmed locations.
        dry = self.run_cli("run", "--runner", "cc", "--prompt", "inspect", "--dry-run", locations=False)
        self.assertEqual(dry.returncode, 0, msg=dry.stderr)
        self.assertFalse(self.json_of(dry)["locations"]["confirmed"])

        confirmed = self.run_cli("onboard", "--locations", "default", locations=False)
        self.assertEqual(confirmed.returncode, 0, msg=confirmed.stderr)
        stored = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(stored["locations_confirmed"])
        self.assertEqual(Path(stored["locations"]["records"]), self.state_dir / "runs")

        direct = self.run_cli("run", "--runner", "cc", "--workspace", str(self.workspace), "--prompt", "inspect",
                              extra_env={"FAKE_RECORD": str(record)}, locations=False)
        self.assertEqual(direct.returncode, 0, msg=direct.stderr)
        result = self.result_of(direct)
        self.assertEqual(result["runner"], "claude")
        self.assertTrue(Path(result["run_dir"]).is_relative_to(self.state_dir / "runs"))

    def test_runner_pool_choice_default_and_merge(self) -> None:
        missing = self.run_task("opencode", extra_env={P + "OPENCODE_BIN": str(self.root / "missing" / "opencode")})
        self.assertEqual(missing.returncode, 1)
        self.assertIn("not an executable file", missing.stderr)

        before = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "inspect")
        self.assertEqual(before.returncode, 10)
        payload = self.json_of(before)
        self.assertEqual(payload["needs"], ["runners"])
        self.assertEqual(set(payload["installed_runners"]), set(RUNNERS))
        self.assertFalse((self.state_dir / "config.json").exists())

        self.assertEqual(self.run_cli("onboard", "--enable", "claude,dsh", "--default-runner", "ask").returncode, 0)
        choice = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "inspect")
        self.assertEqual(choice.returncode, 11)
        self.assertEqual(self.json_of(choice)["status"], "NEEDS_RUNNER_CHOICE")

        configured = self.run_cli("config", "--set-default-runner", "agy")
        self.assertEqual(configured.returncode, 0, msg=configured.stderr)
        self.assertEqual(self.json_of(configured)["default_runner"], "antigravity")
        default_run = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "inspect")
        self.assertEqual(default_run.returncode, 0, msg=default_run.stderr)
        self.assertEqual(self.result_of(default_run)["runner"], "antigravity")

        config_path = self.state_dir / "config.json"
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        stored["future_field"] = {"keep": True}
        config_path.write_text(json.dumps(stored), encoding="utf-8")
        again = self.run_cli("onboard", "--enable", "claude,codebuddy,deepseek-harness,agy,claude", "--default-runner", "claude-code")
        self.assertEqual(again.returncode, 0, msg=again.stderr)
        merged = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(merged["future_field"], {"keep": True})
        self.assertEqual(merged["default_runner"], "claude")
        self.assertEqual(len(merged["enabled_runners"]), len(set(merged["enabled_runners"])))

    def test_short_aliases_work_in_onboarding_run_and_config(self) -> None:
        onboard = self.run_cli("onboard", "--enable", "cc,wb,agy,dsh,claude,grok-build,kimi-code,pi-agent", "--default-runner", "CC")
        self.assertEqual(onboard.returncode, 0, msg=onboard.stderr)
        payload = self.json_of(onboard)
        self.assertEqual(payload["enabled_runners"], ["claude", "workbuddy", "antigravity", "dsh", "grok", "kimi", "pi"])
        self.assertEqual(payload["default_runner"], "claude")
        for runner, alias in (("claude", "cc"), ("workbuddy", "wb"), ("antigravity", "anti"), ("grok", "grok-build"), ("kimi", "kimi-code"), ("pi", "pi-agent")):
            self.assertIn(alias, payload["detected_runners"][runner]["aliases"])
        for alias, canonical, mode in (("wb", "workbuddy", "read"), ("kimi-code", "kimi", "full"), ("anti", "antigravity", "read"), ("pi-coding-agent", "pi", "read")):
            with self.subTest(alias=alias):
                proc = self.run_task(alias, mode=mode)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(self.result_of(proc)["runner"], canonical)
        self.assertEqual(self.json_of(self.run_cli("config", "--set-default-runner", "WB"))["default_runner"], "workbuddy")
        binary = self.run_cli("config", "--set-bin", f"CC={self.binaries['claude']}")
        self.assertEqual(binary.returncode, 0, msg=binary.stderr)
        stored = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["runner_paths"]["claude"], str(self.binaries["claude"]))
        self.assertEqual(self.run_cli("config", "--clear-bin", "cc").returncode, 0)
        stored = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("claude", stored["runner_paths"])

    def test_disabled_runner_is_not_silently_substituted(self) -> None:
        self.assertEqual(self.run_cli("onboard", "--enable", "claude", "--default-runner", "claude").returncode, 0)
        result = self.run_task("codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not enabled", result.stderr)

    def test_set_bin_and_default_runner_in_same_request(self) -> None:
        missing_path = {P + "OPENCODE_BIN": str(self.root / "nowhere" / "opencode")}
        missing = self.run_cli("config", "--set-default-runner", "opencode", extra_env=missing_path)
        self.assertEqual(missing.returncode, 1)
        self.assertFalse((self.state_dir / "config.json").exists())
        configured = self.run_cli("config", "--set-bin", f"opencode={self.binaries['opencode']}", "--set-default-runner", "opencode",
                                  remove_env=[P + "OPENCODE_BIN"])
        self.assertEqual(configured.returncode, 0, msg=configured.stderr)
        self.assertEqual(self.json_of(configured)["default_runner"], "opencode")
        stored = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["runner_paths"]["opencode"], str(self.binaries["opencode"]))

    def test_config_and_runner_choice_skip_version_probes(self) -> None:
        call_log = self.root / "calls.jsonl"
        self.run_cli("onboard", "--enable", ",".join(RUNNERS), "--default-runner", "ask", extra_env={"FAKE_CALL_LOG": str(call_log)})
        self.assertTrue(call_log.exists())
        call_log.unlink()
        self.assertEqual(self.run_cli("config").returncode, 0)
        self.assertFalse(call_log.exists())
        choice = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "x")
        self.assertEqual(choice.returncode, 11)
        self.assertFalse(call_log.exists())


class PermissionTests(CliTestCase):
    def test_all_runner_modes_and_resume(self) -> None:
        self.onboard_all()
        record_path = self.root / "record.json"
        for runner in RUNNERS:
            for mode, table in (("read", READ_FLAGS), ("write", WRITE_FLAGS), ("full", FULL_FLAGS)):
                with self.subTest(runner=runner, mode=mode):
                    record_path.unlink(missing_ok=True)
                    proc = self.run_task(runner, mode=mode, extra_env={"FAKE_RECORD": str(record_path)})
                    if runner not in table:
                        self.assertEqual(proc.returncode, 1, msg=proc.stdout)
                        self.assertIn(f"cannot run with --mode {mode}", proc.stderr)
                        self.assertFalse(record_path.exists())
                        continue
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                    result = self.result_of(proc)
                    record = self.record_of()
                    self.assertEqual((result["status"], result["response"]), ("completed", "fake response"))
                    expected_session = PI_SESSION if runner == "pi" else f"fake-{runner}-session"
                    self.assertEqual(result["session_id"], expected_session)
                    self.assertEqual(record["cwd"], str(self.workspace))
                    self.assertTrue(self.contains_run(record["argv"], table[runner]), msg=record["argv"])
                    self.assertEqual(result["danger"], mode == "full")
                    self.assertFalse(any("inspect the fixture" in part for part in result["command"]))
                    if runner in STDIN_RUNNERS:
                        self.assertIn("inspect the fixture", record["stdin"])
                        self.assertNotIn("<prompt>", result["command"])
                    else:
                        self.assertIn("<prompt>", result["command"])
                    if runner == "opencode":
                        permission = json.loads(record["permission"])
                        expected = {"read": ("deny", "deny", "deny"), "write": ("allow", "deny", "deny"), "full": ("allow", "allow", "allow")}[mode]
                        self.assertEqual((permission["edit"], permission["bash"], permission["external_directory"]), expected)
                        self.assertEqual(permission["question"], "deny")
                    if runner == "workbuddy":
                        self.assertEqual(record["disable_background"], "1")
                        self.assertEqual(record["codebuddy_sandbox"], "1" if mode == "full" else None)
                    if runner == "dsh":
                        self.assertTrue(self.contains_run(record["argv"], ["--approval-policy", "never"]))
                    if runner == "kimi":
                        self.assertNotIn("--auto", record["argv"])
                        self.assertIn("stream-json", record["argv"])
                    for forbidden in ("--trust", "--ephemeral"):
                        self.assertNotIn(forbidden, record["argv"])

            with self.subTest(runner=runner, mode="resume"):
                record_path.unlink(missing_ok=True)
                session = PI_SESSION if runner == "pi" else f"fake-{runner}-session"
                proc = self.run_task(runner, mode=first_mode(runner), session=session, extra_env={"FAKE_RECORD": str(record_path)})
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                resumed = self.result_of(proc)
                record = self.record_of()
                self.assertEqual(resumed["session_id"], session)
                self.assertIn(RESUME_FLAGS[runner], record["argv"])
                if runner == "codex":
                    self.assertTrue(self.contains_run(record["argv"], ["-c", "sandbox_mode=read-only"]))
                    self.assertNotIn("--sandbox", record["argv"])

    def test_default_mode_is_read_only(self) -> None:
        args = cli.build_parser().parse_args(["run", "--prompt", "inspect"])
        self.assertIsNone(args.mode)
        self.onboard_all()
        record_path = self.root / "record.json"
        for runner, flags in (("grok", ["--permission-mode", "plan"]), ("codex", ["--sandbox", "read-only"])):
            with self.subTest(runner=runner):
                proc = self.run_task(runner, mode=None, extra_env={"FAKE_RECORD": str(record_path)})
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(self.result_of(proc)["mode"], "read")
                self.assertTrue(self.contains_run(self.record_of()["argv"], flags))
        kimi = self.run_task("kimi", mode=None)
        self.assertEqual(kimi.returncode, 1)
        self.assertIn("cannot run with --mode read", kimi.stderr)

    def test_full_access_is_explicit_and_flagged(self) -> None:
        self.onboard_all()
        proc = self.run_task("codex", mode="full")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("FULL ACCESS", proc.stderr)
        self.assertIn("WARNING=FULL ACCESS", proc.stdout)
        result = self.result_of(proc)
        self.assertTrue(result["danger"])
        self.assertIn("bypassed", result["permission"])
        dry = self.json_of(self.run_cli("run", "--runner", "codex", "--mode", "full", "--prompt", "x", "--dry-run"))
        self.assertTrue(dry["danger"])

    def test_dsh_legacy_permission_flags_use_environment(self) -> None:
        self.onboard_all()
        for mode, expected in (("read", "read-only"), ("write", "workspace-write"), ("full", "danger-full-access")):
            with self.subTest(mode=mode):
                record_path = self.root / "record.json"
                proc = self.run_task("dsh", mode=mode, extra_env={"FAKE_RECORD": str(record_path), "FAKE_DSH_LEGACY": "1"})
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                record = self.record_of()
                self.assertEqual(record["dsh_mode"], expected)
                self.assertNotIn("--permission-mode", record["argv"])

    def test_model_effort_and_web_search_only_when_asked(self) -> None:
        self.onboard_all()
        record_path = self.root / "record.json"
        plain = self.run_task("codex", extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(plain.returncode, 0, msg=plain.stderr)
        argv = self.record_of()["argv"]
        self.assertFalse(any(part.startswith(("model", "web_search", "model_reasoning_effort")) for part in argv))
        self.assertNotIn("-m", argv)
        asked = self.run_task("codex", "read", None, {"FAKE_RECORD": str(record_path)}, "30",
                              "--web-search", "live", "--model", "gpt-test", "--effort", "high")
        self.assertEqual(asked.returncode, 0, msg=asked.stderr)
        argv = self.record_of()["argv"]
        for flags in (["-c", "web_search=live"], ["-m", "gpt-test"], ["-c", "model_reasoning_effort=high"]):
            self.assertTrue(self.contains_run(argv, flags), msg=argv)
        claude = self.run_task("claude", "read", None, {"FAKE_RECORD": str(record_path)}, "30", "--model", "sonnet", "--effort", "low")
        self.assertEqual(claude.returncode, 0, msg=claude.stderr)
        self.assertTrue(self.contains_run(self.record_of()["argv"], ["--model", "sonnet"]))
        for runner, option in (("grok", ["--model", "x"]), ("claude", ["--web-search", "live"]), ("pi", ["--effort", "high"])):
            with self.subTest(runner=runner, option=option):
                refused = self.run_task(runner, "read", None, None, "30", *option)
                self.assertEqual(refused.returncode, 1)
        bad_effort = self.run_task("codex", "read", None, None, "30", "--effort", "very high")
        self.assertEqual(bad_effort.returncode, 1)


class RoleTests(CliTestCase):
    def run_role(self, role: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_cli("run", "--role", role, "--workspace", str(self.workspace), "--prompt", "review the fixture", *extra, extra_env=env)

    def test_role_is_asked_once_then_saved_and_review_roles_force_read_mode(self) -> None:
        self.onboard_all()
        asked = self.run_role("review", "--dry-run")
        self.assertEqual(asked.returncode, common.EXIT_NEEDS_ROLE_CHOICE, msg=asked.stderr)
        payload = self.json_of(asked)
        self.assertEqual((payload["status"], payload["mode"], payload["candidates"][0]), ("NEEDS_ROLE_CHOICE", "read", "host"))
        self.assertNotIn("kimi", payload["candidates"])
        implement = self.json_of(self.run_role("implement", "--dry-run"))
        self.assertIn("kimi", implement["candidates"])
        self.assertEqual(implement["candidate_modes"]["kimi"], ["full"])

        rejected = self.run_cli("config", "--set-role", "review=kimi")
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("read-only", rejected.stderr)
        saved = self.run_cli("config", "--set-role", "review=cc")
        self.assertEqual(saved.returncode, 0, msg=saved.stderr)
        self.assertEqual(self.json_of(saved)["roles"], {"review": ["claude"]})

        record_path = self.root / "record.json"
        proc = self.run_role("review", env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = self.result_of(proc)
        self.assertEqual((result["runner"], result["role"], result["mode"]), ("claude", "review", "read"))
        self.assertIn("plan", self.record_of()["argv"])

        conflict = self.run_role("review", "--mode", "write")
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("always runs in read mode", conflict.stderr)
        explicit = self.json_of(self.run_role("review", "--runner", "codex", "--dry-run"))
        self.assertEqual((explicit["runner"], explicit["mode"]), ("codex", "read"))

    def test_implement_defaults_to_workspace_write(self) -> None:
        self.onboard_all()
        self.assertEqual(self.run_cli("config", "--set-role", "implement=codex").returncode, 0)
        dry = self.json_of(self.run_role("implement", "--dry-run"))
        self.assertEqual((dry["runner"], dry["mode"], dry["danger"]), ("codex", "write", False))
        self.assertTrue(self.contains_run(dry["command"], ["--sandbox", "workspace-write"]))
        full = self.json_of(self.run_role("implement", "--mode", "full", "--dry-run"))
        self.assertTrue(full["danger"])
        self.assertEqual(self.run_role("implement", "--mode", "read", "--dry-run").returncode, 1)
        warning = self.json_of(self.run_cli("config", "--set-role", "implement=pi"))
        self.assertTrue(any("--mode full" in item for item in warning["warnings"]))

    def test_host_role_and_diagnose_slots(self) -> None:
        self.assertEqual(self.run_cli("config", "--set-role", "critique=host").returncode, 0)
        host = self.run_role("critique")
        self.assertEqual(host.returncode, common.EXIT_USE_HOST, msg=host.stderr)
        self.assertEqual(self.json_of(host)["status"], "USE_HOST")
        self.assertFalse(self.records.exists())
        for bad in ("diagnose=codex", "diagnose=codex,codex", "review=claude,codex", "triage=codex"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli("config", "--set-role", bad).returncode, 1)
        self.assertEqual(self.run_cli("config", "--set-role", "diagnose=host,codex").returncode, 0)
        self.assertEqual(self.run_role("diagnose").returncode, common.EXIT_USE_HOST)
        second = self.run_role("diagnose", "--role-slot", "2", "--dry-run")
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertEqual(self.json_of(second)["runner"], "codex")
        self.assertEqual(self.run_role("diagnose", "--role-slot", "3", "--dry-run").returncode, 1)

    def test_role_warnings_reset_and_disabled_runner_cleanup(self) -> None:
        self.onboard_all()
        same = self.run_cli("config", "--set-role", "implement=codex", "--set-role", "review=codex")
        self.assertEqual(same.returncode, 0, msg=same.stderr)
        self.assertIn("same runner", self.json_of(same)["warnings"][0])
        self.run_cli("config", "--set-role", "critique=host")
        self.assertEqual(self.run_cli("config", "--set-role", "review=ask").returncode, 0)
        self.assertEqual(self.json_of(self.run_cli("config"))["roles"], {"implement": ["codex"], "critique": ["host"]})
        narrowed = self.run_cli("config", "--set-enabled-runners", "claude")
        self.assertEqual(self.json_of(narrowed)["roles"], {"critique": ["host"]})


class RunContractTests(CliTestCase):
    def test_prompt_composition(self) -> None:
        prompt = cli.compose_prompt("inspect", self.workspace, "write", [])
        for forbidden in ("Do not run git add", "git commit", "git push", "publish"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("Final report", prompt)
        self.assertIn("cite sources", prompt)
        hinted = cli.compose_prompt("inspect", self.workspace, "read", [], isolated=True,
                                    hints={"python": "/x/python", "python_version": "3.12.1", "conda_base": "/x/conda"})
        self.assertIn("Environment hints", hinted)
        self.assertIn("conda base at /x/conda", hinted)
        self.assertIn("empty folder", hinted)

        self.onboard_all()
        record_path = self.root / "record.json"
        with_hints = self.run_task("codex", extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(with_hints.returncode, 0, msg=with_hints.stderr)
        self.assertIn("Environment hints", self.record_of()["stdin"])
        without = self.run_task("codex", "read", None, {"FAKE_RECORD": str(record_path)}, "30", "--no-env-hints")
        self.assertEqual(without.returncode, 0, msg=without.stderr)
        self.assertNotIn("Environment hints", self.record_of()["stdin"])

    def test_prompt_file_and_stdin(self) -> None:
        self.onboard_all()
        record_path = self.root / "record.json"
        brief = self.root / "brief.md"
        brief.write_text("﻿先核实前提：\"quoted\" & 100% 'x'\n第二行", encoding="utf-8")
        from_file = self.run_cli("run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt-file", str(brief),
                                 extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(from_file.returncode, 0, msg=from_file.stderr)
        stdin = self.record_of()["stdin"]
        self.assertTrue(stdin.startswith("先核实前提：\"quoted\" & 100% 'x'\n第二行"))
        from_stdin = self.run_cli("run", "--runner", "codex", "--workspace", str(self.workspace), "--prompt", "-",
                                  input="来自 stdin 的任务\n", extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(from_stdin.returncode, 0, msg=from_stdin.stderr)
        self.assertIn("来自 stdin 的任务", self.record_of()["stdin"])
        both = self.run_cli("run", "--runner", "codex", "--prompt", "x", "--prompt-file", str(brief))
        self.assertEqual(both.returncode, 1)
        empty = self.run_cli("run", "--runner", "codex", "--workspace", str(self.workspace), "--prompt", "   ")
        self.assertEqual(empty.returncode, 1)
        self.assertIn("empty", empty.stderr)

    def test_dry_run_has_no_process_or_artifacts(self) -> None:
        self.onboard_all()
        record_path = self.root / "record.json"
        result = self.run_cli("run", "--runner", "claude", "--prompt", "do not execute", "--dry-run", extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = self.json_of(result)
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertTrue(payload["workspace"]["isolated"])
        self.assertTrue(payload["workspace"]["would_create"])
        self.assertTrue(payload["stdin"])
        self.assertFalse(record_path.exists())
        self.assertFalse(self.records.exists())
        self.assertFalse(self.workspaces.exists())

    def test_failure_timeout_and_dsh_bad_final(self) -> None:
        self.onboard_all()
        failed = self.run_task("claude", extra_env={"FAKE_FAIL": "1"})
        self.assertEqual(failed.returncode, 2)
        failed_result = self.result_of(failed)
        self.assertEqual(failed_result["status"], "failed")
        self.assertIn("fake failure", failed_result["error"])
        self.assertTrue(all(Path(path).is_file() for path in failed_result["raw_events"]["paths"]))
        self.assertIn("failed", failed_result["raw_events"]["policy"])
        timed = self.run_task("antigravity", timeout="1", extra_env={"FAKE_SLEEP": "5"})
        self.assertEqual(timed.returncode, 124)
        timed_result = self.result_of(timed)
        self.assertEqual((timed_result["status"], timed_result["timed_out"]), ("failed", True))
        self.assertIn("Timeout", timed_result["error"])
        bad_final = self.run_task("dsh", extra_env={"FAKE_DSH_BAD_FINAL": "1"})
        self.assertEqual(bad_final.returncode, 1)
        bad_result = self.result_of(bad_final)
        self.assertEqual(bad_result["status"], "failed")
        self.assertIn("bad final", bad_result["error"])

    def test_json_output_contract_and_resume_commands(self) -> None:
        self.onboard_all()
        expected = {
            "claude": ["--resume", "fake-claude-session"],
            "workbuddy": ["--resume", "fake-workbuddy-session"],
            "antigravity": ["--conversation", "fake-antigravity-session"],
            "codex": ["resume", "fake-codex-session"],
            "opencode": ["--session", "fake-opencode-session"],
            "dsh": ["tui", "--resume", "fake-dsh-session"],
            "grok": ["--resume", "fake-grok-session"],
            "kimi": ["-S", "fake-kimi-session"],
            "pi": ["--session", PI_SESSION],
        }
        for runner, tail in expected.items():
            with self.subTest(runner=runner):
                proc = self.run_cli("run", "--runner", runner, "--workspace", str(self.workspace), "--mode", first_mode(runner), "--prompt", "inspect", "--json")
                self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
                payload = self.json_of(proc)
                self.assertEqual((payload["contract_version"], payload["status"], payload["runner"]), (1, "completed", runner))
                commands = payload["resume_commands"]
                self.assertEqual(posix_tail(commands["posix"], len(tail)), tail)
                self.assertTrue(commands["powershell"].startswith("& '"), msg=commands["powershell"])
                self.assertTrue(commands["powershell"].endswith(" ".join(tail)))
                self.assertTrue(commands["cmd"].endswith(" ".join(tail)))
                self.assertIn(payload["resume_command"], commands.values())
                stored = json.loads(Path(payload["result_path"]).read_text(encoding="utf-8"))
                self.assertEqual(stored["resume_command"], payload["resume_command"])
                self.assertEqual(stored["caller_verification"]["status"], "pending")
                self.assertTrue(stored["self_report"]["reply_path"].endswith("reply.md"))
        dry = self.json_of(self.run_cli("run", "--runner", "agy", "--workspace", str(self.workspace), "--prompt", "x", "--dry-run", "--json"))
        self.assertEqual((dry["status"], dry["runner"], dry["contract_version"]), ("DRY_RUN", "antigravity", 1))

    def test_json_mode_reports_errors_and_keeps_exit_codes(self) -> None:
        self.onboard_all()
        refused = self.run_cli("run", "--runner", "kimi", "--workspace", str(self.workspace), "--mode", "read", "--prompt", "x", "--dry-run", "--json")
        self.assertEqual(refused.returncode, 1)
        payload = self.json_of(refused)
        self.assertEqual(payload["status"], "ERROR")
        self.assertTrue(payload["error"])
        timed = self.run_cli("run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt", "x", "--timeout", "1", "--json", extra_env={"FAKE_SLEEP": "5"})
        self.assertEqual(timed.returncode, 124)
        self.assertEqual(self.json_of(timed)["status"], "failed")
        choice = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "x", "--json")
        self.assertEqual(choice.returncode, 11)
        self.assertEqual(self.json_of(choice)["status"], "NEEDS_RUNNER_CHOICE")

    def test_parallel_runs_use_distinct_result_paths(self) -> None:
        self.onboard_all()
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", str(SCRIPT_PATH), "run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt", f"task {index}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=self.base_env(),
            )
            for index in range(3)
        ]
        paths = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, msg=stderr)
            paths.append(dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)["result_path"])
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(Path(path).is_file() for path in paths))

    def test_secret_environment_is_not_copied_into_records(self) -> None:
        self.onboard_all()
        sentinel = "agent-ferry-test-sentinel"
        proc = self.run_task("dsh", extra_env={"DEEPSEEK_API_KEY": sentinel})
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = self.result_of(proc)
        run_dir = Path(result["run_dir"])
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(sentinel, path.read_text(encoding="utf-8", errors="replace"), msg=str(path))
        self.assertIsNone(result["raw_events"]["paths"])

    def test_entry_file_is_validated_and_included_only_in_prompt(self) -> None:
        self.onboard_all()
        missing = self.run_cli("run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt", "inspect", "--file", "missing.py")
        self.assertEqual(missing.returncode, 1)
        entry = self.workspace / "entry.py"
        entry.write_text("print('ok')\n", encoding="utf-8")
        record_path = self.root / "record.json"
        valid = self.run_cli("run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt", "inspect", "--file", "entry.py",
                             extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(valid.returncode, 0, msg=valid.stderr)
        self.assertIn(str(entry), self.record_of()["stdin"])
        self.assertNotIn(str(entry), " ".join(self.result_of(valid)["command"]))

    def test_empty_responses_are_failures_for_all_runners(self) -> None:
        self.onboard_all()
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                proc = self.run_task(runner, mode=first_mode(runner), extra_env={"FAKE_EMPTY": "1"})
                self.assertNotEqual(proc.returncode, 0)
                result = self.result_of(proc)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["error"])

    def test_codex_failure_without_detail_is_not_success(self) -> None:
        self.onboard_all()
        proc = self.run_task("codex", extra_env={"FAKE_CODEX_FAIL_NO_DETAIL": "1"})
        self.assertNotEqual(proc.returncode, 0)
        result = self.result_of(proc)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Codex turn failed", result["error"])
