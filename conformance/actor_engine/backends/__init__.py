# Decision record (file header; no inline comments by repository rule):
# - This loader is the suite's adapter seam (spec 3.4, 15): it refuses any
#   ENGINE_ABI_VERSION other than 1 (LIFE-04) and fails closed when the native
#   module is absent (raise, never skip).
# - Native handler injection follows CL-2: the native module registers the
#   conformance factories through the Rust-side register_native_factory when it
#   is built with the test-only `conformance-handlers` feature; the Python
#   backends register the same trampoline through their own twin channel.
# - conformance.env_panic_factory builds a trampoline, but panics at build()
#   while the environment variable HYPRIAL_CONFORMANCE_BUILD_PANIC is "1". It
#   lets one case observe a native FACTORY panic at first start (SUP-10) and at
#   restart (SUP-06, E-9) without process-global build counters.
# - The Python trampoline's "panic" raises the backend's PanicException twin.
#   The native module exports no such type, so a suite-local
#   PanicException(BaseException) stands in whenever no twin was installed;
#   py-handler cases on the native backend then still raise a BaseException
#   named PanicException, never an IndexError.
# - P8 removed the pykka adapter along with the shipped engine: the remaining
#   adapters are the packaged reference module and the native extension, so no
#   backend loads any hyprial.kernel module (import-closure case).
"""Backend selection for the actor engine conformance suite."""

from __future__ import annotations

import importlib
import os
from types import ModuleType

BACKENDS = ("reference-abi", "native")
NATIVE_MODULE = "hyprial._core.actors"
TRAMPOLINE = "conformance.trampoline"
ENV_PANIC_FACTORY = "conformance.env_panic_factory"
BUILD_PANIC_ENV = "HYPRIAL_CONFORMANCE_BUILD_PANIC"
REQUIRED_EXPORTS = (
    "ENGINE_ABI_VERSION",
    "Engine",
    "EffectCompleted",
    "NativeHandlerFactory",
    "ActorAdmissionError",
    "ActorFailedError",
    "ActorSpawnError",
)
_MODULES = {"reference-abi": "backends.reference_abi"}


class BackendLoadError(RuntimeError):
    """The selected backend cannot be used; the run must fail, not skip."""


def check_abi(module: ModuleType) -> ModuleType:
    """Refuse a module that does not expose exactly ABI v1 (LIFE-04)."""
    missing = [name for name in REQUIRED_EXPORTS if not hasattr(module, name)]
    if missing:
        raise BackendLoadError(f"{module.__name__} lacks ABI exports: {', '.join(missing)}")
    version = getattr(module, "ENGINE_ABI_VERSION")
    if type(version) is not int or version != 1:
        raise BackendLoadError(f"{module.__name__} reports ENGINE_ABI_VERSION={version!r}; adapter requires 1")
    return module


def load(name: str) -> ModuleType:
    if name not in BACKENDS:
        raise BackendLoadError(f"unknown backend {name!r}; choose one of {', '.join(BACKENDS)}")
    target = NATIVE_MODULE if name == "native" else _MODULES[name]
    try:
        module = importlib.import_module(target)
    except ImportError as exc:
        raise BackendLoadError(f"backend {name!r}: cannot import {target} ({exc}); fail closed") from exc
    check_abi(module)
    if name != "native":
        install_python_trampoline(module)
    return module


def trampoline(command: object) -> object:
    """Shared handler body: ``"panic"`` panics, callables run, data echoes."""
    if isinstance(command, str) and command == "panic":
        raise _panic_type()("conformance panic")
    if callable(command):
        return command()
    return command


class PanicException(BaseException):
    """Suite-local panic stand-in used when the backend installs no twin."""


_PANIC: list[type] = []


def _panic_type() -> type:
    return _PANIC[0] if _PANIC else PanicException


def env_panic_build() -> object:
    """Python twin of conformance.env_panic_factory's build()."""
    if os.environ.get(BUILD_PANIC_ENV) == "1":
        raise _panic_type()("conformance factory panic")
    return trampoline


def install_python_trampoline(module: ModuleType) -> None:
    if not _PANIC:
        _PANIC.append(module.PanicException)
    for name, build in ((TRAMPOLINE, lambda: trampoline), (ENV_PANIC_FACTORY, env_panic_build)):
        try:
            module.register_native_factory(name, build)
        except RuntimeError:
            pass
