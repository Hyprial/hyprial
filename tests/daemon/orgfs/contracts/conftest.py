# Decisions:
# - Design §7 (C4) and triage §5.1: one batch of contract tests runs against
#   ``legacy`` (today) and ``state`` (phase D).  Skip is never used.
# - Phase D2 (plan §3.4 step 3, design §9 R17): every ``state`` factory
#   method is wired, so the two-stage ``NotImplementedError`` hook of D1 is
#   gone; every ``state`` case runs and must pass.
# - ``legacy_xfail(reason)`` marks a contract that the design requires but the
#   legacy implementation does not meet.  It applies a strict xfail only to
#   the legacy parameter; the reason names the design clause.
# - Design §9 R18 / §3.1: the writes a member makes before it learns of its
#   demotion are accepted by both implementations (a known problem, not a
#   regression).  That one contract is a strict xfail on both parameters; it
#   is named here because the test module keeps its ``legacy_xfail`` marker.
"""Parameterized OrgFS implementation fixture for the contract suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .support import LegacyImpl, OrgFsImpl, StateImpl

_XFAIL_ON_BOTH = {
    "test_writes_of_a_member_demoted_while_apart_do_not_change_the_owner": (
        "design §3.1: a member demoted to read-only while apart still passes the node "
        "gate, so its writes made before it learned of the demotion are accepted by both "
        "implementations (known problem, design §9 R18)"
    ),
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "legacy_xfail(reason): strict xfail on the legacy OrgFS implementation only",
    )


@pytest.fixture(params=[pytest.param("legacy", id="legacy"), pytest.param("state", id="state")])
def orgfs_impl(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Iterator[OrgFsImpl]:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    impl: OrgFsImpl = LegacyImpl() if request.param == "legacy" else StateImpl()
    both = _XFAIL_ON_BOTH.get(getattr(request.node, "originalname", ""))
    if both is not None:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=both))
    elif impl.name == "legacy":
        marker = request.node.get_closest_marker("legacy_xfail")
        if marker is not None:
            reason = marker.kwargs.get("reason") or (marker.args[0] if marker.args else "")
            request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))
    try:
        yield impl
    finally:
        impl.close()
