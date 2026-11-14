# agent-ferry

[简体中文](README.md) · [English](README.en.md)

[![Offline checks](https://github.com/GongXLiang/agent-ferry/actions/workflows/checks.yml/badge.svg)](https://github.com/GongXLiang/agent-ferry/actions/workflows/checks.yml)

Ferry tasks between agents: a host such as Claude Code or Codex hands **one well-bounded task** to a different CLI coding agent and brings back the result, an audit summary and a resume command, to be checked before it is relayed. Read-only and an isolated workspace by default, and it always tells you where its files are.

> This project is adapted from [oil-oil/oil-delegate](https://github.com/oil-oil/oil-delegate) (MIT). It keeps the upstream adapters for nine runners, structured output, session resume, roles and session export. Changes are summarized below and in [docs/DESIGN.md](docs/DESIGN.md) (Chinese); attribution is in [NOTICE.md](NOTICE.md).

## What it does

- **One-sentence delegation**: "Have Codex research this, read-only, and report back", "Use cc to review the login flow of this repo".
- **Finds Python on its own**: Python-free bootstrap scripts check PATH, conda, uv, pyenv, Scoop, common folders and the Windows registry, and reject the Microsoft Store placeholder. `doctor` lists every candidate and why it was rejected.
- **Predictable file locations**: config, run records, temporary cache and isolated workspaces are separate and configurable, and follow the host's layout (e.g. `CLAUDE_CODE_TMPDIR`, Codex's `projectlessWorkspaceRoot`). On first use it shows where files will go and asks; every run reports the locations.
- **No duplicate transcripts**: the delegated CLI already keeps the full conversation, so only the task, the final reply and a compact result with a pointer to that session file are stored.
- **Safe defaults**: read-only by default; "workspace-write" only when changes are needed; full access only on explicit request, clearly flagged in the output.
- **Auditable, and verified by the caller**: commands run, files changed, searches and pages opened, failed and declined actions; results separate "self-reported by the delegate" from "verified by the caller".
- **Resume and clean up**: resume commands ready to paste into PowerShell, cmd or bash; `prune` only lists what could be removed until you confirm.

## Main changes from upstream

| Area | oil-delegate (upstream) | agent-ferry |
| --- | --- | --- |
| Finding Python | docs say `py -3`/`python3`; no detection | bootstrap scripts + `doctor`; conda/uv/pyenv/Scoop/registry; pin or override |
| File locations | everything in `~/.oil-delegate`, not reported | config / records / cache / isolated workspaces, host-aware, confirmed on first use, reported every run, `locations`/`prune` |
| Raw event streams | always stored in full | pointer only by default; compressed on failure or on request |
| Default permissions | each CLI's highest level | read-only; `--mode write` for workspace edits; `--mode full` only on request, flagged |
| Working directory | current directory | isolated empty folder unless a project is given |
| Verification | read the reply and raw events | built-in audit summary, `audit`/`verify`, self-reported vs verified |
| Prompts and resume | `--prompt` only; POSIX-quoted resume command | `--prompt-file`/stdin; PowerShell, cmd and POSIX resume commands |
| Windows | not verified on real machines | safe `.cmd` launching, process-tree kill on timeout, UTF-8 output, CI on Windows and Linux |

## Supported agents and permission levels

| Agent | Names and aliases | read (default) | write (workspace only) | full (explicit) |
| --- | --- | --- | --- | --- |
| Codex | `codex` | ✓ read-only sandbox | ✓ `workspace-write` | ✓ |
| Claude Code | `claude`, `cc` | ✓ plan | ✓ acceptEdits | ✓ |
| OpenCode | `opencode` | ✓ | ✓ (no bash, no outside folders) | ✓ |
| DeepSeek Harness | `dsh`, `deepseek` | ✓ | ✓ | ✓ |
| WorkBuddy | `workbuddy`, `wb` | ✓ | — | ✓ |
| Antigravity | `antigravity`, `agy` | ✓ | — | ✓ |
| Grok Build | `grok` | ✓ | — | ✓ |
| Pi Agent | `pi` | ✓ | — | ✓ |
| Kimi Code | `kimi` | — | — | ✓ (`-p` always auto-approves) |

"—" means the level cannot be guaranteed for that runner, so it is refused instead of silently using more access.

## Install

Put the repository where your host discovers skills, keeping the folder name `agent-ferry`:

```bash
# Claude Code
git clone https://github.com/GongXLiang/agent-ferry.git ~/.claude/skills/agent-ferry
# Codex
git clone https://github.com/GongXLiang/agent-ferry.git ~/.codex/skills/agent-ferry
```

You need at least one supported CLI that is installed and logged in, and any Python 3.10+ (the bootstrap finds it).

## First use

Just describe the task. The first run writes nothing; it first tells you:

1. **Where files will go**: a recommendation inferred from the host layout (for example, a sibling folder on the drive where your host data lives) and the platform default (`%LOCALAPPDATA%` on Windows, `~/Library/Application Support` on macOS, `~/.local/state` on Linux).
2. **Which agents are installed**: installed does not mean logged in.

After you choose, the agent runs `onboard` once and continues with your task.

## Everyday requests

```text
Have codex research the background of today's story, read-only. Check the premises first, separate facts, reports and inference, and cite sources.
Use cc to review this repo's login flow, read-only, with file and line references.
Have codex implement the plan we just agreed on, workspace only, and run the tests before reporting.
Continue that codex session and ask about the risks it missed.
Where are this skill's files and how much space do they use? List what can be cleaned up.
```

## Where files live

| Location | Contents | Default (configurable) |
| --- | --- | --- |
| home | `config.json` (a few KB) | `agent-ferry` in the platform data folder |
| records | per run: the task, the final reply, `result.json` with the audit summary | chosen on first use |
| cache | raw output while a run is in flight; deleted when it ends | follows `CLAUDE_CODE_TMPDIR`, else the platform cache folder |
| workspaces | isolated empty folders for non-project tasks | follows Codex's `projectlessWorkspaceRoot`, named `<date>/<name>` |

Failed runs keep their raw output compressed in the run folder for diagnosis; successful runs only keep a pointer to the delegated CLI's own session file (use `--keep-raw` to keep more). `prune` never deletes workspaces or the delegated CLI's own sessions.

## Verification

Each run reports which commands ran (including failed and declined ones), which files changed (flagging changes outside the workspace), what was searched and which pages were opened. The skill tells the host agent to verify before relaying: compare with the sources, re-run the checks, look for out-of-scope edits, read the audit, and keep "self-reported" apart from "verified" in the final answer.

## What has been verified

- Windows 11: all offline tests pass with Python 3.14 (re-run with 3.12); `doctor`, `locations` and `run --dry-run` were checked read-only against the CLIs actually installed; all four entry points (PowerShell 7, Windows PowerShell 5.1, cmd, and sh under Git Bash) selected the right interpreter.
- Real Codex calls (Windows, isolated workspace): read-only live web search (`--web-search live`), workspace-write and session resume were each run once; run records and isolated workspaces landed in the configured places, and `prune` refused to delete a run folder containing a directory junction (checked with a real junction).
- GitHub Actions: Ubuntu and Windows with Python 3.10 and the latest 3.x (see the repository's Actions page for results).
- Not verified: macOS hardware; real model calls for runners other than Codex (tests use fake CLIs; the Claude runner's command line was checked against `claude --help`, but a trial run found the standalone CLI not logged in on this machine); the location of Claude desktop session metadata on Windows/Linux.

## Development

```bash
python -B -m unittest discover -s tests -v
```

Tests are offline, never call a model, and never touch real CLI data folders. More: [SKILL.md](SKILL.md), [references/](references/), [docs/DESIGN.md](docs/DESIGN.md).

## License

[MIT License](LICENSE). The upstream copyright notice of oil-oil is retained; changes in this project are Copyright (c) 2026 GongXLiang.
