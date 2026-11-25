"""The announce filter only checks ``protocol`` once a host sets it.

Decision (phase D construction plan §3.3, package D0): the state host's
directory sets ``_ANNOUNCE_PROTOCOL``; the legacy directory accepts any.
Decision (phase D, package T3): the D0 content-hook test, which subclassed the
legacy facade, was deleted; ``StateOrgFs`` is the host those hooks served and
the contract suite (``tests/daemon/orgfs/contracts``) runs it directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.orgfs.composition.directory import RuntimeDirectory


class _Directory(RuntimeDirectory):
    pass


class _ProtocolDirectory(RuntimeDirectory):
    _ANNOUNCE_PROTOCOL = 3


def _announce(**extra: Any) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "type": "orgfs-announce",
        "node": "peer",
        "spaces": [],
        **extra,
    }


@pytest.mark.parametrize("extra", [{}, {"protocol": 3}, {"protocol": 2}])
def test_the_legacy_announce_filter_ignores_protocol(extra: dict[str, Any]) -> None:
    assert _Directory()._announce_shape_ok(_announce(**extra), "peer")


def test_a_host_with_an_announce_protocol_drops_other_protocols() -> None:
    directory = _ProtocolDirectory()

    assert directory._announce_shape_ok(_announce(protocol=3), "peer")
    assert not directory._announce_shape_ok(_announce(), "peer")
    assert not directory._announce_shape_ok(_announce(protocol=2), "peer")
    assert not directory._announce_shape_ok(_announce(protocol=3), "other")
