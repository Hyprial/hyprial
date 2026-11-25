#!/usr/bin/env python3
"""Exec Codex with HYPRIAL's DeepSeek config without putting a key in argv."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deepseek import MODEL, PROVIDER, deepseek_environment
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.model_provider import codex_provider_configuration


def main() -> None:
    configured = os.environ.get("HYPRIAL_CODEX_BIN")
    binary = Path(configured) if configured else Path(shutil.which("codex") or "")
    if not binary.is_file():
        raise SystemExit("codex executable is unavailable")
    environment = {**os.environ, **deepseek_environment()}
    spec = HarnessLaunchSpec(
        "codex",
        "e2e-codex",
        True,
        model_provider=PROVIDER,
        model=MODEL,
    )
    config_args, provider_environment = codex_provider_configuration(spec, environment)
    environment.update(provider_environment)
    os.execve(
        str(binary.resolve()),
        [str(binary.resolve()), *config_args, *sys.argv[1:]],
        environment,
    )


if __name__ == "__main__":
    main()
