# Runner 适配参考

适配知识主要来自上游 oil-oil/oil-delegate（MIT），本项目改了权限级别、提示词传递与 Windows 启动方式。只在首次配置、排查某个 CLI 或维护适配器时阅读。

## 共同约束

- 正式名称：`claude`、`workbuddy`、`antigravity`、`codex`、`opencode`、`dsh`、`grok`、`kimi`、`pi`。别名：`cc/claude-code`、`wb/codebuddy/cbc`、`agy/anti`、`deepseek/deepseek-harness`、`grok-build`、`kimi-code`、`pi-agent/pi-coding-agent`。不区分大小写。
- 可执行文件查找顺序：环境变量 `AGENT_FERRY_<RUNNER>_BIN` → `config --set-bin` → PATH → 已知目录（`~/.local/bin`、`~/.kimi-code/bin`；Windows 另有 `%APPDATA%\npm`；macOS/Linux 另有 `/opt/homebrew/bin`、`/usr/local/bin`）。
- 所有调用都用参数数组，提示词不经过 shell。默认不指定模型、provider 或思考强度。
- 权限参数只影响本次进程，不修改目标 CLI 的永久配置。任何级别都不是安全隔离的保证：静态 deny、hooks、管理锁、登录、额度和网络仍可能导致失败。

## 权限级别

| runner | read（默认） | write（只写工作区） | full（显式） |
| --- | --- | --- | --- |
| codex | `--sandbox read-only -c approval_policy=never` | `--sandbox workspace-write -c approval_policy=never` | `--dangerously-bypass-approvals-and-sandbox` |
| claude | `--permission-mode plan --permission-prompts none` | `--permission-mode acceptEdits --permission-prompts none` | `--dangerously-skip-permissions` |
| opencode | `--agent plan`；`OPENCODE_PERMISSION` 拒绝 edit、bash、external_directory | 允许 edit；拒绝 bash、external_directory | 全部允许 |
| dsh | `--permission-mode read-only` | `--permission-mode workspace-write` | `--permission-mode danger-full-access` |
| workbuddy | `--permission-mode plan -y` | 不提供 | `--permission-mode bypassPermissions -y`，并设 `CODEBUDDY_IS_SANDBOX=1` |
| antigravity | `--mode plan` | 不提供 | `--dangerously-skip-permissions` |
| grok | `--permission-mode plan` | 不提供 | `--always-approve` |
| pi | `--no-extensions --no-mcp --tools read,grep,find,ls` | 不提供 | 不加限制 |
| kimi | 不提供 | 不提供 | 默认参数（`-p` 自带 auto 权限） |

- 「不提供」表示无法确认该 CLI 能把写入限制在工作区内，或无法保证只读，所以直接报错，不换成更高权限。
- OpenCode 的 `question` 始终拒绝，避免无人值守时等待输入。DSH 支持时加 `--approval-policy never`；旧版没有 `--permission-mode` 时改设 `DSH_PERMISSION_MODE`。WorkBuddy 所有级别都设 `CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS=1`。
- 角色：`review`、`critique`、`diagnose` 固定 read，kimi 不能配给它们；`implement` 默认 write，用户明确同意时可 `--mode full`。

## Codex 专项

- 新建：`codex exec --cd <workspace> --json --skip-git-repo-check <权限> [-m] [-c …] -`，提示词从 stdin 传入。
- 续接：`codex exec resume --json --skip-git-repo-check -c sandbox_mode=<…> -c approval_policy=never <thread_id> -`（`exec resume` 没有 `--sandbox`）。
- `exec` 是非交互的，用户交互式配置里的 `approval_policy = "on-request"` 在这里没有审批人，所以显式传 `never`；沙箱由权限级别决定。模型、provider、思考强度、`web_search` 一律沿用用户配置。
- 联网检索：`web_search` 取值以本机 Codex（0.169.0 实测）为准：`disabled`、`cached`、`indexed`、`live`。官方文档说明：`cached` 使用 OpenAI 维护的网页索引缓存（默认，降低提示注入风险）；`indexed` 只在搜索索引放行时访问外部网页；`live` 抓取最新网页，等同交互式的 `--search`；`disabled` 关闭工具。`codex exec` 没有 `--search`，按次开启实时检索用 `--web-search live`（即 `-c web_search=live`）。只读沙箱不影响检索工具。
- 从不默认使用 `--trust`（会永久写入信任设置，`--ephemeral` 也挡不住）和 `--ephemeral`（会话无法续接，也没有会话文件可指向）。
- 解析：JSONL 中的 `thread.started`、`item.completed` 的 `agent_message`、`turn.failed` 与 `error`。审计见 audit.md。

## 其他 runner

| runner | 新建 | 续接 | 输出解析 |
| --- | --- | --- | --- |
| claude | `claude -p --output-format json`，提示词走 stdin | `--resume <session_id>` | 最终 JSON 的 `session_id`、`result`、`is_error`、`permission_denials` |
| workbuddy | `codebuddy -p <prompt> --output-format json` | `--resume` | 与 Claude 兼容的最终 JSON |
| antigravity | `agy -p <prompt> --output-format json` | `--conversation` | `conversation_id`、`response`、`status`（只有 `SUCCESS` 算成功） |
| opencode | `opencode run --format json --dir <ws> <prompt>` | `--session` | 事件里的 session id、`text` 事件的 `part.text`、`error` |
| dsh | `dsh --profile headless [--permission-mode …] --json -`，提示词走 stdin | `--session-id`（须在原 cwd） | `session`、`final.text`、`final.reason` 或 `status turn_end` |
| grok | `grok --cwd <ws> -p <prompt> --output-format json` | `--resume` | `sessionId`、`text`、`stopReason` |
| kimi | `kimi -p <prompt> --output-format stream-json` | `-S` | assistant 文本与 `session.resume_hint` |
| pi | `pi --mode json`，提示词走 stdin | `--session <UUID>` | `session`、`message_end`、`agent_settled` |

