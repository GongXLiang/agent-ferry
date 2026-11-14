# agent-ferry

[简体中文](README.md) · [English](README.en.md)

[![Offline checks](https://github.com/GongXLiang/agent-ferry/actions/workflows/checks.yml/badge.svg)](https://github.com/GongXLiang/agent-ferry/actions/workflows/checks.yml)

在不同 Agent 之间摆渡任务：让 Claude Code、Codex 等宿主把**一项边界清楚的任务**派给另一个 CLI Agent，带回结果、审计摘要和续接命令，由你核实后再转述。默认只读、默认隔离工作区，文件放在哪里会明确告诉你。

> 本项目基于 [oil-oil/oil-delegate](https://github.com/oil-oil/oil-delegate)（MIT）改造，保留了上游的 9 个 runner 适配、结构化输出、续接、角色和会话导出等能力。相对上游的改动见下文与 [docs/DESIGN.md](docs/DESIGN.md)，来源说明见 [NOTICE.md](NOTICE.md)。

## 能做什么

- **一句话委托**：「让 Codex 调研这件事，只读，回来汇报」「用 cc 审查这个仓库的登录流程」。
- **自动找 Python**：引导脚本不依赖 Python，按 PATH、conda、uv、pyenv、Scoop、常见目录和注册表依次探测，拒绝 Microsoft Store 占位符；`doctor` 列出所有候选与被拒原因。
- **文件位置可预期**：配置、运行记录、临时缓存、隔离工作区分开存放，可配置，会跟随宿主布局（例如 `CLAUDE_CODE_TMPDIR`、Codex 的 `projectlessWorkspaceRoot`）。第一次使用先告诉你放哪、问你同不同意；每次运行都报告位置。
- **不重复存对话**：被委托 CLI 自己已经保存了完整会话，这里只存任务书、最终回复和精简结果，并指向那份会话文件。
- **安全默认值**：默认只读；需要改文件才用「只写工作区」；完全放开必须显式要求，输出会醒目标记。
- **可审计、要核实**：自动整理被委托方执行的命令、改动的文件、搜索与打开的网页、失败与被拒的操作；结果区分「被委托方自报」和「调用方已核实」。
- **可续接、可清理**：生成可直接粘贴到 PowerShell、cmd 或 bash 的续接命令；`prune` 默认只列出可清理的记录，确认后才删除。

## 相对上游的主要改动

| 方面 | oil-delegate（上游） | agent-ferry |
| --- | --- | --- |
| 找 Python | 文档写 `py -3`/`python3`，脚本不探测 | 引导脚本 + `doctor`，识别 conda/uv/pyenv/Scoop/注册表，可固定、可覆盖 |
| 文件位置 | 全部写进 `~/.oil-delegate`，不告知 | 分为配置/记录/缓存/隔离工作区，跟随宿主布局，首次确认、每次告知，`locations`/`prune` |
| 原始事件流 | 每次都完整保存 | 默认只留指针；失败或显式要求时压缩保存 |
| 默认权限 | 各 CLI 的最高权限 | 只读；工作区写入需 `--mode write`；完全放开需 `--mode full` 并醒目标记 |
| 工作目录 | 默认当前目录 | 默认隔离空目录，项目任务显式传入 |
| 验收 | 读回复与原始事件 | 内置审计摘要、`audit`/`verify`，区分自报与已核实 |
| 提示词与续接 | 只能 `--prompt`；续接命令按 POSIX 引用 | `--prompt-file`/stdin；PowerShell、cmd、POSIX 三种续接写法 |
| Windows | 未实机验证 | `.cmd` 安全启动、超时杀进程树、UTF-8 输出，CI 覆盖 Windows 与 Linux |

## 支持的 Agent 与权限级别

| Agent | 名称与别名 | read（默认） | write（只写工作区） | full（显式开启） |
| --- | --- | --- | --- | --- |
| Codex | `codex` | ✓ 只读沙箱 | ✓ `workspace-write` | ✓ |
| Claude Code | `claude`、`cc` | ✓ plan | ✓ acceptEdits | ✓ |
| OpenCode | `opencode` | ✓ | ✓（禁 bash 与外部目录） | ✓ |
| DeepSeek Harness | `dsh`、`deepseek` | ✓ | ✓ | ✓ |
| WorkBuddy | `workbuddy`、`wb` | ✓ | — | ✓ |
| Antigravity | `antigravity`、`agy` | ✓ | — | ✓ |
| Grok Build | `grok` | ✓ | — | ✓ |
| Pi Agent | `pi` | ✓ | — | ✓ |
| Kimi Code | `kimi` | — | — | ✓（`-p` 自带自动权限） |

「—」表示无法保证该级别的语义，所以直接拒绝，不偷偷换成更高权限。

## 安装

把仓库放进宿主能发现技能的目录，文件夹名保持 `agent-ferry`：

```bash
# Claude Code
git clone https://github.com/GongXLiang/agent-ferry.git ~/.claude/skills/agent-ferry
# Codex
git clone https://github.com/GongXLiang/agent-ferry.git ~/.codex/skills/agent-ferry
```

需要至少一个已安装并登录的受支持 CLI，以及任意 Python 3.10+（引导脚本会自己找）。

## 第一次使用

直接对 Agent 说任务即可。第一次运行时它不会写任何文件，而是先告诉你：

1. **文件会放在哪**：推荐位置（按宿主布局推断，例如数据在 D 盘时推荐 D 盘的同级目录）和平台默认位置（Windows `%LOCALAPPDATA%`、macOS `~/Library/Application Support`、Linux `~/.local/state`）。
2. **本机装了哪些 Agent**：已安装不等于已登录。

你选定后，它会运行一次 `onboard` 保存配置，然后继续原来的任务。

## 平时这样说

```text
让 codex 调研今天这条新闻的来龙去脉，只读，先核实前提，标明哪些是事实、哪些是报道或推断，附来源。
用 cc 审查这个仓库的登录流程，只读，告诉我问题在哪个文件哪一行。
让 codex 按刚才确认的方案实现，只改工作区，跑完测试再汇报。
继续刚才那个 codex 会话，追问它遗漏的风险。
看看这个技能的文件都放在哪、占了多少空间；列出可以清理的记录。
```

## 文件放在哪里

| 位置 | 内容 | 默认（可配置） |
| --- | --- | --- |
| home | `config.json`（几 KB） | 平台数据目录下的 `agent-ferry` |
| records | 每次运行的任务书、最终回复、`result.json`（含审计摘要） | 首次确认时选定 |
| cache | 运行中的原始输出，运行结束即删除 | 跟随 `CLAUDE_CODE_TMPDIR`，否则平台缓存目录 |
| workspaces | 非项目任务的隔离空目录 | 跟随 Codex 的 `projectlessWorkspaceRoot`，按 `<日期>/<名称>` 命名 |

失败的运行会把原始输出压缩保存在 run 目录，便于排查；成功的运行只保留指向被委托 CLI 会话文件的指针（需要时可 `--keep-raw`）。`prune` 永远不会删除工作区或被委托 CLI 自己的会话。

## 验收

每次运行都会给出审计摘要：执行了哪些命令（含失败、被拒）、改了哪些文件（标出工作区外的改动）、搜索了什么、打开了哪些网页。技能要求宿主 Agent 按清单核实后再转述：对照原文、重跑验证、检查越界改动、看审计摘要，并在最终回复里区分「被委托方自报」与「已核实」。

## 已验证范围

- Windows 11 本机：Python 3.14（另用 3.12 复跑），全部离线测试通过；`doctor`、`locations`、`run --dry-run` 对本机真实安装的 CLI 做过只读核对；四种启动方式（PowerShell 7、Windows PowerShell 5.1、cmd、Git Bash 里的 sh）都选对了解释器。
- 真实 Codex 调用（Windows，隔离工作区）：只读联网检索（`--web-search live`）、工作区写入、续接会话各跑过一次；运行记录与隔离工作区都落在配置的位置；`prune` 对含目录联接的记录目录拒绝删除（用真实联接验证过）。
- GitHub Actions：Ubuntu 与 Windows，Python 3.10 与最新 3.x（结果见仓库的 Actions 页面）。
- 未验证：macOS 实机；Codex 以外运行器的真实模型调用（测试使用假 CLI；Claude 运行器的命令行对照过 `claude --help`，但试跑时本机的独立 CLI 未登录）。Claude 桌面会话元数据在 Windows/Linux 上的位置未验证。

## 维护与测试

```bash
python -B -m unittest discover -s tests -v
```

测试不联网、不调用模型，也不读写本机真实的 CLI 数据目录。更多细节：[SKILL.md](SKILL.md)、[references/](references/)、[docs/DESIGN.md](docs/DESIGN.md)。

## 许可

[MIT License](LICENSE)。保留上游 oil-oil 的版权声明；本项目改动 Copyright (c) 2026 GongXLiang。
