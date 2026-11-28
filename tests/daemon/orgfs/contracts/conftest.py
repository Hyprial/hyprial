# Decisions:
# - Design §7 (C4) and triage §5.1: the contract tests construct OrgFS only
#   through the ``orgfs_impl`` factory.  Skip is never used.
# - Package S (2026-11-25): the journal implementation is deleted, so the
#   fixture has one parameter.  Its id stays ``state`` so test ids remain
#   comparable with the runs before the switch.  The ``legacy_xfail`` marker
#   and the both-implementations xfail table are gone; the one contract the
#   design keeps as a known problem (design §3.1, §9 R18) carries its own
#   strict xfail in its module.
"""OrgFS implementation fixture for the contract suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .support import OrgFsImpl, StateImpl


@pytest.fixture(params=[pytest.param("state", id="state")])
def orgfs_impl(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[OrgFsImpl]:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    impl: OrgFsImpl = StateImpl()
    try:
        yield impl
    finally:
        impl.close()
