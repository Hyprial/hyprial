"""Load the stand-alone scripts under ``packaging/clients`` by path."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[4]
PACKAGING = ROOT / "packaging" / "clients"
LOCK = PACKAGING / "runtime-lock.json"


def load(name: str) -> ModuleType:
    """Import ``packaging/clients/<name>.py`` once, under a private module name."""

    module_name = f"_packaging_clients_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, PACKAGING / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
