#!/usr/bin/env python3
"""Host-neutral dev-upgrade smoke instrument (package entrypoint)."""

from __future__ import annotations

from _dev_upgrade_smoke.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
