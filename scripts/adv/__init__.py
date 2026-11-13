"""agent-delegate-verify internals (standard library only, Python 3.10+).

Based on oil-oil/oil-delegate (MIT). Modules:

- common: constants, errors, JSON/stdio helpers
- toml_compat: read config.toml on Python 3.10 without tomllib
- config: the skill's own config.json
- storage: home/records/cache/workspaces resolution, run records, prune
- pyfind: Python interpreter discovery (doctor, --set-python)
- runners: runner registry, executable resolution, command lines, resume commands
- launch: process execution with streaming capture and process-tree termination
- parsing: runner output parsing
- audit: audit summaries of delegated runs
- sessions: session search and export (from upstream)
- cli: argument parsing and subcommands
"""

__version__ = "1.0.0"
