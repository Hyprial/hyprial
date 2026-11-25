"""Mutation probes: seeded synthetic red cases proving the goldens can fail.

Each probe applies one deliberate defect of the kind phase A must not
introduce, always in product code, and asserts the recorded scenario no
longer matches: a changed ``uuid5`` namespace for minted message ids and a
changed reply id for the PAC worker's ``reply:workflow-<uuid>`` reply.
A probe that stays green means the golden would not notice the regression.
The probes are skipped while recording: they compare against snapshots the
same run may be rewriting.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from hyprial.daemon.impl.application.messaging.inbox_surface import reply as reply_module
from hyprial.daemon.impl.application.messaging.inbox_surface import send as send_module
from tests.daemon.golden.test_control_golden import SCENARIOS as CONTROL_SCENARIOS
from tests.daemon.golden.test_message_golden import SCENARIOS as MESSAGE_SCENARIOS
from tests.support.golden.app import GoldenDaemon, golden_daemon as golden_daemon
from tests.support.golden.snapshot import recording

pytestmark = pytest.mark.skipif(recording(), reason="probes compare against recorded snapshots")


def _mismatch(
    build: Callable[..., GoldenDaemon],
    scenarios: dict,
    name: str,
) -> str | None:
    options, scenario = scenarios[name]
    golden = scenario(build(**options))
    golden.name = name
    return golden.mismatch()


def test_unmutated_scenarios_match(golden_daemon: Callable[..., GoldenDaemon]) -> None:
    assert _mismatch(golden_daemon, MESSAGE_SCENARIOS, "agent-roundtrip") is None
    assert _mismatch(golden_daemon, CONTROL_SCENARIOS, "workflows-live") is None


def test_a_changed_uuid5_namespace_turns_the_message_golden_red(
    golden_daemon: Callable[..., GoldenDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(send_module, "NAMESPACE_URL", uuid.NAMESPACE_DNS)
    difference = _mismatch(golden_daemon, MESSAGE_SCENARIOS, "agent-roundtrip")
    assert difference is not None and '"messageId"' in difference


def test_a_changed_pac_worker_reply_id_turns_the_workflow_golden_red(
    golden_daemon: Callable[..., GoldenDaemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = reply_module.reply_message_id
    monkeypatch.setattr(
        reply_module, "reply_message_id", lambda message_id: original(f"x:{message_id}")
    )
    difference = _mismatch(golden_daemon, CONTROL_SCENARIOS, "workflows-live")
    assert difference is not None and '"replyMessageId"' in difference
