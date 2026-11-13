# 解释器与环境

## 引导脚本

| 脚本 | 适用 | 说明 |
| --- | --- | --- |
| `scripts/delegate.ps1` | Windows PowerShell 5.1+、PowerShell 7 | 纯 ASCII；管道输入会以 UTF-8 转给 Python |
| `scripts/delegate.sh` | Linux、macOS、Git Bash/MSYS | POSIX sh；Windows 风格路径用 `cygpath` 转换 |
| `scripts/delegate.cmd` | cmd.exe | 用 `-ExecutionPolicy RemoteSigned` 调用 `delegate.ps1`（优先 `pwsh`） |

它们按下面的顺序取第一个可用的解释器，然后把全部参数转给 `scripts/delegate.py`，并设置 `AGENT_DELEGATE_VERIFY_PYTHON_SOURCE` 说明来源（`doctor` 会显示）。

1. `AGENT_DELEGATE_VERIFY_PYTHON`：显式覆盖。不可用时报错退出（退出码 9），不悄悄换成别的。
2. `config --set-python` 固定的路径（读 `home/config.json` 的 `python`）：不可用时警告并继续。
3. PATH：Windows 依次所有 `python`、所有 `python3`、`py -3`；POSIX 依次 `python3`、`python`、`py -3`。
4. 环境与版本管理器：`CONDA_PREFIX`（已激活环境）；由 `CONDA_EXE`、`MAMBA_EXE`、`MAMBA_ROOT_PREFIX`、`conda info --base` 推出的 base 及其 `envs/*`；`uv python find`（设置 `UV_PYTHON_DOWNLOADS=never`，只找不下载）；pyenv / pyenv-win、asdf、mise；Windows 的 Scoop（`SCOOP`、`~/scoop`、`SCOOP_GLOBAL`、`%ProgramData%\scoop` 下的 `python*`、`miniconda3`、`anaconda3`、`miniforge`、`mambaforge`）。
5. 常见目录：Windows `%LOCALAPPDATA%\Programs\Python\Python3*`、`%ProgramFiles%\Python3*`、`C:\Python3*` 与 `~`、`%LOCALAPPDATA%`、`%ProgramData%` 下的 conda 默认目录；POSIX `/opt/homebrew/bin`、`/usr/local/bin`、`/usr/bin`、python.org framework、`~` 与 `/opt` 下的 conda 默认目录。
6. Windows 注册表 PEP 514：`HKCU`、`HKLM`、`HKLM\…\WOW6432Node` 的 `Software\Python\<公司>\<版本>\InstallPath`。

每个候选都实际执行一次 `-I -c`，读出版本和 `sys.executable`：低于 3.10、执行失败、Microsoft Store 的占位 `python.exe` 一律拒绝并记录原因。探测代码不含双引号和 `%`，可安全经过 PowerShell 5.1 与 `.bat` 包装。`DELEGATE --which` 打印选中的解释器、版本、来源和被拒候选，不启动主脚本。

## 固定与覆盖

- 一次性：`AGENT_DELEGATE_VERIFY_PYTHON=<路径>`。
- 长期：`DELEGATE config --set-python <路径>`（或 `current` 固定当前解释器，`clear` 取消）。写入前同样执行校验。
- 项目或用户已指定解释器时，直接用它运行 `scripts/delegate.py`，不经过引导脚本也可以。

## `doctor`

只读诊断，`--json` 时输出单个对象：

- `python`：当前解释器、引导来源、覆盖与固定值、所有候选（接受/拒绝及原因）、第一个可用者。
- `package_managers`：`conda info --json` 的版本、base、环境列表；uv 位置；Scoop 根目录。
- `runners`：9 个 runner 的可执行文件、来源、启动方式（native/npm-shim/batch/python-script）、版本、支持的权限级别。
- `locations`：同 `locations` 子命令。
- `hosts`：Codex 的 `CODEX_HOME`、`config.toml` 中的 `model`、`model_reasoning_effort`、`sandbox_mode`、`approval_policy`、`web_search`、`[desktop].projectlessWorkspaceRoot`；Claude 的 `CLAUDE_CONFIG_DIR`、`CLAUDE_CODE_TMPDIR`。
- `warnings`：例如没有可用的 Python、PATH 上有 Store 占位符、位置尚未确认、`config.toml` 解析失败。

`doctor` 会运行各候选解释器的版本探测、`conda info --json` 和各 CLI 的 `--version`，不会调用模型。

## 环境提示

默认在委托提示词末尾的运行时上下文里附一行：当前解释器路径与版本、conda base、已激活的 conda 环境（来自环境变量或 PATH 上的 conda 位置，不启动子进程）。只含路径与版本，不含任何环境变量值或凭据。`run --no-env-hints` 或 `config --env-hints off` 关闭。

## Python 3.10 兼容

主脚本只用标准库。`tomllib` 在 3.11 才有：3.10 上读取 `config.toml` 时改用内置的只读小型解析器，只支持表、点号键、基本/字面字符串、布尔与数字，跳过数组、内联表和多行字符串（本技能只需要其中的标量键）。
