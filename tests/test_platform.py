"""Cross-platform behaviour: Windows shims, quoting, UTF-8 output, process trees, Python discovery and bootstraps."""
from __future__ import annotations

import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import IS_WINDOWS, P, ROOT, SCRIPTS, CliTestCase, common, minimal_path, pyfind, runners, write_fake_interpreter

NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@scope\tool\bin\tool.js" %*
"""
OLD_NPM_SHIM = '@IF EXIST "%~dp0\\node.exe" (\r\n  "%~dp0\\node.exe"  "%~dp0\\node_modules\\old\\cli.js" %*\r\n) ELSE (\r\n  node  "%~dp0\\node_modules\\old\\cli.js" %*\r\n)\r\n'


def process_alive(pid: int) -> bool:
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie still answers kill(0); check its state when /proc exists
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        return state != "Z"
    except OSError:
        return True


class LauncherTests(CliTestCase):
    def test_npm_shim_is_started_with_node_directly(self) -> None:
        for name, content, relative in (("tool.cmd", NPM_SHIM, ("node_modules", "@scope", "tool", "bin", "tool.js")),
                                        ("old.cmd", OLD_NPM_SHIM, ("node_modules", "old", "cli.js"))):
            with self.subTest(name=name):
                shim_dir = self.root / name.split(".")[0]
                script = shim_dir.joinpath(*relative)
                script.parent.mkdir(parents=True)
                script.write_text("// js", encoding="utf-8")
                (shim_dir / "node.exe").write_bytes(b"")
                shim = shim_dir / name
                shim.write_text(content, encoding="utf-8")
                self.assertEqual(runners.npm_shim_target(shim), script)
                with mock.patch.object(runners, "IS_WINDOWS", True):
                    launcher = runners.make_launcher(str(shim))
                self.assertEqual(launcher.kind, "npm-shim")
                self.assertEqual(launcher.prefix, [str(shim_dir / "node.exe"), str(script)])
                self.assertEqual(launcher.path, str(shim))

    def test_other_batch_files_are_checked_before_launch(self) -> None:
        batch = self.root / "dsh.cmd"
        batch.write_text('@echo off\r\n"%~dp0app.exe" --flag %*\r\n', encoding="utf-8")
        with mock.patch.object(runners, "IS_WINDOWS", True):
            launcher = runners.make_launcher(str(batch))
        self.assertEqual(launcher.kind, "batch")
        prompt = 'say "hi" & 100%\nnext line'
        runners.check_launch_safety(launcher, [str(batch), "--json", "-"], prompt, prompt)  # prompt on stdin: fine
        with self.assertRaisesRegex(common.DelegateError, "batch file"):
            runners.check_launch_safety(launcher, [str(batch), "-p", prompt], prompt, None)
        with self.assertRaisesRegex(common.DelegateError, "cmd.exe"):
            runners.check_launch_safety(launcher, [str(batch), "--cd", "C:\\100%"], prompt, prompt)

    def test_long_argv_prompt_is_refused(self) -> None:
        launcher = runners.Launcher("grok", ["grok"], "native")
        prompt = "x" * 40000
        with mock.patch.object(runners, "IS_WINDOWS", True), self.assertRaisesRegex(common.DelegateError, "too long"):
            runners.check_launch_safety(launcher, ["grok", "-p", prompt], prompt, None)
        runners.check_launch_safety(launcher, ["codex", "-"], prompt, prompt)  # stdin has no such limit

    def test_resume_commands_for_each_shell(self) -> None:
        odd = self.root / "Program Files" / "it's" / "codex.CMD"
        odd.parent.mkdir(parents=True)
        odd.write_text("@echo off\r\n", encoding="utf-8")
        commands = runners.resume_commands("codex", str(odd), "019a-uuid")
        self.assertEqual(shlex.split(commands["posix"]), [str(odd), "resume", "019a-uuid"])
        self.assertEqual(commands["powershell"], "& '" + str(odd).replace("'", "''") + "' resume 019a-uuid")
        self.assertEqual(commands["cmd"], f'"{odd}" resume 019a-uuid')
        short_dir = self.root / "short"
        short_dir.mkdir()
        exe = short_dir / ("codex.exe" if IS_WINDOWS else "codex")
        exe.write_bytes(b"")
        exe.chmod(0o755)
        with self.patched_env(PATH=str(short_dir)):
            short = runners.resume_commands("codex", str(exe), "abc")
        self.assertEqual(short, {"posix": "codex resume abc", "powershell": "codex resume abc", "cmd": "codex resume abc"})
        self.assertIsNone(runners.resume_commands("codex", str(exe), None))
        self.assertEqual(runners.resume_commands("dsh", None, "s 1")["powershell"], "dsh tui --resume 's 1'")

    @unittest.skipUnless(IS_WINDOWS, "cmd.exe batch shims exist only on Windows")
    def test_codex_through_a_cmd_shim_on_path(self) -> None:
        shim_dir = self.root / "shims"
        shim_dir.mkdir()
        shim = shim_dir / "codex.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{self.binaries["codex"]}" %*\r\n', encoding="utf-8")
        self.onboard_all()
        record = self.root / "record.json"
        env = {"PATH": str(shim_dir) + os.pathsep + minimal_path(), "FAKE_RECORD": str(record)}
        proc = self.run_cli("run", "--runner", "codex", "--workspace", str(self.workspace), "--prompt", 'quotes " and 100% & more',
                            extra_env=env, remove_env=[P + "CODEX_BIN"])
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        result = self.result_of(proc)
        self.assertEqual(result["launcher"]["kind"], "batch")
        self.assertIn('quotes " and 100% & more', self.record_of()["stdin"])
        self.assertEqual(result["resume_command"], "codex resume fake-codex-session")


class OutputTests(CliTestCase):
    def test_utf8_output_survives_a_non_utf8_console_encoding(self) -> None:
        self.onboard_all()
        text = "完成 ✅ — naïve 🚀"
        env = {"FAKE_RESPONSE": text, "PYTHONIOENCODING": "ascii"}
        as_json = self.run_cli("run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt", "x", "--json", extra_env=env)
        self.assertEqual(as_json.returncode, 0, msg=as_json.stderr)
        self.assertEqual(self.json_of(as_json)["response"], text)
        chinese_workspace = self.root / "工作区 ✅"
        chinese_workspace.mkdir()
        plain = self.run_cli("run", "--runner", "claude", "--workspace", str(chinese_workspace), "--prompt", "x",
                             extra_env={"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(plain.returncode, 0, msg=plain.stderr)
        self.assertIn("workspace=" + str(chinese_workspace), plain.stdout)
        self.assertIn("工作区 ✅", plain.stderr)

    def test_timeout_terminates_the_whole_process_tree(self) -> None:
        self.onboard_all()
        pid_file = self.root / "grandchild.pid"
        proc = self.run_task("claude", timeout="3", extra_env={"FAKE_SPAWN_CHILD": str(pid_file), "FAKE_SLEEP": "30"})
        self.assertEqual(proc.returncode, 124, msg=proc.stdout + proc.stderr)
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        alive = process_alive(pid)
        if alive:  # never leave a stray process behind, even when the assertion fails
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
            else:
                os.kill(pid, 9)
        self.assertFalse(alive, "the grandchild survived the timeout")


class PythonDiscoveryTests(CliTestCase):
    def test_discovery_order_versions_and_rejections(self) -> None:
        old_dir = self.root / "old"
        good_dir = self.root / "good"
        broken_dir = self.root / "broken"
        write_fake_interpreter(old_dir, "python", "3.9.7")
        good = write_fake_interpreter(good_dir, "python", "3.12.4")
        write_fake_interpreter(broken_dir, "python3", "", fail=True)
        path = os.pathsep.join(str(item) for item in (old_dir, broken_dir, good_dir))
        with self.patched_env(PATH=path):
            found = pyfind.discover({})
        by_path = {Path(item.command[0]).parent.name: item for item in found if item.source.startswith("PATH")}
        self.assertFalse(by_path["old"].accepted)
        self.assertIn("older than 3.10", by_path["old"].reason)
        self.assertFalse(by_path["broken"].accepted)
        self.assertIn("failed to run", by_path["broken"].reason)
        self.assertTrue(by_path["good"].accepted)
        self.assertEqual(by_path["good"].version, "3.12.4")
        self.assertEqual(next(item for item in found if item.accepted).command, [str(good)])

    def test_override_and_pinned_come_first(self) -> None:
        good = write_fake_interpreter(self.root / "pinned", "python", "3.11.0")
        override = write_fake_interpreter(self.root / "override", "python", "3.13.1")
        with self.patched_env(PATH=str(self.root / "empty"), **{P + "PYTHON": str(override)}):
            found = pyfind.discover({"python": str(good)})
        self.assertEqual([item.source.split(" ")[0] for item in found[:2]], [P + "PYTHON", "config.json"])
        self.assertTrue(found[0].accepted and found[1].accepted)

    def test_conda_environment_variables_are_used(self) -> None:
        base = self.root / "miniconda3"
        (base / "conda-meta").mkdir(parents=True)
        (base / "envs" / "work").mkdir(parents=True)
        scripts = base / ("Scripts" if IS_WINDOWS else "bin")
        scripts.mkdir(exist_ok=True)
        conda_exe = scripts / ("conda.exe" if IS_WINDOWS else "conda")
        conda_exe.write_bytes(b"")
        with self.patched_env(PATH=str(self.root / "empty"), CONDA_EXE=str(conda_exe)):
            commands = pyfind.candidate_commands({})
        sources = [source for _, source in commands]
        self.assertIn("conda base (CONDA_EXE)", sources)
        self.assertIn("conda env work (CONDA_EXE)", sources)
        hints = None
        with self.patched_env(CONDA_EXE=str(conda_exe), CONDA_PREFIX=str(base / "envs" / "work")):
            hints = pyfind.environment_hints()
        self.assertEqual(hints["conda_base"], str(base))
        self.assertEqual(hints["conda_active_env"], str(base / "envs" / "work"))
        self.assertEqual(hints["python"], sys.executable)

    @unittest.skipUnless(IS_WINDOWS, "Microsoft Store placeholders exist only on Windows")
    def test_store_placeholder_is_rejected(self) -> None:
        apps = self.root / "Microsoft" / "WindowsApps"
        write_fake_interpreter(apps, "python", "", fail=True)
        with self.patched_env(PATH=str(apps)):
            found = pyfind.discover({})
        placeholder = next(item for item in found if "WindowsApps" in item.command[0])
        self.assertIn("Microsoft Store placeholder", placeholder.reason)

    def test_doctor_reports_python_runners_locations_and_hosts(self) -> None:
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('web_search = "indexed"\nsandbox_mode = "workspace-write"\n', encoding="utf-8")
        proc = self.run_cli("doctor", "--json")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = self.json_of(proc)
        self.assertEqual(payload["python"]["running"]["executable"], sys.executable)
        self.assertIn("candidates", payload["python"])
        self.assertTrue(all(payload["runners"][name]["installed"] for name in payload["runners"]))
        self.assertEqual(payload["runners"]["codex"]["launcher"]["kind"], "python-script")
        self.assertEqual(payload["hosts"]["codex"]["settings"]["web_search"], "indexed")
        self.assertEqual(payload["locations"]["records"]["source"], "env")
        text = self.run_cli("doctor")
        self.assertEqual(text.returncode, 0, msg=text.stderr)
        self.assertIn("runner.codex=", text.stdout)
        self.assertIn("codex.settings=", text.stdout)


class BootstrapTests(CliTestCase):
    def tool_dir(self) -> Path:
        """POSIX tools the sh bootstrap needs, without any python on PATH."""
        tools = self.root / "tools"
        if not tools.exists():
            tools.mkdir()
            for name in ("dirname", "basename", "sed", "head", "tr", "uname"):
                found = shutil.which(name)
                if found:
                    (tools / name).symlink_to(found)
        return tools

    def bootstrap_env(self, path_dirs: list[Path], **extra: str) -> dict[str, str]:
        env = self.base_env()
        if IS_WINDOWS:
            root = os.environ.get("SystemRoot", r"C:\Windows")
            dll_dir = Path(sys.executable).parent / "Library" / "bin"  # conda builds load DLLs from here
            system = [str(dll_dir)] if dll_dir.is_dir() else []
            system += [os.path.join(root, "System32"), os.path.join(root, "System32", "WindowsPowerShell", "v1.0")]
        else:
            system = [str(self.tool_dir())]
        env["PATH"] = os.pathsep.join([*(str(item) for item in path_dirs), *system])
        env.update(extra)
        return env

    def run_bootstrap(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if IS_WINDOWS:
            shell = shutil.which("powershell.exe") or "powershell.exe"
            command = [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(SCRIPTS / "delegate.ps1"), *args]
        else:
            command = [shutil.which("sh") or "/bin/sh", str(SCRIPTS / "delegate.sh"), *args]
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              stdin=subprocess.DEVNULL, timeout=120, check=False)

    def which(self, env: dict[str, str]) -> dict[str, str]:
        proc = self.run_bootstrap(["--which"], env)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        values: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values.setdefault(key, value)
                if key == "rejected":
                    values["rejected_all"] = values.get("rejected_all", "") + value + "\n"
        return values

    def setUp(self) -> None:
        super().setUp()
        if IS_WINDOWS and not shutil.which("powershell.exe"):
            self.skipTest("Windows PowerShell is not available")
        if not IS_WINDOWS and not shutil.which("sh"):
            self.skipTest("sh is not available")

    def test_bootstrap_skips_old_and_broken_interpreters(self) -> None:
        old = self.root / "old"
        good = self.root / "good"
        broken = self.root / "broken"
        if IS_WINDOWS:  # every `python` on PATH is tried in order, then `python3`
            write_fake_interpreter(old, "python", "3.8.10")
            write_fake_interpreter(broken, "python", "", fail=True)
            chosen = write_fake_interpreter(good, "python", "3.12.4")
            path_dirs = [old, broken, good]
        else:  # sh only sees the first match of each name: python3, then python
            write_fake_interpreter(old, "python3", "3.8.10")
            chosen = write_fake_interpreter(good, "python", "3.12.4")
            path_dirs = [old, good]
        values = self.which(self.bootstrap_env(path_dirs))
        self.assertEqual(Path(values["python"]), chosen)
        self.assertEqual(values["version"], "3.12.4")
        self.assertIn("older than 3.10", values.get("rejected_all", ""))
        if IS_WINDOWS:
            self.assertIn("failed to run", values.get("rejected_all", ""))

    def test_bootstrap_override_must_work_and_pinned_is_used(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        bad = self.run_bootstrap(["--which"], self.bootstrap_env([empty], **{P + "PYTHON": str(self.root / "missing-python")}))
        self.assertEqual(bad.returncode, 9, msg=bad.stdout + bad.stderr)
        self.assertIn("not usable", bad.stderr)
        pinned = write_fake_interpreter(self.root / "pinned", "python", "3.11.2")
        self.state_dir.mkdir()
        (self.state_dir / "config.json").write_text(json.dumps({"python": str(pinned)}, indent=2), encoding="utf-8")
        values = self.which(self.bootstrap_env([empty]))
        self.assertEqual(Path(values["python"]), pinned)
        self.assertIn("pinned", values["source"])

    def test_bootstrap_conda_base_from_environment(self) -> None:
        if IS_WINDOWS:
            # <base>\python.exe must be a real executable on Windows, so use the test interpreter's folder as the
            # conda base; CONDA_EXE only has to point at <base>\Scripts\conda.exe, it is never executed.
            base = Path(sys.executable).parent
            target = base / "python.exe"
            if not target.is_file():
                self.skipTest("the test interpreter is not named python.exe")
            conda_exe = base / "Scripts" / "conda.exe"
        else:
            base = self.root / "miniforge3"
            (base / "conda-meta").mkdir(parents=True)
            target = write_fake_interpreter(base / "bin", "python3", "3.12.0")
            conda_exe = base / "bin" / "conda"
            conda_exe.write_bytes(b"")
        empty = self.root / "empty"
        empty.mkdir()
        values = self.which(self.bootstrap_env([empty], CONDA_EXE=str(conda_exe)))
        self.assertEqual(Path(values["python"]), target)
        self.assertIn("CONDA_EXE", values["source"])

    def test_bootstrap_launches_the_main_script(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        env = self.bootstrap_env([empty], **{P + "PYTHON": sys.executable})
        proc = self.run_bootstrap(["--version"], env)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        self.assertIn("agent-ferry", proc.stdout)
        exit_code = self.run_bootstrap(["show", "no-such-run"], env)
        self.assertEqual(exit_code.returncode, 1, msg=exit_code.stdout + exit_code.stderr)


class RepositoryHygieneTests(unittest.TestCase):
    def test_bootstrap_scripts_are_ascii(self) -> None:
        for name in ("delegate.ps1", "delegate.sh", "delegate.cmd"):
            data = (SCRIPTS / name).read_bytes()
            self.assertTrue(all(byte < 128 for byte in data), msg=name)

    def test_no_bytecode_or_run_records_in_the_repository(self) -> None:
        leftovers = [path for path in ROOT.rglob("*") if path.name == "__pycache__" or path.suffix == ".pyc" or path.name == "result.json"]
        self.assertEqual(leftovers, [])
