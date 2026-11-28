"""The directory accepts only protocol 3 announces.

Decision (phase D construction plan §3.3, package D0): the state host's
directory set ``_ANNOUNCE_PROTOCOL``; the legacy directory accepted any.
Decision (phase D, package T3): the D0 content-hook test, which subclassed the
legacy facade, was deleted; ``LocalOrgFs`` is the host those hooks served and
the contract suite (``tests/daemon/orgfs/contracts``) runs it directly.
Decision (package S, 2026-11-25): the hook lost its legacy half;
``RuntimeDirectory`` itself requires ``protocol: 3`` (design §4.1, §9 R15).
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.orgfs.composition.directory import RuntimeDirectory


def _announce(**extra: Any) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "type": "orgfs-announce",
        "node": "peer",
        "spaces": [],
        **extra,
    }


def test_the_directory_drops_announces_of_other_protocols() -> None:
    directory = RuntimeDirectory()

    assert directory._announce_shape_ok(_announce(protocol=3), "peer")
    assert not directory._announce_shape_ok(_announce(), "peer")
    assert not directory._announce_shape_ok(_announce(protocol=2), "peer")
    assert not directory._announce_shape_ok(_announce(protocol=3), "other")
