"""Read-only first-run planning and its CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
from hyprial.daemon.impl.adapters.lark.state.records import ReplyRoute, RequestCorrelation
from hyprial.shell.impl.onboarding.plan import (
    OnboardingPlanError,
    OnboardingStateReader,
    plan_first_run,
)
from hyprial.identity import UserProfileStore
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "runtimeReady": True,
        "agents": [
            {"actor": "default", "kind": "default"},
            {"actor": "worker", "kind": "worker"},
        ],
        "squire": {"configured": True},
        "lark": {
            "available": True,
            "adapters": 1,
            "authorized": True,
            "routes": 1,
            "pinned": True,
            "roundTripVerified": True,
        },
    }
    value.update(overrides)
    return value


def _state(plan: dict[str, object], step_id: str) -> str:
    steps = plan["steps"]
    assert isinstance(steps, list)
    return next(
        step["state"]
        for step in steps
        if isinstance(step, dict) and step["id"] == step_id
    )


def test_complete_snapshot_matches_the_reference_plan_shape() -> None:
    plan = plan_first_run(_snapshot())

    assert plan["complete"] is True
    assert plan["remaining"] == 0
    assert plan["blockedCount"] == 0
    assert plan["needsHuman"] is False
    assert plan["next"] is None
    assert [step["id"] for step in plan["steps"]] == [
        "runtime-ready",
        "default-agent",
        "worker-agent",
        "squire",
        "lark-app",
        "lark-authorize",
        "lark-route",
        "lark-pin",
        "lark-roundtrip",
    ]


def test_half_configured_lark_offers_only_the_runnable_next_actions() -> None:
    plan = plan_first_run(
        _snapshot(
            lark={
                "available": True,
                "adapters": 1,
                "authorized": False,
                "routes": 0,
                "pinned": False,
                "roundTripVerified": False,
            }
        )
    )

    assert _state(plan, "lark-app") == "done"
    assert _state(plan, "lark-authorize") == "ready"
    assert _state(plan, "lark-route") == "ready"
    assert _state(plan, "lark-pin") == "blocked"
    assert _state(plan, "lark-roundtrip") == "blocked"
    assert plan["next"] == {
        "id": "lark-authorize",
        "title": "租户授权 scopes",
        "interactive": True,
        "action": "hyprial adapter scopes authorize <name> --interactive",
        "state": "ready",
        "reason": "尚未完成",
    }
    assert plan["needsHuman"] is True


def test_lark_route_is_not_interactive_once_the_chat_can_be_observed() -> None:
    """The route target is observed, not typed, whenever the machine saw one chat.

    The outbound route points at the chat the user already talked to the bot in,
    and that id is in the adapter's own inbound observations.  Like ``lark-pin``
    the step carries ``interactive: false`` and is decided from the snapshot: the
    action registry only offers the primitive when the answer is unique, so
    ``--auto`` skips it and a single-step apply says ``MANUAL_ACTION_REQUIRED``
    when it is not.  The manual command stays in ``action`` as the fallback a
    person can copy.
    """

    plan = plan_first_run(
        _snapshot(
            lark={
                "available": True,
                "adapters": 1,
                "authorized": True,
                "routes": 0,
                "pinned": True,
                "roundTripVerified": True,
            }
        )
    )

    route = next(step for step in plan["steps"] if step["id"] == "lark-route")
    assert route["state"] == "ready"
    assert route["interactive"] is False
    assert "<native_chat_id>" in route["action"]


def test_lark_pin_needs_no_person_once_the_bot_is_authorized() -> None:
    """The pin target is derivable, so it is not an interactive step.

    ``lark-pin`` names the Squire agent, which ``squire setup`` already created and
    whose actor name this machine derives itself; nothing here waits on a human.
    Binding an adapter is still the machine's action to take, so the step carries
    ``interactive: false`` and stays out of ``needsHuman``.
    """

    plan = plan_first_run(
        _snapshot(
            lark={
                "available": True,
                "adapters": 1,
                "authorized": True,
                "routes": 0,
                "pinned": False,
                "roundTripVerified": False,
            }
        )
    )

    pin = next(step for step in plan["steps"] if step["id"] == "lark-pin")
    assert pin["state"] == "ready"
    assert pin["interactive"] is False
    assert "squire" in pin["action"]
    assert _state(plan, "lark-pin") != "blocked"


def test_no_messaging_platform_skips_lark_steps_and_completes() -> None:
    plan = plan_first_run(_snapshot(lark={"available": False}))

    assert plan["complete"] is True
    assert plan["remaining"] == 0
    for step_id in ("lark-app", "lark-authorize", "lark-route", "lark-pin", "lark-roundtrip"):
        assert _state(plan, step_id) == "skipped"
        step = next(step for step in plan["steps"] if step["id"] == step_id)
        assert step["reason"] == "本机未检测到该消息平台"


def test_missing_prerequisite_blocks_dependants_and_names_the_wait() -> None:
    plan = plan_first_run(
        _snapshot(
            runtimeReady=False,
            agents=[],
            squire={"configured": False},
        )
    )

    assert _state(plan, "runtime-ready") == "ready"
    assert _state(plan, "default-agent") == "blocked"
    assert _state(plan, "squire") == "blocked"
    assert plan["next"]["id"] == "runtime-ready"
    default = next(step for step in plan["steps"] if step["id"] == "default-agent")
    assert default["reason"] == "等待前置：runtime-ready"


def test_bad_primitive_output_is_treated_as_absent_state_not_an_exception() -> None:
    bad_shapes: list[dict[str, object]] = [
        {"agents": "not-a-list", "squire": "bad", "lark": "bad"},
        {"agents": [None, 3, {"actor": "unrelated"}], "lark": {"adapters": "many"}},
    ]
    for snapshot in bad_shapes:
        plan = plan_first_run(snapshot)
        assert plan["complete"] is False
        assert _state(plan, "runtime-ready") == "ready"
        assert _state(plan, "default-agent") == "blocked"


def test_planning_is_deterministic_and_does_not_mutate_the_snapshot() -> None:
    snapshot = _snapshot()
    before = json.dumps(snapshot, sort_keys=True)

    first = plan_first_run(snapshot)
    second = plan_first_run(snapshot)

    assert first == second
    assert json.dumps(snapshot, sort_keys=True) == before


def test_non_mapping_snapshot_is_refused() -> None:
    with pytest.raises(OnboardingPlanError, match="snapshot object"):
        plan_first_run(None)  # type: ignore[arg-type]
    with pytest.raises(OnboardingPlanError, match="snapshot object"):
        plan_first_run("snapshot")  # type: ignore[arg-type]


def test_reader_builds_snapshot_from_real_read_surfaces(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    profiles = UserProfileStore(state / "users.json")
    profiles.ensure(
        owner="owner",
        owner_key="owner",
        login_name="owner",
        machine="machine",
        machine_key="machine",
    )
    profiles.associate_agent("owner", "agent:owner:machine:squire")

    def daemon(method: str, params: dict[str, object], **_kwargs: object) -> object:
        del params
        if method == "ps":
            return {"ok": True}
        if method == "agent.list":
            return {
                "ok": True,
                "agents": [
                    {"actor": "default"},
                    {"actor": "developer"},
                    {"actor": "unrelated"},
                ],
            }
        if method == "adapter.pins":
            return {"ok": True, "pins": {"feishu": "agent:owner:machine:squire"}}
        raise AssertionError(f"unexpected daemon method: {method}")

    reader = OnboardingStateReader(
        hyprial_home=home,
        state_dir=state,
        daemon_request=daemon,
        adapters_reader=lambda _home: {
            "ok": True,
            "adapters": [{"name": "feishu", "routes": [{"name": "owner"}]}],
        },
        scope_authorized=lambda name: name == "feishu",
    )

    snapshot = reader.read()
    plan = plan_first_run(snapshot)

    assert snapshot["runtimeReady"] is True
    assert snapshot["agents"] == [
        {"actor": "default", "kind": "default"},
        {"actor": "developer", "kind": "worker"},
        {"actor": "unrelated"},
    ]
    assert snapshot["squire"] == {"configured": True}
    assert snapshot["lark"] == {
        "available": True,
        "adapters": 1,
        "authorized": True,
        "routes": 1,
        "pinned": True,
        "roundTripVerified": False,
    }
    assert _state(plan, "default-agent") == "done"
    assert _state(plan, "worker-agent") == "done"
    assert _state(plan, "lark-roundtrip") == "ready"


def test_reader_degrades_bad_output_without_writing_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()

    def daemon(method: str, params: object, **_kwargs: object) -> object:
        del method, params
        return "not-json"

    reader = OnboardingStateReader(
        hyprial_home=home,
        state_dir=state,
        daemon_request=daemon,
        adapters_reader=lambda _home: ["not", "an", "object"],
        scope_authorized=lambda _name: False,
        lark_cli_reader=lambda: None,
    )

    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    snapshot = reader.read()
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert snapshot == {
        "runtimeReady": False,
        "agents": [],
        "squire": {"configured": False},
        "lark": {
            "available": False,
            "adapters": 0,
            "authorized": False,
            "routes": 0,
            "pinned": False,
            "roundTripVerified": False,
        },
    }
    assert after == before


def test_reader_uses_lark_cli_as_read_only_messaging_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()

    reader = OnboardingStateReader(
        hyprial_home=home,
        state_dir=state,
        daemon_request=lambda *_args, **_kwargs: {},
        adapters_reader=lambda _home: {"adapters": []},
        lark_cli_reader=lambda: "/usr/local/bin/lark-cli",
    )

    snapshot = reader.read()

    assert snapshot["lark"]["available"] is True


def test_reader_asks_the_configured_adapter_for_a_completed_exchange(
    tmp_path: Path,
) -> None:
    """``lark-roundtrip`` is decided by evidence, not by a marker this machine wrote.

    Only the adapter can answer whether two-way messaging works: an inbound
    message was forwarded and the platform returned a native id for the reply.
    The collector asks each configured adapter's own state for the chats where
    that happened; the store itself is covered in
    ``test_lark_state_sqlite.test_replied_chats_needs_a_paired_exchange_not_a_claim``.
    """

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    asked: list[tuple[Path, str]] = []

    def rounds(path: Path, name: str) -> tuple[str, ...]:
        asked.append((path, name))
        return ("oc_dm",) if name == "feishu" else ()

    reader = OnboardingStateReader(
        hyprial_home=home,
        state_dir=state,
        daemon_request=lambda *_args, **_kwargs: {},
        adapters_reader=lambda _home: {
            "adapters": [
                {"name": "feishu", "routes": [{"name": "owner"}]},
                {"name": "other", "routes": []},
            ]
        },
        round_trip_reader=rounds,
    )

    snapshot = reader.read()

    assert snapshot["lark"]["roundTripVerified"] is True
    assert asked == [
        (state / "adapters.sqlite3", "feishu"),
        (state / "adapters.sqlite3", "other"),
    ]


def test_reader_never_verifies_a_round_trip_it_cannot_observe(tmp_path: Path) -> None:
    """Absent, malformed, or unreadable evidence is absence, never verification.

    Wrong answers are the expensive failure here: a false ``True`` closes the
    step on a machine whose messaging never worked, so every degraded path has
    to land on ``False``.
    """

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()

    def read_with(rounds: object) -> object:
        return OnboardingStateReader(
            hyprial_home=home,
            state_dir=state,
            daemon_request=lambda *_args, **_kwargs: {},
            adapters_reader=lambda _home: {"adapters": [{"name": "feishu"}]},
            round_trip_reader=rounds,  # type: ignore[arg-type]
        ).read()["lark"]["roundTripVerified"]

    assert read_with(lambda _path, _name: ()) is False
    assert read_with(lambda _path, _name: [None, 7, ""]) is False

    def explode(_path: Path, _name: str) -> tuple[str, ...]:
        raise RuntimeError("unreadable store")

    assert read_with(explode) is False


def test_reader_skips_the_exchange_probe_when_no_adapter_is_configured(
    tmp_path: Path,
) -> None:
    """A machine with no adapter has no chat to verify and no store to open."""

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    asked: list[str] = []

    def rounds(_path: Path, name: str) -> tuple[str, ...]:
        asked.append(name)
        return ("oc_dm",)

    snapshot = OnboardingStateReader(
        hyprial_home=home,
        state_dir=state,
        daemon_request=lambda *_args, **_kwargs: {},
        adapters_reader=lambda _home: {"adapters": []},
        round_trip_reader=rounds,
    ).read()

    assert snapshot["lark"]["roundTripVerified"] is False
    assert asked == []
    assert not (state / "adapters.sqlite3").exists()


def test_reader_reads_the_real_adapter_store_under_the_worker_namespace(
    tmp_path: Path,
) -> None:
    """The default reader looks where the adapter worker actually writes.

    A configured adapter named ``feishu`` stores its state under ``lark:feishu``;
    the collector has to map the configured name the same way or the evidence
    would never be found.  Both writes go through the real store, so this covers
    the pairing rule end to end: an inbound message alone leaves the step
    outstanding, and the reply the platform confirmed closes it.
    """

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()

    def daemon(method: str, params: object, **_kwargs: object) -> object:
        del params
        if method == "ps":
            return {"ok": True}
        if method == "agent.list":
            return {"ok": True, "agents": [{"actor": "squire"}]}
        if method == "adapter.pins":
            return {"ok": True, "pins": {"feishu": "agent:owner:machine:squire"}}
        raise AssertionError(f"unexpected daemon method: {method}")

    store = LarkStateStore(state / "adapters.sqlite3", adapter="lark:feishu")

    def snapshot() -> dict[str, object]:
        return OnboardingStateReader(
            hyprial_home=home,
            state_dir=state,
            daemon_request=daemon,
            adapters_reader=lambda _home: {
                "adapters": [{"name": "feishu", "routes": [{"name": "owner"}]}]
            },
            scope_authorized=lambda name: name == "feishu",
        ).read()

    try:
        store.record_inbound(
            RequestCorrelation(
                harness_message_id="hm-1",
                message_id="om-1",
                chat_id="oc_dm",
                conversation_id="lark:oc_dm:oc_dm",
            ),
            event_id="evt-inbound",
        )

        # Only the round trip is missing: every prerequisite is satisfied, so a
        # false True here would close the whole plan on an untested channel.
        outstanding = snapshot()
        assert outstanding["lark"]["roundTripVerified"] is False
        assert _state(plan_first_run(outstanding), "lark-roundtrip") == "ready"

        store.record_reply(
            ReplyRoute(
                message_id="om-reply",
                actor_id="agent:owner:machine:default",
                actor_key="default",
                harness_message_id="hm-1",
                conversation_id="lark:oc_dm:oc_dm",
                chat_id="oc_dm",
            )
        )
        verified = snapshot()
    finally:
        store.close()

    assert verified["lark"]["roundTripVerified"] is True
    assert _state(plan_first_run(verified), "lark-roundtrip") == "done"


def test_onboarding_plan_cli_emits_the_reference_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: _snapshot())

    result = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "plan", "--json"])

    assert result.exit_code == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["complete"] is True
    assert payload["next"] is None
    assert len(payload["steps"]) == 9


def test_first_run_actions_match_the_agent_create_name_option() -> None:
    """The plan must not advertise a flag the CLI does not accept.

    ``hyprial agent create`` takes the actor name with ``--name``; a first-run
    step saying ``--kind`` failed as soon as a user copied it.
    """

    actions = {
        step["id"]: step["action"]
        for step in plan_first_run(_snapshot())["steps"]
        if step["id"] in {"default-agent", "worker-agent"}
    }

    assert actions == {
        "default-agent": "hyprial agent create --name default",
        "worker-agent": "hyprial agent create --name worker",
    }
