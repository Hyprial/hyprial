"""The extended local-topology predicate, and the negative control that justified the extension.

WHY THIS FILE EXISTS.  On 2026-10-13 the v12 six-cell run was refused by a guard in
``contract/e2e/_tailcat_isolation.py`` whose predicate was "every zenoh endpoint is loopback TCP".
Under ``HYPRIAL_ZENOH_GOSSIP=1`` the daemon listens on ``unixsock-stream`` slots inside the round
directory, which cannot satisfy that predicate, so the guard fired by construction and could not
measure the configuration at all.  hq ruled path A: the guard's PURPOSE is "no ROUTABLE endpoint",
not "only TCP", because a unix socket lives in the filesystem domain and is therefore MORE local
than loopback TCP; the predicate was extended to accept filesystem-domain endpoints inside the
round directory, and this test is the negative control for that change.

THE CONTROLS THAT MATTER ARE THE REFUSALS.  A predicate that simply accepted more shapes would
pass every "does it work now" test and quietly stop guarding anything, so most of the cases below
assert that something is STILL refused: routable addresses, wildcard binds, non-TCP protocols,
filesystem sockets outside the round directory, and a prefix-sibling directory that shares the
round directory's name prefix.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "contract" / "e2e"))

from _tailcat_isolation import endpoint_is_local_only, local_topology_violations  # noqa: E402

ROUND = "/private/tmp/e2e-rounds/e2e-auth-s1v12-0fa6f99c/root-1/a"

ACCEPTED = [
    ("tcp/127.0.0.1:17458", "loopback TCP"),
    ("tcp/[::1]:17458", "loopback TCP v6"),
    (f"unixsock-stream//{ROUND}/home/state/tailcat/slots/slot-00", "filesystem socket inside the round directory"),
    (f"unixsock-stream//{ROUND}/home/state/tailcat/slots/slot-31", "the last of the thirty-two slots"),
]

REFUSED = [
    ("unixsock-stream//private/tmp/elsewhere/slot-00", "filesystem socket OUTSIDE the round directory"),
    (f"unixsock-stream//{ROUND}X/slot-00", "prefix-sibling directory, must not match the round directory"),
    ("tcp/100.64.30.1:17457", "routable tailnet address"),
    ("tcp/192.168.1.5:17457", "routable LAN address"),
    ("tcp/0.0.0.0:17457", "wildcard bind"),
    ("tcp/127.0.0.2:17457", "loopback but not 127.0.0.1"),
    ("udp/127.0.0.1:1234", "not a TCP transport"),
    ("ws/127.0.0.1:1234", "other protocol on loopback"),
    ("", "empty endpoint"),
]


def test_filesystem_endpoints_inside_the_round_directory_are_local() -> None:
    for endpoint, why in ACCEPTED:
        assert endpoint_is_local_only(endpoint, ROUND), f"{endpoint} should be local ({why})"


def test_every_routable_or_foreign_endpoint_is_still_refused() -> None:
    """The negative control.  Without these cases the extension would be indistinguishable from
    removing the guard."""
    for endpoint, why in REFUSED:
        assert not endpoint_is_local_only(endpoint, ROUND), f"{endpoint} must be refused ({why})"


def test_a_missing_round_root_refuses_filesystem_endpoints_rather_than_assuming() -> None:
    """A check that cannot fail is not a check: with no round directory to compare against, a
    filesystem endpoint is refused instead of being waved through."""
    assert not endpoint_is_local_only(f"unixsock-stream//{ROUND}/slot-00", None)


def test_local_topology_violations_reports_exactly_the_refused_ones() -> None:
    endpoints = [e for e, _ in ACCEPTED[:2]] + [e for e, _ in REFUSED[:4]]
    violations = local_topology_violations(endpoints, ROUND)
    assert violations == [e for e, _ in REFUSED[:4]]
    assert local_topology_violations([e for e, _ in ACCEPTED], ROUND) == []


def test_the_endpoint_from_the_actual_refusal_is_now_accepted() -> None:
    """Pinned to the real value the v12 run produced, so the regression this change exists for
    cannot silently come back."""
    real = (
        "unixsock-stream//private/tmp/e2e-rounds/e2e-auth-s1v12-0fa6f99c/"
        "root-20261013T055420Z-r1/a/home/state/tailcat/slots/slot-00"
    )
    root = "/private/tmp/e2e-rounds/e2e-auth-s1v12-0fa6f99c/root-20261013T055420Z-r1/a"
    assert endpoint_is_local_only(real, root)
