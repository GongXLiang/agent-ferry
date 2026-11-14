# agent-ferry 设计说明

> 本项目基于 [oil-oil/oil-delegate](https://github.com/oil-oil/oil-delegate)（MIT）改造。本文按「需求 → 方案 → 取舍」记录设计，先于实现写成，实现后按实际结果修订。

## 0. 定位

宿主 Agent（Claude Code、Codex 等）把**一项边界清楚的任务**交给另一个 CLI Agent，拿回结构化结果、审计摘要和续接方式；调用方核实后再转述给用户。它不是编排器：不做多话题看板、任务 DAG、常驻进程。

一次委托的生命周期：

```text
引导脚本找 Python ──> 主脚本 run ──> 解析位置/权限/工作区 ──> 启动被委托 CLI（流式写入 cache）
      ──> 解析回复 + 生成审计摘要 ──> 写 records（任务书、回复、result.json）──> 清理 cache
      ──> 输出状态、位置、续接命令 ──> 调用方按清单核实，再告诉用户
```

## 1. 需求 → 方案

### P0-A 解释器与环境识别

**问题**：上游 SKILL.md 写死 `py -3`（Windows）/`python3`，脚本本身不探测；没有 `py` 启动器或只装了 conda 的机器上，宿主 Agent 只能自己找解释器。

**方案**

- **引导脚本**（不依赖 Python）：`scripts/delegate.ps1`（Windows PowerShell 5.1+ / PowerShell 7）、`scripts/delegate.sh`（POSIX sh，也可在 Git Bash 里用）、`scripts/delegate.cmd`（转调 ps1）。按顺序取第一个可用的解释器，然后把全部参数转给 `scripts/delegate.py`：
  1. 环境变量 `AGENT_FERRY_PYTHON`：显式覆盖，不可用就报错退出，不悄悄换成别的（借鉴 orchestration「选定的可执行文件跑不起来就报原样错误并停止」）。
  2. `config.json` 里用 `config --set-python` 固定的路径：不可用时警告并继续探测。
  3. PATH：Windows 依次 `python`、`python3`、`py -3`；POSIX 依次 `python3`、`python`。
  4. 环境与包管理器：`CONDA_PREFIX`（已激活环境）、`CONDA_EXE`/`MAMBA_EXE`/`MAMBA_ROOT_PREFIX` 推出的 base 及其 `envs/*`、`conda info --base`、`uv python find`（设置 `UV_PYTHON_DOWNLOADS=never`，只找不下载）、pyenv / pyenv-win、asdf、mise、Scoop（`SCOOP` 或默认根下的 `python*`、`miniconda3`、`anaconda3`、`miniforge`、`mambaforge`）。
  5. 常见安装目录（含 `~/miniconda3` 等 conda 默认目录）；Windows 再查注册表 PEP 514（HKCU、HKLM、WOW6432Node）。
- 每个候选都**真的执行**一次 `-I -c`，取版本和 `sys.executable`。低于 3.10、执行失败、Microsoft Store 占位 `python.exe` 一律拒绝并记录原因。探测代码不含双引号和 `%`，避开 PowerShell 5.1 的参数引号缺陷和 cmd 的变量展开。
- `--which`：只打印选中的解释器、来源和被拒候选，不启动主脚本，便于排错。
- 主脚本只用标准库并兼容 3.10：`tomllib`（3.11+）缺失时用内置的只读小型 TOML 解析器，只取需要的键。
- **`doctor`**：Python 侧实现同一套探测，列出接受/拒绝的解释器及原因，conda/uv/pyenv/Scoop 的位置，9 个 runner 的可执行文件、启动方式与版本，全部存储位置与体积，Codex/Claude 配置的发现结果；支持 `--json`。只读，不写配置。
- **固定与覆盖**：`config --set-python PATH|current|clear`（写入前同样执行校验）；环境变量覆盖优先于固定值。
- **环境提示**（可关闭）：默认在委托提示词的运行时上下文里附上「当前解释器路径与版本、conda base 路径」，让被委托方不必重新探测。只含路径与版本，不含任何环境变量值；`--no-env-hints` 或 `config --env-hints off` 关闭。

**取舍**：探测逻辑在 ps1、sh、Python 三处各实现一遍，有重复维护成本；但引导必须先于 Python 运行，而 `doctor` 需要可测试的完整列表，所以接受重复，用测试约束三者在关键场景上选择一致。

### P0-B 存储位置与磁盘卫生

**问题**：上游把配置和运行记录都写进 `~/.oil-delegate`，每次 run 还保存一份原始事件流（等于把被委托 CLI 自己已保存的对话再存一份）；位置不随宿主、不告知用户。

**方案：四个位置（配置、记录、缓存、隔离工作区）**

| 位置 | 类别 | 内容 | 解析顺序（先到先得） |
| --- | --- | --- | --- |
| `home` | 配置 | `config.json`（几 KB） | `--home` → `AGENT_FERRY_HOME` → 平台默认 |
| `records` | 长期记录 | 每次 run 的任务书、最终回复、`result.json`（含审计摘要） | `--records-dir` → `AGENT_FERRY_RECORDS_DIR` → 配置 → 宿主推断 → `<home>/runs` |
| `cache` | 临时 | 运行中的原始输出；run 结束即删除本次的在途文件 | `--cache-dir` → `AGENT_FERRY_CACHE_DIR` → 配置 → `CLAUDE_CODE_TMPDIR/agent-ferry` → 平台缓存目录 |
| `workspaces` | 隔离工作区 | 非项目任务的空目录 | `--workspace-root` → `AGENT_FERRY_WORKSPACE_ROOT` → 配置 → Codex `[desktop].projectlessWorkspaceRoot` → `<home>/workspaces` |

- 平台默认：`home` 为 Windows `%LOCALAPPDATA%\agent-ferry`、macOS `~/Library/Application Support/agent-ferry`、Linux `$XDG_STATE_HOME`（缺省 `~/.local/state`）`/agent-ferry`；`cache` 为 Windows `%LOCALAPPDATA%\agent-ferry\cache`、macOS `~/Library/Caches/…`、Linux `$XDG_CACHE_HOME`（缺省 `~/.cache`）`/…`。
- `config.json` 固定在 `home`，因为引导脚本要在没有 Python 的情况下读出固定的解释器；它只有几 KB。占空间的记录都可以搬走。
- **宿主推断**（只在没有显式设置时使用）：
  - 隔离工作区：读取 `$CODEX_HOME/config.toml`（缺省 `~/.codex`）的 `[desktop].projectlessWorkspaceRoot`，沿用 Codex 的 `<根>/<YYYY-MM-DD>/<slug>`、重名加 `-N` 的命名。
  - 缓存：跟随 `CLAUDE_CODE_TMPDIR`。
  - 记录：取宿主数据根的兄弟目录，例如 `CLAUDE_CODE_TMPDIR=E:\Data\Claude\temp` 推出 `E:\Data\agent-ferry\runs`。锚点依次为 `CLAUDE_CODE_TMPDIR`（取上一级为 Claude 数据根）、Codex 的 `projectlessWorkspaceRoot`（同上）、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`；推出的目录若恰是用户主目录或盘符根，则放弃推断。
- **首次确认**：位置未确认前，`run` 不写任何文件，返回 `NEEDS_ONBOARDING`，其中给出「推荐位置（按宿主布局推断）」与「平台默认位置」及理由；宿主 Agent 展示给用户，用户选择后 `onboard --locations recommended|default` 把具体路径写进配置。`run --dry-run` 不受此限，因为它什么都不写。
- **每次 run 都告知**：输出与 `result.json` 都包含 run 目录、隔离工作区路径、被委托 CLI 自己的会话文件位置（Codex 由 thread id 在 `$CODEX_HOME/sessions` 下定位 rollout 文件；Claude 按 `projects/<编码后的 cwd>/<id>.jsonl` 推出；其他 runner 如实写「未接入」）。SKILL.md 要求最终回复把这些位置告诉用户。
- **不复制对话**：records 只存任务书、最终回复和精简的 `result.json`（审计摘要、错误末尾、指针）。原始事件流先流式写入 cache，结束后：
  - 成功：解析完即删除，只留指针；
  - 失败或超时：压缩（gzip）进 run 目录，便于排查；
  - 用户显式 `--keep-raw` 或 `config --keep-raw on`：也压缩保留。
  这样审计信息在 run 结束时已提取进 `result.json`，大部分情况不必保留原文；需要原文时仍可回到被委托 CLI 自己的会话文件。「失败时保留」是对「默认只留指针」的有意例外：启动失败、未登录、协议错误时被委托 CLI 往往没有可用的会话文件，原始 stdout/stderr 是唯一证据，且已压缩、通常只有几 KB。
- **查询与清理**：`locations` 列出各位置的来源、体积、文件数、最早/最新记录，以及旧版 `~/.oil-delegate` 和被委托 CLI 的会话目录（只读）。`prune` 默认只预览，列出路径、数量、体积和原因；只有 `--apply` 才删除。规则：超过保留天数（默认 30 天）或总量超过上限（默认 500 MB，从最旧的删）的 run 目录，以及超过 24 小时的孤立在途缓存。永远不删被委托 CLI 自己的会话文件、任何工作区（含隔离工作区）和 `config.json`；只删目录名符合 run id 格式、位于 records/cache 内、且不含链接的目录。
- **旧版导入**：`config --import-legacy [PATH]` 只读导入 `~/.oil-delegate/config.json` 里的启用列表、默认 runner、可执行文件路径和角色偏好，不碰旧目录。

**取舍**：推断规则写死在代码里，只覆盖常见布局；推断错了用户可以在首次确认时选平台默认，或之后 `config --set-location` 改。没有做「推荐值随宿主环境变量实时变化」：确认时写入具体路径，换来可预期。

### P0-C 安全的默认值

**问题**：上游默认 `write` 对应各 CLI 的最高权限（Codex 是 `--dangerously-bypass-approvals-and-sandbox`），会被宿主安全层拦截，也和用户自己的 Codex 默认值不一致。

**方案：三级权限，默认 `read`**

| 级别 | 含义 | 何时使用 |
| --- | --- | --- |
| `read`（默认） | 只读；不改文件 | 调研、分析、审查、方案评审 |
| `write` | 只能写工作区内的文件 | 按已确认方案实现 |
| `full` | 关闭沙箱与审批 | 只有用户明确要求时；输出带 `danger` 标记并在 stderr 醒目提示 |

各 runner 的映射（只在能保证该级别语义时才提供，否则明确拒绝）：

| runner | read | write | full |
| --- | --- | --- | --- |
| codex | `--sandbox read-only` | `--sandbox workspace-write` | `--dangerously-bypass-approvals-and-sandbox` |
| claude | `--permission-mode plan --permission-prompts none` | `--permission-mode acceptEdits --permission-prompts none` | `--dangerously-skip-permissions` |
| opencode | `--agent plan`，拒绝 edit/bash/外部目录 | 允许 edit，拒绝 bash 与外部目录 | 全部允许（question 始终拒绝） |
| dsh | `read-only` | `workspace-write` | `danger-full-access` |
| workbuddy / antigravity / grok / pi | 上游的只读参数 | 不提供（无法确认能限定在工作区） | 上游的最高权限参数 |
| kimi | 不提供（`-p` 自带 auto 权限） | 不提供 | 默认参数 |

- Codex 非交互运行再加 `-c approval_policy=never`（`exec` 没有审批人）；续接时 `codex exec resume` 没有 `--sandbox`，改用 `-c sandbox_mode=…`。值都不带引号，避免经过 `.cmd` 时被 cmd 解析。
- 角色：`review`/`critique`/`diagnose` 固定 `read`；`implement` 默认 `write`，用户明确要求时可 `--mode full`。
- **尊重用户的 Codex 配置**：不覆盖模型、provider、思考强度和 `web_search`；只有用户或任务明确要求时才传 `--model`、`--effort`、`--web-search`。沙箱与审批由权限级别决定，因为 `exec` 是非交互的，用户交互式的 `on-request` 在这里没有审批人。
- **联网检索**：本机 Codex 0.169.0 实测 `web_search` 只接受 `disabled`/`cached`/`indexed`/`live`；`codex exec` 没有 `--search`（那是交互式 TUI 的参数），所以按次开启实时检索用 `--web-search live`，映射为 `-c web_search=live`。官方文档的含义：`cached` 用 OpenAI 维护的索引缓存（默认，降低提示注入风险）；`indexed` 只在搜索索引放行时访问外部网页；`live` 抓取最新网页（等同交互式 `--search`）；`disabled` 关闭。只读沙箱不影响检索工具。
- **永不默认使用** `--trust`（会永久写入信任设置）和 `--ephemeral`（会让会话无法续接、也没有可指向的会话文件）。
- **隔离工作区**：省略 `--workspace` 时，在 `workspaces` 下新建空目录运行，避免把宿主项目的 `AGENTS.md`、私有材料带给第三方模型；项目任务必须显式 `--workspace`。续接时若省略，则沿用该会话首次 run 记录里的工作区。

### P1 审计、任务书与回报契约

- **审计摘要**（`result.json.audit`，`audit` 子命令可对历史 run、run 目录或任意事件文件重算）：
  - Codex JSONL 完整支持：`command_execution`（命令、退出码、状态含 `declined`）、`file_change`（路径、类型，标出工作区外路径）、`web_search`（查询、打开的网页 `open_page`、页内查找）、`mcp_tool_call`、`collab_tool_call`、`todo_list`、`error`、`turn.failed`、用量。字段依据 `codex-rs/exec/src/exec_events.rs` 与本机两次真实运行的结构（只看了键名，未复制内容）。
  - Claude：只有最终 JSON，能取到的是 `permission_denials`、轮数和费用，标记为 `partial`。
  - 其他 runner：`supported: false` 并写明原因。
- **任务书**：`references/task-brief.md` 给出精简模板（目标与边界、完成标准、入口线索、验证方式、回报格式）和三段可选片段：先核实前提、区分事实/报道/推断、引用来源。`--prompt-file PATH` 与 `--prompt -`（stdin）避免命令行引号问题。
- **回报契约**：`result.json` 把「被委托方自报」（`self_report`：回复文件与摘录）和「调用方已核实」（`caller_verification`：初始 `pending`）分开；`verify <run> --status …` 记录调用方的核实结论。SKILL.md 给出最小验收清单：对照原文、重跑验证、查越界改动、看审计摘要。
- **续接命令**：同时给出 POSIX、PowerShell（`& '…\codex.CMD' resume <id>`）、cmd 三种写法，`resume_command` 按平台选一种；可执行文件名在 PATH 上能直接找到时直接用短名。

### P1 续接、会话与异步

- 保留上游的 `--session` 续接、`sessions` 搜索和 `sessions export` 导出，测试随迁。
- 会话根目录允许是符号链接或 Windows 目录联接（用户常用它把数据搬到别的盘）：根目录解析一次，内部遍历不跟随链接。上游在 POSIX 上会拒绝符号链接根目录、拒绝挂载点下的所有路径，这里放宽并加回归测试。
- Claude 桌面会话元数据目录按平台给默认值（macOS 已由上游验证；Windows `%APPDATA%\Claude\claude-code-sessions`、Linux `~/.config/Claude/claude-code-sessions` 未验证），目录不存在时安静返回空列表。
- **不做 `--background`**：Claude Code 与 Codex 都能自己把命令放到后台；在技能里实现脱离进程要处理孤儿进程、Windows 脱离控制台、状态竞争等，收益小于复杂度。替代做法：run 一开始就写 `status: running` 的 `result.json` 并在 stderr 打印 run 目录，宿主后台运行时可以用 `show <run-id>` 查看进度。

### P2 来自 orchestration 的想法

- 采用：每次 run 有唯一 run id（相当于派发 ID）；`NEEDS_*`、`USE_HOST` 状态码作为显式决策门；解释器与可执行文件「解析一次、跑不起来就原样报错」；任务书写明「非零退出算不算失败」「交付证据」；超时后杀整棵进程树，不把沉默当作已退出。
- 不采用：`ask`/`reply` 邮箱、`worker_done`、任务 DAG、多 worker 监督。单次委托加续接已能覆盖「Claude Code 指挥 Codex 干活并回报」，再往上做就是编排器，且不依赖 Orca。

## 2. Windows 专项

| 隐患 | 处理 |
| --- | --- |
| stdout/stderr 不是 UTF-8 | 入口把 stdin/stdout/stderr 重设为 UTF-8 |
| prompt 经 argv 传给 `.cmd` 包装脚本 | npm 生成的 `.cmd` 解析出 `node <脚本>` 直接启动；其他 `.cmd` 只在 prompt 走 stdin 且参数不含 `"`、`%`、换行时允许；argv 过长时报错 |
| Claude 的 prompt | 改走 stdin（`claude -p` 在无位置参数时读 stdin） |
| 超时只杀直接子进程 | Windows 用 `taskkill /T /F`，POSIX 用进程组 |
| 续接命令引号 | 分别生成 POSIX、PowerShell、cmd 写法 |
| 假 CLI 夹具 | 测试用 `.py` 假 CLI，经 `sys.executable` 启动；可执行文件配置为 `.py` 时由当前解释器运行 |
| `HOME` 在 Windows 上不生效 | 测试同时隔离 `HOME`、`USERPROFILE`、`APPDATA`、`LOCALAPPDATA`、`XDG_*` |

## 3. 相对上游的改动清单

1. 改名为 agent-ferry；保留上游版权与许可全文。
2. 新增引导脚本（ps1/sh/cmd）、`doctor`、解释器固定与环境提示。
3. 位置分为 home/records/cache/workspaces，可配置、可推断、首次确认、每次告知；新增 `locations`、`prune`、`show`、旧版导入。
4. 默认权限由「最高权限」改为 `read`；新增 `full` 显式级别；无法保证语义的组合直接拒绝。
5. 省略 `--workspace` 时改用隔离空目录（上游是当前目录）。
6. 默认不再保存原始事件流；失败或显式要求时压缩保存。
7. 新增审计摘要、`audit` 与 `verify` 子命令、自报/核实分离。
8. 新增 `--prompt-file`、`--prompt -`、`--model`、`--effort`、`--web-search`、`--keep-raw`、`--slug`。
9. 续接命令按 shell 生成；`.cmd` 安全启动；超时杀进程树；UTF-8 输出。
10. 会话根目录允许链接/联接；Claude 桌面元数据按平台取默认目录。
11. 代码拆成单一职责的模块；测试改为跨平台假 CLI，覆盖 9 个 runner。
12. 结果字段调整：`output_path` 改为 `reply_path`；删除 `events_path`、`raw_stdout_path`、`stderr_path`（原始流默认不再保存，改为 `raw_events.paths`）；新增 `run_dir`、`workspace`、`cli_session_file`、`audit`、`self_report`、`caller_verification`、`resume_commands`、`danger`、`timed_out`、`stderr_tail`。`contract_version` 仍为 1（新项目从 1 开始，与上游的 1 不兼容）。
13. 删减（有意）：上游的 `write` 实为最高权限，现改名为 `full`；workbuddy、antigravity、grok、pi 不再提供工作区写入级别，kimi 只剩 `full`，因为无法保证对应语义。上游测试 `test_stdout_aliases_share_one_file…` 随原始流字段一起删除，`test_default_mode_is_full_access` 改为验证默认只读，「拒绝符号链接根目录」改为「接受链接根目录、拒绝云目录」。
14. 未改动：9 个 runner 的发现与解析、结构化 JSON、角色与 onboarding 流程、`sessions`/`sessions export` 的读取规则。

## 4. 借鉴与未借鉴

| 来源 | 借鉴 | 未借鉴及原因 |
| --- | --- | --- |
| session-helm（作者的另一个 MIT 项目） | UTF-8 输出、Windows+Linux 的 CI 矩阵、「推荐/提交/生效」分开记录（这里体现为记录请求的权限级别与实际传给 CLI 的参数，不声称已核实 CLI 内部生效）、任务卡与回报模板（精简为一页）、运行文件与技能目录分离、`agents/openai.yaml`、中英双 README | 账本、看板、多项目队列、模型路由：超出单次委托的范围 |
| orchestration（第三方，只借想法） | 见 P2 | 不复制文字，不依赖 Orca |

## 5. 已知限制

- 只在 Windows 11 本机用 Python 3.14 实测（3.12 复跑）；Linux 与 3.10 依赖 CI，macOS 未验证。
- 真实模型调用只对 Codex 做过烟测：只读联网检索（`--web-search live`）、工作区写入、续接会话各一次。Claude 运行器的命令行对照 `claude --help` 核对，真实调用时遇到独立 CLI 未登录，无法验证提示词走 stdin 的实际效果；其余 runner 只由 `--dry-run` 与假 CLI 核对。
- 审计摘要只对 Codex 完整；Claude 部分；其他 runner 不支持。Codex 的 `file_changes` 只来自 `file_change` 事件（补丁式编辑），模型用 shell 命令写的文件只出现在 `commands` 里，所以越界检查还要看 `git status` 或对比目录。
- workbuddy、antigravity、grok、pi 没有 `write` 级别；kimi 只有 `full`。
- Claude 桌面会话元数据在 Windows/Linux 的位置未验证。
- 引导脚本与 Python 侧探测是三份实现，靠测试约束一致性；注册表与 Scoop 等分支只在本机覆盖了一部分。
- 宿主推断只覆盖常见布局；推断不了就用平台默认。

## 6. 测试策略

- 全部离线：假 CLI 由 `tests/support.py` 生成（`.py` 文件，经 `sys.executable` 启动），按 runner 名模拟输出、记录 argv/stdin/cwd/环境；所有数据根目录、主目录变量都指向临时目录；PATH 只含假文件。
- 覆盖：上游仍有效的用例（onboarding、别名、角色、模式、dry-run、JSON 契约、失败与超时、并行、密钥不落盘、入口文件、空回复、会话搜索与导出）全部迁移；新增解释器探测（含引导脚本）、位置解析与推断、prune 安全、审计、续接命令引号、`.cmd` 解析、进程树终止、UTF-8 输出、目录联接/符号链接回归。
- CI：`ubuntu-latest`、`windows-latest` × Python 3.10、最新 3.x；运行 `python -B -m unittest discover -s tests -v`，并检查 sh 与 PowerShell 脚本语法。
