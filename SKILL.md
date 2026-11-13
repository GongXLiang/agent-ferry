---
name: agent-delegate-verify
description: "把一项边界清楚的任务从 Claude Code 等宿主委托给另一个 CLI Agent（Codex、Claude Code、OpenCode、DeepSeek Harness、Kimi Code、Pi、Grok Build、WorkBuddy、Antigravity），默认只读沙箱与隔离工作区，自动找 Python/conda，告诉用户文件放在哪，返回审计摘要与续接命令，结果须由调用方核实后再转述。Use when the user asks to delegate or hand off a task to Codex, Claude Code or another CLI coding agent, get a second opinion, review, research or implementation from it, continue that agent's session, audit what it did, or check and clean this skill's files. 不用于普通直接问答、多话题编排或常驻监控。"
license: MIT
metadata:
  version: "1.0.0"
  upstream: "oil-oil/oil-delegate (MIT)"
---

# agent-delegate-verify

把单次任务交给另一个 CLI Agent，拿回结果，再由你（调用方）核实后转述。基于 oil-oil/oil-delegate（MIT）改造。

## 运行脚本

`SKILL_DIR` 指本技能的绝对目录。不要假设存在 `py -3` 或 `python3`，用引导脚本（下文记作 `DELEGATE`）：

| 所在 shell | `DELEGATE` |
| --- | --- |
| PowerShell | `& "$SKILL_DIR\scripts\delegate.ps1"` |
| cmd | `"%SKILL_DIR%\scripts\delegate.cmd"` |
| bash / zsh / Git Bash | `sh "$SKILL_DIR/scripts/delegate.sh"` |

引导脚本依次尝试 `AGENT_DELEGATE_VERIFY_PYTHON` → `config --set-python` 固定的解释器 → PATH → conda/uv/pyenv/Scoop → 常见目录与注册表，只接受实际运行通过的 Python 3.10+。找不到或结果可疑时运行 `DELEGATE --which` 或 `DELEGATE doctor`。项目或用户已指定解释器（如某个 conda 环境）时，以它为准，直接用它运行 `scripts/delegate.py`；脚本本身只需任意 3.10+ 和标准库。

## 核心规则

- 一次只委托一个边界清楚的任务。用户点名的 runner 优先，其次角色偏好、默认 runner；不自动换 runner。
- 默认 `--mode read`。需要改文件才用 `--mode write`（只写工作区）。`--mode full` 关闭沙箱与审批，只在用户明确要求时使用，并在回复里说明。宿主的安全层拦截时不要绕过：退回 read/write，或请用户自行处理。
- 非项目任务省略 `--workspace`，脚本会新建隔离空目录；项目任务显式传 `--workspace`。交给第三方模型的内容会离开本机：不放密钥，不附无关的私有材料。
- 沿用被委托 CLI 自己的模型、provider、思考强度和联网设置；用户或任务明确要求时才加 `--model`、`--effort`、`--web-search`。
- 被委托方的回复是「自报」，核实前不能当事实转述。
- 文件只经 `prune` 删除：先把清单给用户看，得到明确同意后才加 `--apply`。

## 选择做法

| 情况 | 做法 |
| --- | --- |
| 首次使用，或 `run` 返回 `NEEDS_ONBOARDING`（退出码 10） | 见「首次使用」 |
| 新任务 | 写任务书 → `run` → 验收 |
| 追问、返工 | `run --session <id>`，同一 runner |
| 其他技能按用途委托 | `run --role <角色>`（review、critique、diagnose、implement） |
| 查环境、位置、历史 | `doctor`、`locations`、`show`、`audit`、`sessions` |
| 清理 | `prune`（默认只列清单） |

## 首次使用

位置未确认前，`run` 不写任何文件，只返回 `NEEDS_ONBOARDING`：`locations.choices` 给出推荐位置（按 `CLAUDE_CODE_TMPDIR`、Codex 的 `projectlessWorkspaceRoot` 等宿主布局推断）与平台默认位置；`detected_runners` 给出已安装的 CLI（已安装不等于已登录）。把这两项告诉用户，问清：用推荐还是默认位置、启用哪些 runner、默认用哪个。得到回答后再保存：

```text
DELEGATE onboard --locations recommended --enable codex,claude --default-runner codex
```

