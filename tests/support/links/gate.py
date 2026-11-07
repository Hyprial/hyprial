"""Scripted ``LinkGate`` implementations used by the WP4 admission tests.

``ScriptedLinkGate`` satisfies the runtime-checkable ``LinkGate`` protocol and
records every call, including the *thread* each call ran on: review B3 requires
the gate to run on the inbox actor thread, and only a real coordinator/facade
assembly can prove that.

``PermissiveLinkGate`` admits everything; existing tests bind it as an explicit
precondition when they dispatch agent-to-agent mail through a now-gated path.
``BudgetedLinkGate`` provides a single admission slot so the concurrency tests
can prove one budget is shared across every context entry.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from hyprial.daemon.impl.links.ports import Allow, LinkGate


class ScriptedLinkGate:
    """A gate whose answers are scripted per message id."""

    def __init__(
        self,
        *,
        known: Iterable[str] | None = None,
        admit: Mapping[str, bool] | None = None,
        default_admit: bool = True,
        raise_on: Iterable[str] | None = None,
    ) -> None:
        self.known = set(known or ())
        self.admit = dict(admit or {})
        self.default_admit = default_admit
        self.raise_on = set(raise_on or ())
        #: (method, kwargs) for every gate call, in order.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: method -> thread idents observed while calling it.
        self.threads: dict[str, list[int]] = {}
        self.allow_calls = 0
        self.settle_calls = 0

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        self.threads.setdefault(method, []).append(threading.get_ident())
        if method in self.raise_on:
            raise RuntimeError(f"scripted gate failure: {method}")

    def admit_send(
        self,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        now_ms: int,
    ) -> Allow:
        self.allow_calls += 1
        return Allow()

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        self._record("sender_known", recipient=recipient, sender=sender)
        return sender in self.known

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        self._record(
            "admit_dispatch",
            recipient=recipient,
            sender=sender,
            message_id=message_id,
            now_ms=now_ms,
            intent=intent,
            reply_to=reply_to,
        )
        if message_id in self.admit:
            return self.admit[message_id]
        return self.default_admit

    def settle_hint(self, *, actor: str, message_id: str) -> str:
        self.settle_calls += 1
        return "keep"

    def call_threads(self, method: str) -> list[int]:
        return list(self.threads.get(method, ()))

    def admit_calls(self) -> list[dict[str, Any]]:
        return [kwargs for method, kwargs in self.calls if method == "admit_dispatch"]

    def sender_known_calls(self) -> list[tuple[str, str]]:
        return [
            (kwargs["recipient"], kwargs["sender"])
            for method, kwargs in self.calls
            if method == "sender_known"
        ]


class PermissiveLinkGate(ScriptedLinkGate):
    """Every sender known, every message admitted: an explicit test premise."""

    def __init__(self) -> None:
        super().__init__(default_admit=True)

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        self._record("sender_known", recipient=recipient, sender=sender)
        return True


class BudgetedLinkGate(ScriptedLinkGate):
    """One shared admission budget, idempotent per message id.

    A repeat ask for an already admitted id never consumes another unit; two
    different ids cannot both take the last unit.
    """

    def __init__(self, budget: int, *, known: Iterable[str] | None = None) -> None:
        super().__init__(known=known, default_admit=False)
        self._budget = budget
        self._admitted: set[str] = set()

    @property
    def remaining(self) -> int:
        return self._budget

    def admit_dispatch(
        self,
        *,
        recipient: str,
        sender: str,
        message_id: str,
        now_ms: int,
        intent: str = "request",
        reply_to: str | None = None,
    ) -> bool:
        self._record(
            "admit_dispatch",
            recipient=recipient,
            sender=sender,
            message_id=message_id,
            now_ms=now_ms,
            intent=intent,
            reply_to=reply_to,
        )
        if message_id in self._admitted:
            return True
        if self._budget <= 0:
            return False
        self._budget -= 1
        self._admitted.add(message_id)
        return True


def satisfies_protocol(value: object) -> bool:
    return isinstance(value, LinkGate)


def permissive_admission_slot():
    """A bound-permissive admission slot for actor-backed tests."""

    from hyprial.daemon.impl.dispatch.admission import LinkAdmission

    slot = LinkAdmission()
    slot.bind(PermissiveLinkGate())
    return slot
