"""Regenerate the committed upgrade fence from its real branch profiles.

Run from the repository root:
    uv run python scripts/verify/regenerate_post_install_surface.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.shell.cli.installation.restart.post_install.guard_support import (  # noqa: E402
    SCENARIOS,
    SURFACE_DATA,
    copy_package,
    run_profile,
    surface_data,
)


def main() -> None:
    data = surface_data()
    functions: dict[str, str] = {}
    scenarios: dict[str, int] = {}
    primed: list[str] | None = None
    with tempfile.TemporaryDirectory(prefix="post-install-surface-") as temporary:
        for index, scenario in enumerate(SCENARIOS):
            result = run_profile(copy_package(Path(temporary) / str(index)), scenario)
            if result.returncode:
                raise RuntimeError(f"{scenario}: {result.stderr}")
            payload = json.loads(result.stdout)
            if payload["blockedImports"]:
                raise RuntimeError(f"{scenario}: blocked imports")
            if primed is None:
                primed = payload["primed"]
            elif primed != payload["primed"]:
                raise RuntimeError(f"{scenario}: inconsistent pre-install imports")
            scenarios[scenario] = len(payload["trace"])
            for function in payload["trace"]:
                functions.setdefault(function, scenario)
    modules = sorted({function.split(":", 1)[0] for function in functions})
    data.update(
        functions=sorted(functions),
        functionCount=len(functions),
        functionScenarios=dict(sorted(functions.items())),
        modules=modules,
        moduleCount=len(modules),
        primedModules=primed,
        primedModuleCount=len(primed or []),
        scenarios=scenarios,
    )
    SURFACE_DATA.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
