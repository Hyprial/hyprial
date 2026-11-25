"""ActorHostCustody must keep a failed host reachable for the retry.

``ActorHostRegistry.shutdown`` retains a host that refused to stop precisely
so a caller can retry.  Custody that drops its reference before the result is
known makes the second call a false success: the gateway close loop and the
transport close path both depend on ``False`` to keep retrying.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.actor_custody import ActorHostCustody


class _Registry:
    def __init__(self, *results: bool) -> None:
        self._results = list(results)
        self.calls = 0

    def shutdown(self, timeout: float) -> bool:
        del timeout
        self.calls += 1
        return self._results.pop(0)


def _owned(registry: object) -> ActorHostCustody:
    custody = ActorHostCustody(owner="test")
    custody._registry = registry  # type: ignore[assignment]
    custody._host = object()  # type: ignore[assignment]
    return custody


def test_failed_shutdown_stays_owned_and_retries_the_same_registry() -> None:
    registry = _Registry(False, True)
    custody = _owned(registry)

    assert custody.shutdown(0.1) is False
    assert custody.owned is True, "a failed shutdown must keep the host's owner"
    assert custody.shutdown(0.1) is True
    assert registry.calls == 2, "the retry must reach the registry again"
    assert custody.owned is False


class _RaisingRegistry:
    def shutdown(self, timeout: float) -> bool:
        del timeout
        raise RuntimeError("registry shutdown failed")


def test_raising_shutdown_keeps_the_registry_reachable() -> None:
    custody = _owned(_RaisingRegistry())

    with pytest.raises(RuntimeError, match="registry shutdown failed"):
        custody.shutdown(0.1)
    assert custody.owned is True
