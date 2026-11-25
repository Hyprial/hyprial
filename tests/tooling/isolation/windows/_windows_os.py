"""A Windows-shaped ``os`` proxy for simulating Windows on a POSIX host."""

from __future__ import annotations

import os
import types


class WindowsOs(types.ModuleType):
    """Report ``nt`` and hide the POSIX-only uid calls; delegate the rest."""

    def __init__(self) -> None:
        super().__init__("os")
        self.name = "nt"

    def __getattr__(self, attr: str):
        if attr in {"getuid", "geteuid", "getgid", "getegid"}:
            raise AttributeError(attr)
        return getattr(os, attr)