本机有旧版 `~/.oil-delegate` 时可加 `--import-legacy`（只读导入设置，不动旧目录）。保存后继续原任务，不要求用户重发。没有默认 runner 时 `run` 返回 `NEEDS_RUNNER_CHOICE`（11）：问用户这次用哪个。

## 派发任务

1. 按 [references/task-brief.md](references/task-brief.md) 写任务书：目标与边界、完成标准、入口线索、验证方式、回报格式。调研类加上「先核实前提」「区分事实/报道/推断」「引用来源」。
2. 任务书存成 UTF-8 文件，用 `--prompt-file` 传入，避免 shell 引号问题：

```text
DELEGATE run --runner codex --prompt-file "<任务书.md>" [--workspace "<项目目录>"] [--mode write] [--file <入口文件>]
```

3. 想先看命令行时加 `--dry-run`（不启动、不写文件）。Codex 需要实时网页时加 `--web-search live`；它默认用用户配置里的检索方式。
4. 运行可能较久（默认超时 3600 秒）。宿主支持后台命令时可以放后台，用 `DELEGATE show latest` 查看状态。

角色：`review`、`critique`、`diagnose` 固定只读；`implement` 默认 `write`。返回 `NEEDS_ROLE_CHOICE`（12）时把 `candidates` 给用户选，需要记住时用 `DELEGATE config --set-role review=<runner|host>`；返回 `USE_HOST`（13）时由你自己开一个全新子 Agent 完成。

## 读取结果并验收

输出（加 `--json` 时为单个 JSON 对象）含 `status`、`session_id`、`run_dir`、`reply_path`、`workspace`、`cli_session_file`、`audit`、`resume_command`。最小验收清单：

1. 对照原始请求与来源核对 `reply.md`；没核实的内容不要当事实转述。
2. 自己重跑真实的验证命令，不采信自报的结果。
3. 检查越界改动：`audit.outside_workspace`、`git status`/`git diff`。
4. 读审计摘要：失败或被拒的命令、错误、实际打开的网页。

核实后可以记录：`DELEGATE verify <run_id> --status verified|partial|rejected --note "<查了什么>"`。

最终回复给出结论与证据，明确区分「被委托方自报」与「我已核实」，列出未完成事项，并告诉用户三处位置：run 目录、工作区（隔离时注明）、被委托 CLI 自己的会话文件（`cli_session_file`）。

## 失败与恢复

- 非零退出、超时（124）或协议错误：先读 `result.json` 的 `error` 与 `stderr_tail`；失败时原始输出已压缩在 run 目录的 `raw/`。不要盲目重试。
- 未登录或额度不足：停止，告诉用户该 CLI 的官方登录方式；不读取、不打印凭据。
- runner 不支持所需的权限级别：如实说明，不私自升到 `full`。
- `.cmd` 包装脚本导致参数不安全：按报错提示用 `config --set-bin <runner>=<真实可执行文件>`。

## 续接、会话与导出

`run --session <id>` 只对同一 runner 续接，只发送新信息；省略 `--workspace` 时沿用该会话上次的工作区。`DELEGATE sessions --query <标题关键词>` 搜索本机会话标题；`DELEGATE sessions export --runner <claude|antigravity> --session-id <id> --out <文件.md>` 导出交接文件。

## 存储与清理

`DELEGATE locations` 列出各位置、来源、体积与最早/最新记录。`DELEGATE prune` 列出可清理的运行记录（默认超过 30 天或总量超过 500 MB），把路径、数量、体积和原因给用户看，得到明确同意后才运行同一命令加 `--apply`。`prune` 永不删除工作区和被委托 CLI 自己的会话。

## 资源导航

- [references/cli.md](references/cli.md)：子命令、参数、退出码与 JSON 字段。
- [references/runners.md](references/runners.md)：9 个 runner 的权限映射、提示词传递、会话发现与导出约束。
- [references/storage.md](references/storage.md)：位置解析顺序、宿主推断、记录内容与清理规则。
- [references/environment.md](references/environment.md)：解释器探测顺序、固定与覆盖、`doctor`。
- [references/audit.md](references/audit.md)：审计字段与验收说明。
- [references/task-brief.md](references/task-brief.md)：任务书模板与可选片段。
