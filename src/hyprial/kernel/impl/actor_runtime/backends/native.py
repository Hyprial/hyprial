# Decision record (file header; no inline comments by repository rule):
# - This is the only product module allowed to import the compiled
#   hyprial._core.actors extension; scripts/ci/check_module_boundaries.py pins
#   that allowance by source path. Everything else reaches the extension through
#   HYPRIAL_ACTOR_BACKEND=native and create_engine.
# - Loading is fail-closed. A missing extension, a missing export, or an ABI
#   version other than the integer 1 raises ActorBackendUnavailableError. No
#   path falls back to another backend, because a host that asked for the
#   native engine must not silently run a different concurrency model.
# - The extension is imported lazily, when the native backend is selected.
#   Since S made native the shipped default, the wheel must be present; a host
#   without it fails closed.
# - The adapter itself is AbiEngine; the native module exposes the same ABI v1
#   surface as the packaged reference module, so both share one translation
#   layer.
"""Fail-closed loader for the compiled hyprial._core.actors ABI v1 module."""

from __future__ import annotations

import importlib
from types import ModuleType

from hyprial.kernel.impl.actor_runtime.backends.abi import AbiEngine
from hyprial.kernel.impl.actor_runtime.port.config import (
    ActorBackendUnavailableError,
)

NATIVE_MODULE = "hyprial._core.actors"
ENGINE_ABI_VERSION = 1

REQUIRED_EXPORTS = (
    "ENGINE_ABI_VERSION",
    "Engine",
    "ActorAdmissionError",
    "ActorFailedError",
    "ActorSpawnError",
    "EffectCompleted",
)

NativeEngine = AbiEngine


def _unavailable(detail: str, cause: BaseException | None = None) -> ActorBackendUnavailableError:
    message = (
        f"actor backend 'native' cannot use {NATIVE_MODULE}: {detail}; "
        "fail closed without falling back"
    )
    error = ActorBackendUnavailableError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def load_native() -> ModuleType:
    """Import and validate the native ABI module, or fail closed."""

    try:
        module = importlib.import_module("hyprial._core.actors")
    except (ImportError, ModuleNotFoundError) as exc:
        raise _unavailable(
            "the compiled extension is not importable (source checkout: run "
            "`HYPRIAL_BUILD_NATIVE=1 uv sync --reinstall-package hyprial`)",
            exc,
        )
    missing = [name for name in REQUIRED_EXPORTS if not hasattr(module, name)]
    if missing:
        joined = ", ".join(missing)
        raise _unavailable(f"required ABI v1 exports are missing ({joined})")
    version = getattr(module, "ENGINE_ABI_VERSION")
    if isinstance(version, bool) or not isinstance(version, int) or version != ENGINE_ABI_VERSION:
        raise _unavailable(
            f"ENGINE_ABI_VERSION={version} does not match {ENGINE_ABI_VERSION}"
        )
    return module


def create_native_engine(config=None) -> AbiEngine:
    """Construct the native-backed ActorEngine port, or fail closed."""

    return NativeEngine(load_native(), config)
