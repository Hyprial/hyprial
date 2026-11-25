"""Fail-closed values for the WhatsApp linked-device worker boundary."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import FrozenJsonV1, freeze_json_v1


class WhatsAppProviderError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class NativeWorker(Protocol):
    def invoke(self, payload: object, *, timeout: float = 15.0) -> object: ...


@dataclass(frozen=True, slots=True)
class ArtifactStatus:
    available: bool
    release_ready: bool
    build: str
    reason_codes: tuple[str, ...]

    @classmethod
    def unavailable(cls, *reason_codes: str) -> ArtifactStatus:
        if not reason_codes or any(not code for code in reason_codes):
            raise ValueError("unavailable artifact requires reason codes")
        return cls(False, False, "unavailable", tuple(reason_codes))

    @classmethod
    def available_candidate(cls, build: str) -> ArtifactStatus:
        if not build:
            raise ValueError("candidate build must not be empty")
        return cls(True, False, build, ("WHATSAPP_RELEASE_ASSEMBLY_OPEN",))


class WhatsAppEffectFence:
    """One instance/generation/account fence shared by daemon-side effects."""

    def __init__(self, instance, account_jid: str) -> None:
        if not account_jid:
            raise ValueError("effect fence requires an account JID")
        self.instance_id = instance.instance_id
        self.generation = instance.generation
        self.account_jid = account_jid
        self._reason_code: str | None = None
        self._guard = threading.RLock()

    def block(self, reason_code: str) -> None:
        if not reason_code:
            raise ValueError("effect fence reason must not be empty")
        with self._guard:
            self._reason_code = reason_code

    def assert_allows(self, instance, account_jid: str) -> None:
        with self._guard:
            if (
                instance.instance_id != self.instance_id
                or instance.generation != self.generation
                or account_jid != self.account_jid
            ):
                raise WhatsAppProviderError(
                    "WHATSAPP_EFFECT_SCOPE_MISMATCH",
                    "effect does not match the fenced instance, generation, and account",
                )
            if self._reason_code is not None:
                raise WhatsAppProviderError(self._reason_code, "WhatsApp effects are fenced")


def whatsapp_worker_command() -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "hyprial.daemon.impl.adapters.whatsapp.worker.tryx_driver",
    )


def require_mapping(value: object, code: str = "WHATSAPP_NATIVE_RESPONSE_INVALID") -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise WhatsAppProviderError(code, "native worker response must be an object")
    return dict(value)


def frozen_mapping(value: object, code: str = "WHATSAPP_NATIVE_RESPONSE_INVALID") -> FrozenJsonV1:
    try:
        frozen = freeze_json_v1(value)
    except (TypeError, ValueError) as error:
        raise WhatsAppProviderError(code, str(error)) from error
    return frozen


async def invoke_native(
    worker: NativeWorker,
    operation: str,
    payload: Mapping[str, object] | None = None,
    *,
    timeout: float = 15.0,
) -> dict[str, object]:
    request = {"operation": operation, **dict(payload or {})}
    try:
        result = await asyncio.to_thread(worker.invoke, request, timeout=timeout)
    except WhatsAppProviderError:
        raise
    except Exception as error:
        raise WhatsAppProviderError(
            "WHATSAPP_NATIVE_WORKER_UNAVAILABLE", f"native worker call failed: {error}"
        ) from error
    return require_mapping(result)


def require_artifact(artifact: ArtifactStatus) -> None:
    if artifact.available:
        return
    raise WhatsAppProviderError(artifact.reason_codes[0], ",".join(artifact.reason_codes))


__all__ = [
    "ArtifactStatus",
    "NativeWorker",
    "WhatsAppEffectFence",
    "WhatsAppProviderError",
    "invoke_native",
    "require_artifact",
    "whatsapp_worker_command",
]
