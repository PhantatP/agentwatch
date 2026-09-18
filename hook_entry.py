"""Absolute-path entrypoint for the hooks.

Hooks run with the project as the working directory, so `python -m agentwatch.hook`
would not resolve. This file pins the package location instead.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agentwatch.hook import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # never break the caller's turn
