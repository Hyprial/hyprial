"""Adapter/worker collisions must not starve unrelated actors."""
from dataclasses import replace
import json

import pytest

from hyprial.daemon.impl.harnesses.turn_delivery.protocol import (
    harness_supports_before_delivery,
    mechanism_of,
)
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.harnesses.carrier.headless.test_headless_carrier import (
    NODE, BODY, _ScriptedHarnesses, _bridge, _inbox, _message,
)


class Harnesses(_ScriptedHarnesses):
    def streaming_actors(self):
        return ("sprite", "other")


@pytest.fixture
def runtime(tmp_path):
    inbox = _inbox(tmp_path / "inbox.sqlite3")
    harnesses = Harnesses()
    logs = []
    bridge = _bridge(tmp_path, inbox, harnesses, {"now": 100},
                     logs=lambda *a, **kw: logs.append((a, kw)))
    state = bridge.desired_state.load()
    # Real persisted pin and legacy import precede every delivery assertion.
    from hyprial.identity.impl.agents.registry import AgentRegistry
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    registry = AgentRegistry(tmp_path / "agents.sqlite3", owner="op", machine=NODE,
                             hyprial_home=tmp_path / "home")
    try:
        source = tmp_path / "personality"
        source.mkdir()
        registry.create("sprite", config={"sources": [{"path": str(source), "required": True}],
                                          "discovery": "explicit-only"})
        registry.pin("sprite", "sprite")
    finally:
        registry.close()
    collision = HarnessLaunchSpec(harness="lark", name="sprite", headless=True)
    other = replace(state.harnesses[0], name="other")
    legacy = state.to_json()
    legacy.pop("adapters")
    legacy.update(schemaVersion=2, providers=[s.to_json() for s in (collision, *state.harnesses, other)])
    bridge.desired_state.versioned_path.write_text(json.dumps(legacy))
    bridge.desired_state = DesiredStateStore(bridge.desired_state.legacy_path)
    loaded = bridge.desired_state.load()
    assert loaded.adapters[0].provider == "lark"
    assert all(spec.harness != "lark" for spec in loaded.harnesses)
    for name in harnesses.streaming_actors():
        assert inbox.receive(_message(name, recipient=f"agent:op:{NODE}:{name}")).accepted
    bridge._started = True
    yield bridge, harnesses, logs
    inbox.close()


def test_migrated_adapter_runs_and_keeps_pin_while_both_workers_receive(runtime, tmp_path):
    import sqlite3
    from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
    from hyprial.daemon.impl.composition.ports import LarkDesiredStatePort
    from hyprial.kernel import ChannelConfiguration
    from tests.daemon.adapters.lark.actors.actor_domain.support import _Launcher, _gateway

    bridge, harnesses, _ = runtime
    pins = tmp_path / "agents.sqlite3"
    before = pins.read_bytes()
    adapter = AdapterRuntime(_Launcher(), ChannelConfiguration(gateways=(_gateway("sprite"),)),
                             desired_state=LarkDesiredStatePort(bridge.desired_state))
    try:
        adapter.restore(bridge.desired_state.load().adapters)
        (status,) = adapter.status("sprite")
        assert status["processRunning"] is True
        assert status["desired"] is True
        assert "harness" not in status
        bridge.on_timer()
        assert {d.recipient.rsplit(":", 1)[1] for d in harnesses.dispatched} == {"sprite", "other"}
        with sqlite3.connect(pins) as db:
            assert db.execute("SELECT adapter,agent FROM pins").fetchall() == [("sprite", "sprite")]
        assert pins.read_bytes() == before
        assert all(s.harness != "lark" for s in bridge.desired_state.load().harnesses)
    finally:
        adapter.stop()


