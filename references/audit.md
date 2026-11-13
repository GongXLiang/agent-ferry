# 审计与验收

## 审计摘要（`result.json` 的 `audit`）

| runner | 支持程度 | 来源 |
| --- | --- | --- |
| codex | 完整 | `codex exec --json` 事件流 |
| claude、workbuddy | 部分 | 最终 JSON 的 `permission_denials`、`num_turns`、`total_cost_usd`；没有逐条工具事件 |
| 其他 | 不支持（如实标明） | 请查看该 CLI 自己的会话记录 |

Codex 摘要字段（依据 `codex-rs/exec/src/exec_events.rs` 的事件定义，并对照本机真实运行的事件结构）：

| 字段 | 来自 |
| --- | --- |
| `commands` | `command_execution`：命令、退出码、状态（`completed`/`failed`/`declined`；只有开始没有结束的记为 `incomplete`） |
| `failed_commands`、`declined_commands` | 状态为 failed 或退出码非 0；被沙箱或审批拒绝 |
| `file_changes`、`outside_workspace` | `file_change` 的路径与类型（add/delete/update）；标出工作区以外的路径 |
| `web.searches`、`web.pages`、`web.find_in_page` | `web_search` 的查询（`action.queries`/`query`）、打开的网页（`open_page`）、页内查找 |
| `mcp_calls`、`collab_calls`、`todo` | `mcp_tool_call`、`collab_tool_call`、`todo_list` |
| `errors` | `error` 条目、`turn.failed`、流级 `error` |
| `usage` | 各轮 `turn.completed` 的用量之和 |
| `summary` | 以上各项的计数 |

长字段截断到 300 字符，列表最多保留 200～500 条，并标注是否截断。

注意：`file_changes` 只来自 `file_change` 事件（补丁式编辑）。模型用 shell 命令（重定向、`Set-Content` 等）写文件时，改动只出现在 `commands` 里，不会出现在 `file_changes` 或 `outside_workspace`。越界检查因此不能只看审计摘要，还要看 `git status` 或对比目录。

`audit` 子命令：对 run 优先用保存的原始事件重算，没有保存时显示 `result.json` 里的摘要；也可以直接审计一个事件文件（`.jsonl` 或 `.jsonl.gz`，例如旧版 oil-delegate 的 `events.jsonl`），用 `--workspace` 标记越界改动。

## 自报与核实

`result.json` 把两类信息分开：

- `self_report`（以及 `response`、`reply.md`）：被委托方说了什么。
- `caller_verification`：调用方核实了什么，初始为 `pending`，用 `verify <run> --status verified|partial|rejected --note … --check …` 记录。`verify` 只记录，不执行任何检查。

## 最小验收清单

1. **对照原文**：回到原始请求和被委托方引用的来源，核对关键结论；没核实的内容不当事实转述。
2. **重跑验证**：自己运行真实的测试、构建或检查命令；不采信自报的「测试通过」。
3. **查越界改动**：看 `audit.outside_workspace`，在工作区运行 `git status`/`git diff`，确认测试没有被删除、跳过或放宽。
4. **看审计摘要**：失败或被拒的命令、错误、实际打开的网页是否支撑结论。

## 最终回复建议包含

- 结论与支撑证据；明确哪些是被委托方自报、哪些已由你核实。
- 实际改动与验证命令、结果；未完成或无法核实的事项。
- 三处位置：run 目录、工作区（隔离时注明）、被委托 CLI 的会话文件（`cli_session_file`）。
- 需要继续时的 session ID 或续接命令。
