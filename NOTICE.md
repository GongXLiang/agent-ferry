# NOTICE

## 来源

agent-ferry 基于 [oil-oil/oil-delegate](https://github.com/oil-oil/oil-delegate)（MIT License，Copyright (c) 2026 oil-oil）改造而来，不是完全原创。上游的版权声明与许可全文保留在 [LICENSE](LICENSE) 中；本项目的改动以同一 MIT 许可发布（Copyright (c) 2026 GongXLiang）。

保留自上游、仅做适配的部分：9 个 runner 的发现、命令行与输出解析，角色与 onboarding 流程，`sessions` 会话搜索与 `sessions export` 导出（含 Antigravity protobuf 解码与 Claude JSONL 过滤规则），以及对应测试的大部分用例。

## 相对上游的主要改动

- 改名为 agent-ferry，代码拆成单一职责的模块。
- 新增不依赖 Python 的引导脚本（PowerShell / sh / cmd）与 `doctor`，自动识别 Python 3.10+、conda、uv、pyenv、Scoop。
- 存储位置分为配置、运行记录、临时缓存、隔离工作区，可配置、按宿主布局推断、首次确认、每次告知；新增 `locations`、`prune`（默认只预览）、`show`、旧版配置只读导入。
- 默认权限由「各 CLI 的最高权限」改为只读；新增工作区写入级别与需显式开启的 `full` 级别。
- 省略 `--workspace` 时使用隔离空目录；默认不再保存原始事件流。
- 新增审计摘要、`audit`、`verify`、`--prompt-file`、按 shell 生成的续接命令、Windows `.cmd` 安全启动、超时终止进程树、UTF-8 输出。

完整清单与取舍见 [docs/DESIGN.md](docs/DESIGN.md)。

## 借鉴

- [session-helm](https://github.com/GongXLiang/codex-session-helm)（GongXLiang，MIT）：借鉴了任务卡与回报验收模板的结构、UTF-8 输出处理、跨平台 CI 与发布文件的组织方式。
- Orca 自带的 orchestration 技能（第三方）：只借鉴了思路（解析一次可执行文件、跑不起来就报错停止、显式决策门、任务说明自包含），没有复制其文字。

## 素材与商标

本仓库不包含图片。上游的配图（`hero.svg`、插画）与第三方厂商图标均未沿用。

Claude、Claude Code、Codex、OpenAI、Kimi、DeepSeek、Grok、OpenCode、WorkBuddy、Antigravity、Pi 等名称属于各自的所有者。本项目与这些产品或公司没有隶属或背书关系，只调用用户自己安装并登录的 CLI。