def test_retry_pump_warns_once_per_stale_streak(runtime):
    from types import SimpleNamespace
    from hyprial.daemon.impl.inbox import InboxAuthorityUnavailable

    bridge, _, logs = runtime
    outcomes = iter([True] * 23 + [False] + [True] * 20 + [False])
    calls = []
    def retry():
        stale = next(outcomes)
        calls.append(stale)
        if len(calls) == 45:
            bridge._retry_pump_halt.set()
        if stale:
            raise InboxAuthorityUnavailable("STALE_COMMAND: fence advanced")
        return []
    bridge.inbox = SimpleNamespace(retry_due=retry, next_retry_due_ms=lambda: 0)
    bridge._retry_pump_kick = SimpleNamespace(wait=lambda _: False, clear=lambda: None)
    bridge._retry_pump_run()
    warnings = [(a, kw) for a, kw in logs if a[2] == "inbox.retry_pump.starved"]
    assert len(warnings) == 2
    assert all(a[0] == "warn" and kw["consecutiveYields"] == 20 for a, kw in warnings)


def test_registration_failure_isolated(runtime):
    bridge, harnesses, logs = runtime
    def register(actor):
        if actor.endswith(":sprite"):
            raise ValueError("bad registration")
        return type("Registration", (), {"healthy": True})()
    bridge.harness_actor_registrar = register
    bridge.on_timer()
    assert "other" in bridge._actor_registrations
    assert len(harnesses.dispatched) == 2
    assert any(kw.get("phase") == "registration" for _, kw in logs)


def test_inactive_registration_close_failure_isolated(runtime):
    from types import SimpleNamespace

    bridge, harnesses, logs = runtime
    closed = []
    def fail_close(**kwargs):
        raise RuntimeError("close failed")
    bridge.harness_actor_registrar = lambda _: SimpleNamespace(healthy=True)
    bridge._actor_registrations.update({
        "old-bad": SimpleNamespace(close=fail_close),
        "old-good": SimpleNamespace(close=lambda **kw: closed.append("old-good")),
    })
    bridge.on_timer()
    assert closed == ["old-good"]
    assert len(harnesses.dispatched) == 2
    assert any(kw.get("phase") == "registration.close" and kw.get("actor") == "old-bad"
               for _, kw in logs)


def test_collision_tick_dispatches_both_and_finishes(runtime, monkeypatch):
    bridge, harnesses, _ = runtime
    tail = []
    monkeypatch.setattr(bridge, "_kick_retry_pump", lambda: tail.append("retry"))
    monkeypatch.setattr(bridge, "_prune_inbox", lambda: tail.append("prune") or ())
    try:
        bridge.on_timer()
    except KeyError:
        # RED evidence: the adapter collision aborts before Y or timer tails.
        assert not any(d.recipient.endswith(":other") for d in harnesses.dispatched)
        assert tail == []
        raise
    assert {d.recipient.rsplit(":", 1)[1] for d in harnesses.dispatched} == {"sprite", "other"}
    assert tail == ["retry", "prune"]
    assert bridge._harness_kind("sprite") == "pi"


@pytest.mark.parametrize("kind", ["lark", "future-harness", ""])
def test_unknown_mechanism_is_safe(kind):
    assert mechanism_of(kind) is None
    assert harness_supports_before_delivery(kind) is True


@pytest.mark.parametrize("kind,eligible", [
    ("claude", True), ("pi", True), ("codex", True), ("dsh", True),
    ("jev", False), ("user-proxy", False),
])
def test_declared_kinds_keep_batch_eligibility(runtime, monkeypatch, kind, eligible):
    bridge, _, _ = runtime
    monkeypatch.setattr(bridge, "_harness_kind", lambda _: kind)
    message = _message("ordinary")
    assert bridge._model_eligible(message) is eligible
    hook = replace(message, sender=f"agent:op:{NODE}:_turn-hooks",
                   payload=b'{"turnHookRequest":true}')
    assert bridge._model_eligible(hook) is False


@pytest.mark.parametrize("kind", ["claude", "pi", "codex", "dsh"])
def test_declared_model_carrier_keeps_scalar_fallback_without_batch_port(
    runtime, monkeypatch, kind
):
    bridge, harnesses, _ = runtime
    monkeypatch.setattr(bridge, "_harness_kind", lambda _: kind)
    monkeypatch.setattr(bridge.inbox, "batch_snapshot", None)
    bridge.on_timer()
    assert len(harnesses.dispatched) == 2
    assert all(not d.batch_cursor and not d.message_ids for d in harnesses.dispatched)
    assert all(BODY not in d.message for d in harnesses.dispatched)


