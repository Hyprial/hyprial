"""provider_availability_contract: selection scenarios."""

from __future__ import annotations
import pytest
from hyprial.daemon.impl.dispatch import matrix
from hyprial.identity import RuntimeCapability

from .support import (
    _EXPECTED_SELECTION_CALL_SITES,
    _dead_run,
    _profile_store,
    _selection_call_sites,
)


def test_pc1_every_selection_reader_is_covered():
    """Coverage table for the negative claim.

    Mutation contract: adding a production caller of ``resolve`` (or an alias)
    anywhere under ``src/hyprial`` makes this test red until the new entry is
    accounted for.
    """

    assert _selection_call_sites() == _EXPECTED_SELECTION_CALL_SITES


@pytest.mark.parametrize("tier", ["fast", "strong", "super"])
def test_pc1_selection_reads_no_runtime_fact(monkeypatch, tier):
    """No probe, no profile read, and the declared head wins every time.

    Mutation contract: restoring the old ``if observed.status ==
    "unavailable": continue`` / first-live branch makes this red on the spy
    (the profile read) or on the selected candidate.
    """

    monkeypatch.setattr(
        matrix,
        "active_profile",
        lambda: pytest.fail("selection read the runtime profile"),
    )
    monkeypatch.setattr(
        "hyprial.daemon.impl.squire.probe._subprocess_runner",
        lambda *_: pytest.fail("selection probed a harness"),
    )
    choice = matrix.resolve(tier)
    assert choice.selected == matrix.TIERS[tier][0]
    assert choice.probed is False
    assert choice.readings == ()
    from hyprial.daemon.impl.pac.contracts.workflow import launch
    planned = launch({"tier": tier}, "workflow.launch")
    assert (planned["harness"], planned["provider"], planned["model"]) == choice.selected
    minimum = matrix.resolve_minimum_tier(tier)
    assert minimum.selected == matrix.minimum_pool(tier)[0]


def test_pc1_diagnosis_cannot_feed_selection():
    """``--probe`` readings are returned and read by nothing else.

    Mutation contract: wiring ``diagnose``'s result into ``resolve`` (or
    caching it as an availability fact) makes this red: the tier that would be
    "dead" is still resolved to its declared head.
    """

    readings = matrix.diagnose("fast", runner=lambda *_: _dead_run())
    assert [r.status for r in readings] == ["unavailable"] * len(matrix.TIERS["fast"])
    assert matrix.resolve("fast").selected == matrix.TIERS["fast"][0]


@pytest.mark.parametrize("tier", ["fast", "strong", "super"])
def test_pc3_marked_head_is_still_selected_and_mark_survives(tier, monkeypatch):
    """The marked candidate is the head of its pool and must still win.

    Mutation contract: restoring only ``if observed.status == "unavailable":
    continue`` makes this red (the selection moves to the second candidate);
    the bytes assertion proves the mark was not deleted to make it pass.
    """

    store = _profile_store()
    head = matrix.TIERS[tier][0]
    store.set_runtime_capability(
        "owner-key",
        RuntimeCapability(
            harness=head.harness,
            provider=head.provider,
            model=head.model,
            status="unavailable",
            reason="usage-limit",
            detail="quota readout",
            probed_at="2026-09-07T00:00:00Z",
        ),
    )
    before = store.path.read_bytes()
    monkeypatch.setattr(
        "hyprial.daemon.impl.squire.probe._subprocess_runner",
        lambda *_: pytest.fail("selection probed a harness"),
    )

    assert matrix.resolve(tier).selected == head
    assert matrix.resolve_minimum_tier(tier).selected == matrix.minimum_pool(tier)[0]
    # The mark is still written and still readable: it is diagnostic data now,
    # not a gate, and deleting it is not how this contract is satisfied.
    assert store.path.read_bytes() == before
    readings = matrix.diagnose(
        tier, profile=store.get("owner-key"), runner=lambda *_: _dead_run()
    )
    assert readings[0].diagnostic_mark is not None
    # No probe happened above, so the diagnosis is the only thing that talked
    # to the provider; selection did not.
    assert matrix.resolve(tier).readings == ()


def test_pc3_all_marked_pool_still_resolves():
    """A fully marked pool has no fallback to reach: the head is launched."""

    store = _profile_store()
    tier = "super"
    for candidate in matrix.TIERS[tier]:
        store.set_runtime_capability(
            "owner-key",
            RuntimeCapability(
                harness=candidate.harness,
                provider=candidate.provider,
                model=candidate.model,
                status="unavailable",
                reason="usage-limit",
                probed_at="2026-09-07T00:00:00Z",
            ),
        )
    assert matrix.resolve(tier).selected == matrix.TIERS[tier][0]
    assert (
        matrix.resolve_minimum_tier("strong").selected
        == matrix.minimum_pool("strong")[0]
    )
