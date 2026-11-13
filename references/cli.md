# 命令参考

`DELEGATE` 是引导脚本（见 SKILL.md「运行脚本」），也可以用任意 Python 3.10+ 直接运行 `scripts/delegate.py`。全局参数写在子命令之前。

## 全局参数

| 参数 | 作用 |
| --- | --- |
| `--home DIR` | `config.json` 所在目录（也可用环境变量 `AGENT_DELEGATE_VERIFY_HOME`） |
| `--records-dir DIR` | 本次调用的运行记录目录 |
| `--cache-dir DIR` | 本次调用的在途缓存目录 |
| `--workspace-root DIR` | 本次调用的隔离工作区根目录 |
| `--version` | 打印版本 |

## 子命令

| 子命令 | 用途 | 是否写文件 |
| --- | --- | --- |
| `onboard [--scan] [--locations recommended\|default] [--enable LIST] [--default-runner R\|ask] [--import-legacy [DIR]]` | 首次配置：确认位置、启用 runner | `--scan` 不写；其余写 `config.json` |
| `config [选项…]` | 查看或修改配置（不带选项时只读，且不探测 CLI 版本） | 有修改时写 `config.json` |
| `run …` | 派发或续接一个任务 | 写 run 目录；`--dry-run` 不写 |
| `show [RUN]` | 查看一次运行的状态、文件、核实情况 | 否 |
| `verify [RUN] --status verified\|partial\|rejected [--note T] [--check T]…` | 记录调用方的核实结论（不执行任何检查） | 改该 run 的 `result.json` |
| `audit [RUN\|事件文件] [--runner R] [--workspace DIR]` | 审计摘要；事件文件可为 `.jsonl` 或 `.jsonl.gz` | 否 |
| `locations` | 各位置、来源、体积、文件数、最早/最新记录 | 否 |
| `prune [--older-than D] [--max-total-mb M] [--apply]` | 默认只列出可清理的记录；`--apply` 才删除 | 仅 `--apply` |
| `doctor` | 解释器、conda/uv、runner、位置、宿主配置的诊断 | 否 |
| `sessions [--runner R] [--query Q] [--limit N]` | 搜索本机会话标题（只读元数据） | 否 |
| `sessions export --runner R --session-id ID --out FILE [--max-turns N] [--force]` | 导出 Claude/Antigravity 会话为交接 Markdown | 写 `--out` |

`RUN` 可以是 run id、run 目录或 `latest`（默认）。除 `sessions export` 外，所有子命令都支持 `--json`，输出单个 JSON 对象并带 `contract_version: 1`。

## `config` 选项

| 选项 | 作用 |
| --- | --- |
| `--set-enabled-runners LIST`、`--set-default-runner R\|ask` | 启用池与默认 runner |
| `--set-bin R=PATH`、`--clear-bin R` | 指定或清除可执行文件路径（`.py` 文件会用当前 Python 运行） |
| `--set-role ROLE=R\|host\|ask` | 角色偏好；`diagnose` 需要两个不同选择，逗号分隔 |
| `--set-location KIND=PATH\|default` | `records`、`cache`、`workspaces` 的位置；会标记位置已确认 |
| `--set-python PATH\|current\|clear` | 固定引导脚本使用的解释器（写入前先执行校验） |
| `--keep-raw on\|off` | 成功的运行也压缩保存原始事件流 |
| `--env-hints on\|off` | 是否在提示词里附解释器与 conda 路径 |
| `--retention-days N`、`--max-total-mb M` | `prune` 的默认规则 |
| `--import-legacy [DIR]` | 只读导入旧版 `~/.oil-delegate/config.json` |

## `run` 参数

| 参数 | 说明 |
| --- | --- |
| `--prompt TEXT` / `--prompt -` / `--prompt-file PATH` | 三选一；`-` 从 stdin 读；文件须为 UTF-8（可带 BOM） |
| `--runner R` | 名称或别名，不区分大小写 |
| `--workspace DIR` | 项目目录；省略时新建隔离空目录（续接时沿用上次的工作区） |
| `--slug NAME` | 隔离工作区的名称，默认 `<runner>-task` |
| `--mode read\|write\|full` | 默认 `read`；角色会决定或限制它 |
| `--role ROLE`、`--role-slot N` | 按用途委托；`diagnose` 用 1 或 2 |
| `--file PATH` | 入口文件，可重复；只写进提示词 |
| `--session ID` | 续接同一 runner 的会话 |
| `--timeout SEC` | 默认 3600 |
| `--model M`、`--effort E` | 仅 codex、claude，用户要求时才用 |
| `--web-search disabled\|cached\|indexed\|live` | 仅 codex，按次覆盖 `web_search` |
| `--keep-raw` | 本次也压缩保存原始事件流 |
| `--no-env-hints` | 本次不附环境提示 |
| `--dry-run` | 只显示将执行的命令与位置，不启动、不写文件 |
| `--json` | 输出单个 JSON 对象 |

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 参数、配置或运行错误（`--json` 时输出 `status: ERROR`） |
| 9 | 引导脚本找不到可用的 Python 3.10+，或 `AGENT_DELEGATE_VERIFY_PYTHON` 不可用 |
| 10 | `NEEDS_ONBOARDING`：需要确认位置和/或启用 runner |
| 11 | `NEEDS_RUNNER_CHOICE`：没有默认 runner |
| 12 | `NEEDS_ROLE_CHOICE`：该角色还没选过 Agent |
| 13 | `USE_HOST`：用户偏好由宿主自己的子 Agent 完成 |
| 124 | 超时，进程树已终止 |
| 127 | 无法启动被委托 CLI |
| 其他 | 被委托 CLI 的退出码 |

## `run` 结果字段（`result.json` 与 `--json` 输出）

| 字段 | 说明 |
| --- | --- |
| `status` | `running`（运行中写入）、`completed`、`failed` |
| `run_id`、`run_dir`、`prompt_path`、`reply_path`、`result_path` | 本次运行的记录 |
| `runner`、`role`、`mode`、`permission`、`danger` | `danger: true` 表示用了 `full` |
| `session_id`、`resume_commands`（`posix`/`powershell`/`cmd`）、`resume_command` | 续接方式；`resume_command` 是本平台推荐的一种 |
| `workspace` | `{path, isolated}` |
| `cli_session_file` | 被委托 CLI 自己的会话文件（Codex rollout、Claude JSONL）或说明 |
| `raw_events` | `{paths, policy}`：原始事件流是否保存、为什么 |
| `response`、`self_report` | 被委托方的回复（自报） |
| `caller_verification` | `{status: pending\|verified\|partial\|rejected, checklist, note, checks}` |
| `audit` | 审计摘要，见 audit.md |
| `error`、`exit_code`、`timed_out`、`stderr_tail` | 失败信息 |
| `command`、`environment_overrides`、`launcher` | 实际命令行（提示词替换为 `<prompt>`）与启动方式 |
| `options`、`started_at`、`finished_at`、`elapsed_seconds` | 其他 |
