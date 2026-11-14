# 存储位置与清理

## 四个位置

| 位置 | 内容 | 解析顺序（先到先得） |
| --- | --- | --- |
| `home` | `config.json` | `--home` → `AGENT_FERRY_HOME` → 平台默认 |
| `records` | 每次运行一个目录：任务书、最终回复、`result.json` | `--records-dir` → `AGENT_FERRY_RECORDS_DIR` → `config.json` → 宿主推断 → `<home>/runs` |
| `cache` | 运行中的原始输出 | `--cache-dir` → `AGENT_FERRY_CACHE_DIR` → `config.json` → 宿主推断 → 平台缓存目录 |
| `workspaces` | 非项目任务的隔离空目录 | `--workspace-root` → `AGENT_FERRY_WORKSPACE_ROOT` → `config.json` → 宿主推断 → `<home>/workspaces` |

平台默认：

| 平台 | home | cache |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\agent-ferry` | `%LOCALAPPDATA%\agent-ferry\cache` |
| macOS | `~/Library/Application Support/agent-ferry` | `~/Library/Caches/agent-ferry` |
| Linux | `$XDG_STATE_HOME/agent-ferry`（缺省 `~/.local/state`） | `$XDG_CACHE_HOME/agent-ferry`（缺省 `~/.cache`） |

`config.json` 固定放在 `home`，因为引导脚本要在没有 Python 的情况下读取固定的解释器；它只有几 KB。占空间的记录都可以搬到别处。

## 宿主推断

只在命令行、环境变量和配置都没有指定时使用：

- `workspaces`：读取 `$CODEX_HOME/config.toml`（缺省 `~/.codex`）的 `[desktop].projectlessWorkspaceRoot`，沿用 Codex 的 `<根>/<YYYY-MM-DD>/<名称>` 命名，重名依次加 `-2`、`-3`。
- `cache`：`CLAUDE_CODE_TMPDIR/agent-ferry`。
- `records`：宿主数据目录的兄弟目录。锚点依次为 `CLAUDE_CODE_TMPDIR`（取上一级作为 Claude 数据目录）、Codex 的 `projectlessWorkspaceRoot`（同上）、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`。例如 `CLAUDE_CODE_TMPDIR=E:\Data\Claude\temp` 推出 `E:\Data\agent-ferry\runs`。锚点位于用户主目录内（说明没有刻意搬家）时不推断；推出的目录恰为主目录或盘符根时也放弃。

## 首次确认

位置未确认时，`run` 不写任何文件，返回 `NEEDS_ONBOARDING`，其中 `locations.choices` 同时给出：

- `recommended`：按上面的宿主推断得到的路径和理由；推断不出时就是平台默认。
- `default`：平台默认路径。

`onboard --locations recommended|default` 把所选的具体路径写进 `config.json`（之后不再随环境变量变化，换来可预期）。以后可用 `config --set-location records=<路径>` 单独修改，`=default` 恢复推断与默认。三个位置都由命令行或环境变量显式给出时视为已确认。`run --dry-run` 什么都不写，所以不受此限制。

## 每次运行写什么

```text
<records>/<YYYYMMDDTHHMMSS>-<runner>-<6位十六进制>/
  prompt.md      实际发给被委托方的任务书（含运行时上下文）
  reply.md       被委托方的最终回复（标注为自报）和错误
  result.json    状态、位置、审计摘要、续接命令、核实状态
  raw/           仅在失败、--keep-raw 或 keep_raw=on 时：stdout.log.gz、stderr.log.gz
```

- 运行开始就写入 `status: running` 的 `result.json`，并在 stderr 打印 run 目录与工作区。
- 原始输出先流式写到 `<cache>/inflight/<run id>/`，结束后提取审计摘要，再按规则压缩保存或删除（只删本次运行自己创建的在途文件）。
- 被委托 CLI 自己保存了完整对话：`cli_session_file` 指向它（Codex 由 thread id 在 `$CODEX_HOME/sessions` 下找 rollout 文件；Claude 为 `$CLAUDE_CONFIG_DIR/projects/<编码后的 cwd>/<id>.jsonl`；其他 runner 注明未接入）。本技能从不复制、移动或删除这些文件。

## 查询：`locations`

列出 home、records、cache、workspaces 的路径、来源（cli/env/config/inferred/default）、体积、文件数、最早与最新记录；隔离工作区只统计运行记录里登记过的那些。另外列出被委托 CLI 的会话目录（是链接时显示目标）与旧版 `~/.oil-delegate`（只读）。

## 清理：`prune`

- 默认只预览：列出路径、文件数、体积和原因，不删除任何东西。
- 规则：超过保留天数（默认 30 天）的 run 目录；总量超过上限（默认 500 MB）时从最旧的开始；超过 24 小时的孤立在途缓存。正在运行（24 小时内状态为 `running`）的记录跳过。
- 只有加 `--apply` 才删除，且只删目录名符合 run id 格式、位于 records 或 cache 内、内部不含任何链接或联接的目录。
- 永不删除：被委托 CLI 的会话文件、任何工作区（含隔离工作区）、`config.json`、旧版目录。
- 默认值可改：`config --retention-days N --max-total-mb M`；也可按次传 `--older-than`、`--max-total-mb`。

## 旧版导入

`config --import-legacy [DIR]`（或 `onboard --import-legacy`）只读读取 `~/.oil-delegate/config.json` 中的启用列表、默认 runner、可执行文件路径和角色偏好，逐项校验后写入新配置；不读运行记录，不修改也不删除旧目录。
