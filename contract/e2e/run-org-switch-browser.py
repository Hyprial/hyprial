#!/usr/bin/env python3
"""E2E-032: switch the viewed organization on the org page of ``hyprial gui``.

Starts an isolated daemon on a GUI-ready throwaway home, ``hyprial gui`` and a
headless browser; see 32-org-switch-browser.md and ._org_switch.phases for the
assertions. Stands on its own when loaded by path, so the scenario directory
is added to sys.path explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _org_switch.phases import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
