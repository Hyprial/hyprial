from __future__ import annotations

from dataclasses import dataclass

from hyprial.daemon.impl.dispatch.runtime.state import (
    DispatchOwnedMap,
    DispatchOwnedSet,
    DispatchStateAuthority,
    DispatchTable,
)


@dataclass(frozen=True)
class Attempt:
    delivery_id: str
    generation: int


def test_attempt_result_hold_and_notice_tables_are_one_mailbox() -> None:
    owner = DispatchStateAuthority()
    attempts = DispatchOwnedMap[Attempt](owner, DispatchTable.INFLIGHT)
    notices = DispatchOwnedSet(owner, DispatchTable.NOTICE_FAILURES_LOGGED)
    attempts["message-1"] = Attempt("message-1", 1)
    assert attempts.get("message-1") == Attempt("message-1", 1)
    assert attempts.values() == (Attempt("message-1", 1),)
    notices.add("message-1")
    assert "message-1" in notices
    notices.discard("message-1")
    assert "message-1" not in notices
    assert attempts.pop("message-1") == Attempt("message-1", 1)
    assert attempts.values() == ()
    projection = owner.projection()
    assert dict(projection.table_sizes)[DispatchTable.INFLIGHT.value] == 0
    assert projection.version >= 4
    assert owner.close(2)
