"""Environment boundary for Git subprocesses with explicit path authority."""

from __future__ import annotations

import os
from collections.abc import Mapping


def git_subprocess_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment without caller-controlled ``GIT_*`` settings."""

    source = os.environ if environment is None else environment
    return {
        name: value
        for name, value in source.items()
        if not name.startswith("GIT_")
    }
