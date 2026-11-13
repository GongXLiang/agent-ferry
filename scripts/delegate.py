#!/usr/bin/env python3
"""agent-delegate-verify: delegate one bounded task to another CLI agent, then audit and verify it.

Start it through scripts/delegate.ps1, scripts/delegate.sh or scripts/delegate.cmd to find a
Python 3.10+ automatically, or run it with any Python 3.10+ you already know about.
Standard library only. Based on oil-oil/oil-delegate (MIT).
"""
import sys

if sys.version_info < (3, 10):
    sys.stderr.write(
        "[ERROR] agent-delegate-verify needs Python 3.10 or newer. "
        "Run scripts/delegate.ps1 (Windows) or scripts/delegate.sh to locate one.\n"
    )
    sys.exit(1)

from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adv.cli import build_parser, compose_prompt, main  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
