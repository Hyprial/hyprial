"""A grant that cannot be turned into keys is reported once for as long as that lasts.

Decision (service grant integration fix, 2026-11-27): ``key_unavailable``
follows the rule ``directory_unavailable`` already follows. It is remembered
by ``(org, grantId, reason)``; a derivation that reached its pushes and no
longer sees it forgets it, so the next occurrence, or the same grant with
another reason, is reported again.
"""

from __future__ import annotations

from typing import Any

from tests.daemon.service_grants.members.test_members_admission import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    KEY_UNAVAILABLE,
    LAB,
    ORG,
    PORT,
    _Supervisor,
    _tailnet,
    _World,
    world as world,
)


def test_key_unavailable_is_reported_once_and_again_after_recovery(world: _World) -> None:
    world.space(LAB, ALICE_NODE, BOB_NODE, parent=ORG)
    roster = {"readiness": {}, "tailnets": [_tailnet(world.space_id, ALICE_NODE, BOB_NODE, CAROL_NODE)]}
    world.grants().grant(org=LAB, port=PORT, device="@members")
    original = world.store.list_devices
    broken = [True]

    def flaky(org: str) -> Any:
        if broken[0] and org == LAB:
            raise OSError("directory unavailable")
        return original(org)

    world.store.list_devices = flaky  # type: ignore[method-assign]
    enforcer = world.enforcer(roster, _Supervisor())

    enforcer.apply_now()
    enforcer.apply_now()
    enforcer.apply_now()
    assert [fields["reason"] for fields in world.logged(KEY_UNAVAILABLE)] == ["org-devices-unavailable"]

    broken[0] = False
    enforcer.apply_now()
    assert len(world.logged(KEY_UNAVAILABLE)) == 1

    broken[0] = True
    enforcer.apply_now()
    assert [fields["reason"] for fields in world.logged(KEY_UNAVAILABLE)] == ["org-devices-unavailable"] * 2
