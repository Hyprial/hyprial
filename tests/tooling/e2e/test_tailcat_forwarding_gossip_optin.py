"""The gossip opt-in for Tailcat-forwarding acceptance daemons -- and the refusals that keep it narrow.

WHY THIS FILE EXISTS.  The S1/v12 acceptance's repair face is "make the peers visible to each
other": with gossip off a daemon reaches exactly the endpoints it was told about, so a joiner that
is not directly connected to the holder sees neither the liveliness token nor its holder (measured
2026-10-12: an active enumeration returned count=0 on all six sides, while each side saw its own
token).  The acceptance therefore has to RUN a daemon with ``HYPRIAL_ZENOH_GOSSIP=1``.

It could not.  ``contract/e2e/_daemon_runner.py`` builds every Tailcat-forwarding daemon's
environment from ``TAILCAT_FORWARDING_ENV``, which pinned ``HYPRIAL_ZENOH_GOSSIP="0"``, and
``require_tailcat_forwarding_isolation`` asserted that value -- so exporting the variable in the
launcher was silently overridden and a "gossip-enabled" run was in fact gossip-off (verified
2026-10-16: with ``HYPRIAL_ZENOH_GOSSIP=1`` in the parent environment, the constructed daemon's
env still read ``0``).  The opt-in added here lets a scenario ask for gossip explicitly.

THE CONTROLS THAT MATTER ARE THE REFUSALS.  Widening the env to allow gossip in one place would be
indistinguishable from deleting the isolation assertion, so most cases below assert that a
mismatch is still REJECTED, in BOTH directions: an opted-in daemon that lost its setting, a daemon
that carries gossip without having opted in, gossip without the forwarding opt-in at all, and a
routable or out-of-round filesystem endpoint even when gossip is on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "contract" / "e2e"))

from _tailcat_isolation import (  # noqa: E402
    TAILCAT_FORWARDING_ENV,
    TAILCAT_FORWARDING_ENV_WITH_GOSSIP,
    require_tailcat_forwarding_isolation,
    tailcat_forwarding_env,
)

ROUND = "/private/tmp/e2e-rounds/e2e-auth-s1v12-0fa6f99c/root-1/a"
IN_ROUND_SLOT = f"unixsock-stream//{ROUND}/home/state/tailcat/slots/slot-00"

#: The keys ``IsolatedDaemon.__init__`` sets for every daemon, before the forwarding opt-in is
#: applied.  ``HYPRIAL_PEER_DISCOVERY=0`` lives here, not in TAILCAT_FORWARDING_ENV, and the guard
#: asserts it -- so a stand-in without it would fail for the wrong reason.
BASE_DAEMON_ENV = {"HYPRIAL_PEER_DISCOVERY": "0"}


def daemon_env(*, gossip: bool = False) -> dict[str, str]:
    return {**BASE_DAEMON_ENV, **tailcat_forwarding_env(gossip=gossip)}


class FakeDaemon:
    """Duck-typed stand-in: the guard reads four attributes and no product code runs."""

    def __init__(
        self,
        *,
        tailcat_forwarding: bool = True,
        gossip: bool = False,
        env: dict[str, str] | None = None,
        endpoints: list[str] | None = None,
    ) -> None:
        self.tailcat_forwarding = tailcat_forwarding
        self.gossip = gossip
        self.env = dict(env) if env is not None else daemon_env(gossip=gossip)
        self.root = ROUND
        self._endpoints = list(endpoints) if endpoints is not None else [IN_ROUND_SLOT]
        self.stopped = False

    def command(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"zenoh": {"listen": list(self._endpoints), "connect": []}}

    def ps(self) -> dict[str, object]:
        return self.command()

    def stop(self) -> None:
        self.stopped = True


def test_the_default_environment_keeps_gossip_off() -> None:
    assert TAILCAT_FORWARDING_ENV["HYPRIAL_ZENOH_GOSSIP"] == "0"
    assert tailcat_forwarding_env()["HYPRIAL_ZENOH_GOSSIP"] == "0"


def test_the_optin_turns_gossip_on_and_changes_nothing_else() -> None:
    env = tailcat_forwarding_env(gossip=True)
    assert env["HYPRIAL_ZENOH_GOSSIP"] == "1"
    for name, wanted in TAILCAT_FORWARDING_ENV.items():
        if name != "HYPRIAL_ZENOH_GOSSIP":
            assert env[name] == wanted, name


def test_the_returned_environment_is_a_copy() -> None:
    env = tailcat_forwarding_env(gossip=True)
    env["HYPRIAL_ZENOH_GOSSIP"] = "0"
    assert TAILCAT_FORWARDING_ENV_WITH_GOSSIP["HYPRIAL_ZENOH_GOSSIP"] == "1"
    assert TAILCAT_FORWARDING_ENV["HYPRIAL_ZENOH_GOSSIP"] == "0"


@pytest.mark.parametrize("gossip", [False, True])
def test_the_guard_accepts_exactly_the_declared_gossip_value(gossip: bool) -> None:
    require_tailcat_forwarding_isolation(FakeDaemon(gossip=gossip))


def test_the_guard_refuses_an_optin_that_lost_its_setting() -> None:
    daemon = FakeDaemon(gossip=True, env=daemon_env(gossip=False))
    with pytest.raises(AssertionError, match="weakened after construction"):
        require_tailcat_forwarding_isolation(daemon)


def test_the_guard_refuses_gossip_without_the_opt_in() -> None:
    daemon = FakeDaemon(gossip=False, env=daemon_env(gossip=True))
    with pytest.raises(AssertionError, match="weakened after construction"):
        require_tailcat_forwarding_isolation(daemon)


def test_the_guard_refuses_gossip_without_the_forwarding_opt_in() -> None:
    daemon = FakeDaemon(tailcat_forwarding=False, gossip=False)
    with pytest.raises(AssertionError, match="tailcat_forwarding=True opt-in"):
        require_tailcat_forwarding_isolation(daemon)


def test_the_guard_still_refuses_a_routable_endpoint_even_with_gossip() -> None:
    daemon = FakeDaemon(gossip=True, endpoints=["tcp/100.64.30.1:17457"])
    with pytest.raises(AssertionError, match="escaped the loopback topology"):
        require_tailcat_forwarding_isolation(daemon)
    assert daemon.stopped, "a refused daemon must be stopped before the guard raises"


def test_the_guard_still_refuses_a_filesystem_socket_outside_the_round_directory() -> None:
    daemon = FakeDaemon(
        gossip=True,
        endpoints=["unixsock-stream//private/tmp/elsewhere/slot-00"],
    )
    with pytest.raises(AssertionError, match="escaped the loopback topology"):
        require_tailcat_forwarding_isolation(daemon)