DSH 在 onboarding 和每次运行前执行 `dsh --profile headless --help` 检查 `--json`、`--session-id`、`--permission-mode`、`--approval-policy`；缺 `--json` 不接任务，缺 `--session-id` 不续接。DSH 续接必须保持原 cwd，其 ownership、preset 与 cwd 校验失败时如实报告。

## 提示词传递与 Windows 启动

- 走 stdin：codex、dsh、pi、claude。走命令行参数：workbuddy、antigravity、grok、kimi、opencode（这些 CLI 的 stdin 输入方式未核实）。
- 命令行参数在 Windows 上总长不能超过约 32K 字符，Linux 单个参数不能超过 128 KiB：超长时直接报错，请缩短提示词并用 `--file` 指向细节文件，或改用走 stdin 的 runner。
- Windows 上的 `.cmd`/`.bat` 会经过 cmd.exe，它会改写引号、`%` 和换行：
  - npm 生成的 `.cmd`（npm cmd-shim 格式）会解析出背后的 JS 入口，用 `node <脚本>` 直接启动，绕开 cmd.exe。
  - 其他 `.cmd` 只在提示词走 stdin、且其余参数不含 `"`、`%`、换行时才允许；否则报错，请用 `config --set-bin` 指向真实可执行文件。
- 超时：Windows 用 `taskkill /T /F` 终止整棵进程树；POSIX 终止整个进程组。

## 续接命令

`resume_commands` 同时给出 `posix`、`powershell`、`cmd` 三种写法，`resume_command` 是本平台推荐的一种（Windows 为 PowerShell）。可执行文件在 PATH 上能直接找到时用短名（如 `codex resume <id>`）；否则 PowerShell 用 `& '<路径>' …`，cmd 用 `"<路径>" …`。各 runner 的交互式续接：claude/workbuddy/grok `--resume`，antigravity `--conversation`，codex `resume`，opencode/pi `--session`，dsh `tui --resume`，kimi `-S`。

## 会话发现约束（`sessions`）

只读取标题、ID、目录和更新时间，不启动任务、不调用模型、不联网、不写文件。

| 数据根目录 | 默认值 | 覆盖方式 |
| --- | --- | --- |
| Kimi Code | `~/.kimi-code` | `KIMI_CODE_HOME`（官方） |
| Codex | `~/.codex` | `CODEX_HOME`（官方） |
| Antigravity CLI / IDE | `~/.gemini/antigravity-cli`、`~/.gemini/antigravity` | `AGENT_FERRY_ANTIGRAVITY_CLI_HOME`、`AGENT_FERRY_ANTIGRAVITY_HOME` |
| Claude 桌面元数据 | macOS `~/Library/Application Support/Claude/claude-code-sessions`；Windows `%APPDATA%\Claude\claude-code-sessions`、Linux `~/.config/Claude/claude-code-sessions`（后两者未验证） | `AGENT_FERRY_CLAUDE_DESKTOP_HOME` |
| Claude Code 数据 | `~/.claude` | `CLAUDE_CONFIG_DIR`（官方） |

- 数据根目录本身可以是符号链接或 Windows 目录联接（常用于把数据搬到别的盘），会解析一次；根目录内部的链接一律不跟随。云同步目录（iCloud、CloudStorage 等）和 macOS 云占位文件会被拒绝。上游会拒绝符号链接根目录和挂载点下的路径，这里有意放宽并有回归测试。
- Codex：逐行读 `session_index.jsonl` 的 `id`、`thread_name`、`updated_at`，同 ID 以最后一条有效记录为准；只为最终返回的会话在 `sessions/**/rollout-*-<id>.jsonl` 读第一行的 `session_meta.payload.cwd`。不读归档的 `.jsonl.zst`。
- Kimi：调用官方 `kimi session list --all --json`。Antigravity：以 `immutable=1` 只读打开 `conversation_summaries.db`，只查元数据列；IDE 会话不能由 agy 续接。Claude：读桌面元数据的 `cliSessionId`、`cwd`、`title`、`lastActivityAt`，对应 JSONL 存在时才标为可续接。
- workbuddy、opencode、dsh、grok、pi 没有可验证的纯元数据列表接口，列为 `unsupported`。
- 坏行逐行跳过并计入 `skipped`；来源读取失败计入 `errors`，不影响其他来源。

## 会话导出约束（`sessions export`）

只支持 Antigravity 与 Claude，离线读取指定会话，写入标题、目录、时间与按时间排序的用户/Agent 文字，跳过思考、工具、图片与二进制；默认保留最近 30 轮。输出父目录必须存在，已有文件须加 `--force`，拒绝符号链接输出和写入 CLI 数据目录。Antigravity 只解析 `step_format=0` 的类型 14（用户 `19→2`）与 15（回复 `20→1`），用带边界检查的 protobuf 解码，未知格式明确报错。

## 失败处理

- 找不到命令：安装目标 CLI，或 `config --set-bin <runner>=<路径>`；不会自动换 runner。
- 未认证或额度不足：停止并给出该 CLI 的官方登录方式；不读取或打印凭据。
- 权限被拒：先检查静态 deny、hooks、管理锁和实际级别；不自动升级权限。
- 输出无法解析：失败时 run 目录的 `raw/` 保存了压缩的 stdout/stderr；先确认 CLI 版本与输出格式，不把空回复当成功。
- 超时：进程树已终止；先缩小任务或确认是否卡在登录、权限或网络，不要盲目重试。
- 续接失败：核对 runner、session ID、cwd 与会话归属；跨 runner 必须新建任务。
