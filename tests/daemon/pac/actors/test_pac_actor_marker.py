"""The one strict PAC ownership marker shared by observe and the link service."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
from hyprial.daemon.impl.pac.actors.marker import pac_worker_marker


@pytest.mark.parametrize(
    ("nickname", "expected"),
    [
        ("pac:g:n:1:effect", "pac:g:n:1:effect"),
        ("pac:g:n:2:9a1b", "pac:g:n:2:9a1b"),
        # The activation marker always carries the incarnation field; a
        # nickname that only looks like "pac:..." is not PAC ownership.
        ("pac:g:n:effect", None),
        ("pac:g", None),
        ("pac:another-process", None),
        ("pac::n:1:e", None),
        ("pac:g:n:0:e", None),
        ("operator-nickname", None),
        (None, None),
    ],
)
def test_pac_worker_marker_is_a_strict_five_field_grammar(
    nickname: str | None, expected: str | None
) -> None:
    assert pac_worker_marker(nickname) == expected


@pytest.mark.parametrize("name,nickname,managed", [
    ("worker", "pac:g:n:1:effect", True),
    ("worker", "pac:g:n:effect", False),
    ("other", "pac:g:n:1:effect", False),
])
def test_registry_reuse_retains_only_matching_pac_ownership(name, nickname, managed):
    from hyprial.daemon.impl.pac.actors.marker import retain_managed_creation

    original = {"harness": "claude"}
    facts = retain_managed_creation("worker", original, [SimpleNamespace(name=name, nickname=nickname)])
    assert (facts.get("creationOrigin") == "managed-launch") is managed
    assert original == {"harness": "claude"}


def test_daemon_runtime_observe_reports_the_strict_marker() -> None:
    """observe reports the marker only when the strict grammar matches."""

    desired = SimpleNamespace(
        harness_spec=lambda _name, excluding=None: SimpleNamespace(
            nickname="pac:g:n:1:effect", harness="codex"
        )
    )
    application = SimpleNamespace(
        desired_state=desired,
        _canonical_harness_uri=lambda name, _spec: f"agent:test:node:{name}",
        _managed_worker_running=lambda _actor: False,
        agents=SimpleNamespace(get=lambda _name: None),
    )

    observation = DaemonActorRuntime(application).observe("worker")

    assert observation.identity_marker == "pac:g:n:1:effect"