def test_unknown_kind_never_injects_body_and_warns_once(runtime, monkeypatch):
    bridge, harnesses, logs = runtime
    monkeypatch.setattr(bridge, "_harness_kind", lambda _: "future-harness")
    bridge.on_timer()
    bridge.on_timer()
    assert harnesses.dispatched
    assert all(BODY not in d.message for d in harnesses.dispatched)
    warnings = [kw for a, kw in logs if a[2] == "harness.kind.unknown"]
    assert len(warnings) == 2  # once per actor, not per tick/message


def test_unknown_hook_recipient_gets_summary_not_protocol_body(runtime, monkeypatch):
    bridge, _, _ = runtime
    monkeypatch.setattr(bridge, "_harness_kind", lambda _: None)
    message = replace(_message("hook"), sender=f"agent:op:{NODE}:_turn-hooks",
                      payload=b'{"turnHookRequest":true,"message":"PRIVATE-BODY"}')
    text = bridge._injection_message_text(message)
    assert "PRIVATE-BODY" not in text
    assert "harness_read_body" in text


@pytest.mark.parametrize("strict_legacy_policy", [False, True])
def test_lookup_mutation_keeps_other_actor_delivered(runtime, monkeypatch, strict_legacy_policy):
    bridge, harnesses, logs = runtime
    # Exact pre-fix name-only algorithm, with C retained. B also protects the
    # lookup-only mutation. The strict variant removes B at the batch seam to
    # prove C independently against the original KeyError failure shape.
    def legacy_lookup(actor):
        state = bridge.desired_state.load()
        legacy_rows = tuple(HarnessLaunchSpec(a.provider, a.name, True) for a in state.adapters)
        for spec in (*legacy_rows, *state.harnesses):
            if spec.name == actor:
                return spec.harness
        return None
    monkeypatch.setattr(bridge, "_harness_kind", legacy_lookup)
    if strict_legacy_policy:
        original = bridge._model_eligible
        def legacy_policy(message):
            kind = bridge._harness_kind(message.recipient.rsplit(":", 1)[1])
            if kind == "lark":
                raise KeyError((kind, True))
            return original(message)
        monkeypatch.setattr(bridge, "_model_eligible", legacy_policy)
    bridge.on_timer()
    assert any(d.recipient.endswith(":other") for d in harnesses.dispatched)
    if strict_legacy_policy:
        assert any(kw.get("errorType") == "KeyError" for _, kw in logs)


@pytest.mark.parametrize("seam", ["kind", "capture", "dispatch"])
def test_one_actor_exception_does_not_abort_tick(runtime, monkeypatch, seam):
    bridge, harnesses, logs = runtime
    if seam == "kind":
        original = bridge._model_eligible
        def fail_kind(message):
            if message.recipient.endswith(":sprite"):
                raise KeyError(("lark", True))
            return original(message)
        monkeypatch.setattr(bridge, "_model_eligible", fail_kind)
    elif seam == "capture":
        from hyprial.daemon.impl.runtime import reconcile
        original = reconcile.capture_batch
        def fail_capture(inbox, recipient, **kwargs):
            if recipient.endswith(":sprite"):
                raise KeyError(("lark", True))
            return original(inbox, recipient, **kwargs)
        monkeypatch.setattr(reconcile, "capture_batch", fail_capture)
    else:
        original = harnesses.dispatch
        def fail_dispatch(actor, delivery):
            if actor == "sprite":
                raise KeyError(("lark", True))
            return original(actor, delivery)
        monkeypatch.setattr(harnesses, "dispatch", fail_dispatch)
    tail = []
    monkeypatch.setattr(bridge, "_kick_retry_pump", lambda: tail.append("retry"))
    monkeypatch.setattr(bridge, "_prune_inbox", lambda: tail.append("prune") or ())
    bridge.on_timer()
    assert [d.recipient.rsplit(":", 1)[1] for d in harnesses.dispatched] == ["other"]
    assert tail == ["retry", "prune"]
    assert any(a[0] == "error" and kw.get("actor") == "sprite"
               and kw.get("errorType") == "KeyError" for a, kw in logs)
