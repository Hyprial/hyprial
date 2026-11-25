# Decision record (file header; no inline comments by repository rule):
# - This module is the packaged copy of the conformance oracle in
#   conformance/actor_engine/backends/reference_abi.py, which remains the
#   executable spec of docs/design/actor-engine-native-abi-spec-2026-11-01.md
#   (ENGINE_ABI_VERSION 1). P6 selects it through HYPRIAL_ACTOR_BACKEND, so the
#   product carries the same pure-Python ABI implementation instead of
#   importing the test tree at runtime. The two copies must change together;
#   the conformance suite is the oracle and the production selector tests pin
#   this copy's port behavior.
# - It imports only the standard library and its two sibling reference modules,
#   so it can serve as the backend behind the adapter without loading any
#   business domain. One engine-wide RLock guards every record (D12: no
#   reliance on the GIL); every Python callback (factory, handler, sinks) runs
#   with that lock released (GIL-01). Each serial actor owns one daemon thread
#   ("thread per actor", spec 12, LIV-01); pools own pool_size worker threads
#   and one courier thread.
# - register_native_factory is the reference twin of the Rust-side test-only
#   injection channel (CL-2). The suite registers its trampoline here; it is
#   not public ABI. PanicException emulates the PyO3 panic payload type
#   (a BaseException subclass), so the native-handler cases run on every backend.
"""Pure-Python reference implementation of the native actor engine ABI v1."""

from __future__ import annotations

import threading
from typing import Callable

from ._reference_engine import Engine
from ._reference_pools import EffectCompleted

ENGINE_ABI_VERSION = 1


class ActorAdmissionError(Exception):
    """ask admission refused; args[0] is ``"overloaded"`` or ``"closed"``."""


class ActorFailedError(Exception):
    """The ask target suffered a supervised child failure."""


class ActorSpawnError(Exception):
    """The handler factory raised at first start; ``__cause__`` is that error."""


class PanicException(BaseException):
    """Reference stand-in for a Rust panic caught at the FFI boundary."""


class NativeHandlerFactory:
    """Handle around a registered native factory; only lookups mint these."""

    __slots__ = ("_build", "name")

    def __init__(self, name: str, build: Callable[[], Callable[[object], object]]) -> None:
        self.name = name
        self._build = build

    def __call__(self) -> Callable[[object], object]:
        return self._build()

    def __repr__(self) -> str:
        return f"NativeHandlerFactory({self.name!r})"


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, NativeHandlerFactory] = {}


def register_native_factory(name: str, build: Callable[[], Callable[[object], object]]) -> None:
    """Test-only injection channel (CL-2); duplicate names are refused."""
    with _REGISTRY_LOCK:
        if name in _REGISTRY:
            raise RuntimeError(f"duplicate native handler factory name: {name!r}")
        _REGISTRY[name] = NativeHandlerFactory(name, build)


def lookup_native_factory(name: str) -> NativeHandlerFactory:
    with _REGISTRY_LOCK:
        factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(f"unknown native handler factory {name!r}")
    return factory


Engine._errors = (ActorAdmissionError, ActorFailedError, ActorSpawnError)
Engine._native_type = NativeHandlerFactory
Engine._lookup_native = staticmethod(lookup_native_factory)

__all__ = [
    "ENGINE_ABI_VERSION",
    "ActorAdmissionError",
    "ActorFailedError",
    "ActorSpawnError",
    "EffectCompleted",
    "Engine",
    "NativeHandlerFactory",
    "PanicException",
    "register_native_factory",
]
