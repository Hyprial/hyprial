#!/usr/bin/env python3
"""E2E-031: group conversations on a single isolated node (CI gate).

Runs the scenario on one disposable daemon; see 31-group-conversation.md and
._group_conversation.steps for the assertions.  Stands on its own when loaded
by path (spec_from_file_location in the CI baseline tests), so the sibling
packages are added to sys.path explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _group_conversation.phases import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
