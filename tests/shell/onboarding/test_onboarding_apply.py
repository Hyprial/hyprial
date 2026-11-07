"""First-run apply: one step at a time, no bookkeeping, no silent claims."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.init.onboarding import FIRST_RUN_ROUTE_NAME
from hyprial.shell.impl.onboarding.apply import (
    INTERACTIVE_REQUIRED,
    MANUAL_ACTION_REQUIRED,
    PREREQUISITE_NOT_MET,
    STEP_FAILED,
    STEP_NOT_OBSERVED,
    UNKNOWN_STEP,
    OnboardingApplier,
    OnboardingApplyError,
    auto_onboarding_enabled,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "runtimeReady": True,
        "agents": [],
        "squire": {"configured": False},
        "lark": {"available": False},
    }
    value.update(overrides)
    return value


class Machine:
    """A snapshot that primitive stand-ins can mutate, plus their call log."""

    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def read(self) -> dict[str, object]:
        return self.snapshot

    def action(self, step_id: str) -> Any:
        """A primitive that runs and reports success without changing anything."""

        def run() -> dict[str, object]:
            self.calls.append(step_id)
            return {}

        return run


def _applier(
    machine: Machine,
    actions: dict[str, Any] | None = None,
    *,
    auto: bool = True,
) -> OnboardingApplier:
    return OnboardingApplier(
        snapshot=machine.read,
        actions=actions or {},
        auto_enabled=lambda: auto,
    )


def test_a_step_the_machine_already_shows_is_not_run_again() -> None:
    machine = Machine(_snapshot(agents=[{"actor": "default", "kind": "default"}]))

    result = _applier(machine).apply_step("default-agent")

    assert result["state"] == "already-done"
    assert result["changed"] is False
    assert machine.calls == []


def test_an_outstanding_prerequisite_performs_no_work() -> None:
    machine = Machine(_snapshot(runtimeReady=False))

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine, {"default-agent": lambda: {}}).apply_step("default-agent")

    assert failure.value.code == PREREQUISITE_NOT_MET
    assert failure.value.details["blockedBy"] == ["runtime-ready"]
    assert machine.calls == []


def test_an_interactive_step_is_never_run_headless() -> None:
    machine = Machine(
        _snapshot(lark={"available": True, "adapters": 1, "authorized": False})
    )

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine, {"lark-authorize": lambda: {}}).apply_step("lark-authorize")

    assert failure.value.code == INTERACTIVE_REQUIRED
    assert machine.calls == []


def test_lark_route_without_an_observed_chat_names_the_manual_step() -> None:
    """No inbound observation means no chat id, so the step refuses honestly.

    ``lark-route`` is no longer an interactive step (the id is observed rather
    than typed), but this machine has nothing to observe: the action registry is
    empty for it, so a single-step apply reports ``MANUAL_ACTION_REQUIRED`` with
    the command a person can run after messaging the bot once.
    """

    machine = Machine(
        _snapshot(lark={"available": True, "adapters": 1, "authorized": True})
    )

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine).apply_step("lark-route")

    assert failure.value.code == MANUAL_ACTION_REQUIRED
    assert "<native_chat_id>" in failure.value.details["action"]


def test_a_step_without_a_primitive_says_so_instead_of_claiming_success() -> None:
    machine = Machine(
        _snapshot(
            lark={
                "available": True,
                "adapters": 1,
                "authorized": True,
                "routes": 1,
                "pinned": True,
                "roundTripVerified": False,
            }
        )
    )

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine).apply_step("lark-roundtrip")

    assert failure.value.code == MANUAL_ACTION_REQUIRED
    assert failure.value.details["action"] == "hyprial message send … / 等待回执"


def test_a_primitive_that_does_not_change_the_machine_is_a_failure() -> None:
    machine = Machine(_snapshot())

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine, {"default-agent": machine.action("default-agent")}).apply_step(
            "default-agent"
        )

    assert failure.value.code == STEP_NOT_OBSERVED
    assert failure.value.details["observed"] == "ready"
    assert machine.calls == ["default-agent"]


def test_a_primitive_failure_keeps_its_upstream_code() -> None:
    class Boom(RuntimeError):
        code = "AGENT_EXISTS"

    def explode() -> object:
        raise Boom("agent already exists")

    machine = Machine(_snapshot())

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine, {"default-agent": explode}).apply_step("default-agent")

    assert failure.value.code == STEP_FAILED
    assert failure.value.details == {"cause": "Boom", "upstreamCode": "AGENT_EXISTS"}
    assert "agent already exists" in str(failure.value)


def test_applied_reports_the_primitive_detail() -> None:
    machine = Machine(_snapshot())

    def create_default() -> dict[str, object]:
        machine.calls.append("default-agent")
        machine.snapshot["agents"] = [{"actor": "default", "kind": "default"}]
        return {"actor": "default"}

    result = _applier(machine, {"default-agent": create_default}).apply_step(
        "default-agent"
    )

    assert result == {
        "ok": True,
        "mode": "step",
        "step": "default-agent",
        "state": "applied",
        "changed": True,
        "interactive": False,
        "detail": {"actor": "default"},
    }


def test_unknown_steps_are_refused_by_name() -> None:
    machine = Machine(_snapshot())

    with pytest.raises(OnboardingApplyError) as failure:
        _applier(machine).apply_step("not-a-step")

    assert failure.value.code == UNKNOWN_STEP
    assert machine.calls == []


def test_auto_does_nothing_when_the_switch_is_off() -> None:
    machine = Machine(_snapshot())

    result = _applier(machine, {"default-agent": machine.action("default-agent")}, auto=False).apply_auto()

    assert result["ok"] is True
    assert result["state"] == "disabled"
    assert result["changed"] == []
    assert machine.calls == []
    assert result["plan"]["steps"]


def test_auto_applies_every_ready_non_interactive_step_in_plan_order() -> None:
    machine = Machine(_snapshot())

    def create(name: str) -> Any:
        def run() -> dict[str, object]:
            machine.calls.append(name)
            machine.snapshot["agents"] = [
                *machine.snapshot["agents"],  # type: ignore[misc]
                {"actor": name, "kind": "worker" if name == "worker" else "default"},
            ]
            return {"actor": name}

        return run

    def setup_squire() -> dict[str, object]:
        machine.calls.append("squire")
        machine.snapshot["squire"] = {"configured": True}
        return {"configured": True}

    result = _applier(
        machine,
        {
            "default-agent": create("default"),
            "worker-agent": create("worker"),
            "squire": setup_squire,
        },
    ).apply_auto()

    assert result["ok"] is True
    assert result["changed"] == ["default-agent", "worker-agent", "squire"]
    assert result["stopped"] is None
    assert machine.calls == ["default", "worker", "squire"]
    # The returned plan is the post-apply read, so callers render it instead of
    # reasoning about ordering themselves.
    assert result["plan"]["complete"] is True


def test_auto_leaves_interactive_steps_to_the_shell_instead_of_failing() -> None:
    machine = Machine(
        _snapshot(lark={"available": True, "adapters": 1, "authorized": False})
    )

    def create(name: str) -> Any:
        def run() -> dict[str, object]:
            machine.calls.append(name)
            machine.snapshot["agents"] = [
                *machine.snapshot["agents"],  # type: ignore[misc]
                {"actor": name, "kind": "worker" if name == "worker" else "default"},
            ]
            return {"actor": name}

        return run

    result = _applier(
        machine,
        {"default-agent": create("default"), "worker-agent": create("worker")},
    ).apply_auto()

    assert result["ok"] is True
    assert result["changed"] == ["default-agent", "worker-agent"]
    assert machine.calls == ["default", "worker"]
    interactive = next(
        step for step in result["plan"]["steps"] if step["id"] == "lark-authorize"
    )
    assert interactive["state"] == "ready"
    assert interactive["interactive"] is True
    assert result["plan"]["needsHuman"] is True


def test_auto_stops_at_the_first_failure_and_keeps_what_landed() -> None:
    machine = Machine(_snapshot())

    def create_default() -> dict[str, object]:
        machine.calls.append("default-agent")
        machine.snapshot["agents"] = [{"actor": "default", "kind": "default"}]
        return {"actor": "default"}

    def create_worker() -> dict[str, object]:
        machine.calls.append("worker-agent")
        raise RuntimeError("daemon refused")

    result = _applier(
        machine,
        {"default-agent": create_default, "worker-agent": create_worker},
    ).apply_auto()

    assert result["ok"] is False
    assert result["changed"] == ["default-agent"]
    assert result["stopped"]["step"] == "worker-agent"
    assert result["stopped"]["errorCode"] == STEP_FAILED
    assert result["plan"]["steps"]


def test_auto_terminates_when_a_primitive_claims_success_without_evidence() -> None:
    machine = Machine(_snapshot())

    result = _applier(machine, {"default-agent": machine.action("default-agent")}).apply_auto()

    assert result["ok"] is False
    assert result["stopped"]["errorCode"] == STEP_NOT_OBSERVED
    assert machine.calls == ["default-agent"]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (None, True),
        ({}, True),
        ({"autoUpgrade": True}, True),
        ({"autoOnboarding": True}, True),
        ({"autoOnboarding": False}, False),
        ({"autoOnboarding": "false"}, True),
    ],
)
def test_the_auto_switch_defaults_on_and_only_false_disables_it(
    tmp_path: Path, settings: dict[str, object] | None, expected: bool
) -> None:
    home = tmp_path / "hyprial"
    home.mkdir()
    if settings is not None:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    assert auto_onboarding_enabled(home) is expected


def test_an_unreadable_settings_file_does_not_authorise_writes(tmp_path: Path) -> None:
    home = tmp_path / "hyprial"
    home.mkdir()
    (home / "settings.json").write_text("{not json", encoding="utf-8")

    assert auto_onboarding_enabled(home) is False


def test_cli_applies_one_named_step(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()

    def create_default() -> dict[str, object]:
        snapshot["agents"] = [{"actor": "default", "kind": "default"}]
        return {"actor": "default"}

    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        _cli_get_services(), "_onboarding_apply_actions", lambda: {"default-agent": create_default}
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "default-agent", "--json"])

    assert result.exit_code == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["state"] == "applied"
    assert payload["step"] == "default-agent"
    assert payload["changed"] is True


def test_cli_requires_exactly_one_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: _snapshot())
    monkeypatch.setattr(_cli_get_services(), "_onboarding_apply_actions", lambda: {})

    for argv in (
        ["init", "onboarding", "apply", "--json"],
        ["init", "onboarding", "apply", "default-agent", "--auto", "--json"],
    ):
        result = CliRunner().invoke(_cli_root_app, argv)
        assert result.exit_code == 1
        payload: dict[str, Any] = json.loads(result.stdout)
        assert payload["code"] == "INVALID_ARGUMENT"


def test_cli_surfaces_contract_error_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: _snapshot())
    monkeypatch.setattr(_cli_get_services(), "_onboarding_apply_actions", lambda: {})

    result = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "nope", "--json"])

    assert result.exit_code == 1
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == UNKNOWN_STEP


def _authorized_machine(pinned: bool = False) -> dict[str, object]:
    """A machine whose only outstanding work is binding the bot to Squire."""

    return _snapshot(
        agents=[
            {"actor": "default", "kind": "default"},
            {"actor": "worker", "kind": "worker"},
        ],
        squire={"configured": True},
        lark={
            "available": True,
            "adapters": 1,
            "authorized": True,
            "routes": 0,
            "pinned": pinned,
            "roundTripVerified": False,
        },
    )


def test_cli_binds_the_only_adapter_to_squire_without_a_person(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One configured adapter is an answer; the step runs in `--auto`."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "hyprial"))
    snapshot = _authorized_machine()
    calls: list[tuple[str, dict[str, object]]] = []

    def daemon(method: str, params: dict[str, object], **_kwargs: object) -> object:
        calls.append((method, params))
        if method == "adapter.pin":
            snapshot["lark"] = {**snapshot["lark"], "pinned": True}  # type: ignore[arg-type]
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        "hyprial.daemon.list_gateway_routes",
        lambda **_kwargs: {"ok": True, "adapters": [{"name": "feishu"}]},
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "--auto", "--json"])

    assert result.exit_code == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["changed"] == ["lark-pin"]
    assert payload["stopped"] is None
    assert calls == [("adapter.pin", {"name": "feishu", "actor": "squire"})]
    assert next(
        step for step in payload["plan"]["steps"] if step["id"] == "lark-pin"
    )["state"] == "done"


def test_cli_never_picks_a_channel_when_several_adapters_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two configured adapters is a choice the operator owns, not the machine's."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "hyprial"))
    snapshot = _authorized_machine()

    def daemon(_method: str, _params: dict[str, object], **_kwargs: object) -> object:
        raise AssertionError("onboarding must not bind an arbitrary adapter")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        "hyprial.daemon.list_gateway_routes",
        lambda **_kwargs: {
            "ok": True,
            "adapters": [{"name": "feishu"}, {"name": "lark-personal"}],
        },
    )

    auto = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "--auto", "--json"])
    assert auto.exit_code == 0, auto.stderr
    assert json.loads(auto.stdout)["changed"] == []

    single = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "lark-pin", "--json"])
    assert single.exit_code == 1
    payload: dict[str, Any] = json.loads(single.stdout)
    assert payload["ok"] is False
    assert payload["code"] == MANUAL_ACTION_REQUIRED
    assert payload["data"]["action"] == "hyprial adapter pin <name> <squire actor>"


def _first_route_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    candidates: list[dict[str, object]],
) -> tuple[dict[str, object], list[dict[str, Any]]]:
    """Wire the CLI to a machine whose only outstanding work is the first route."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "hyprial"))
    # Pinned already, so the first route is the machine's only outstanding work.
    snapshot = _authorized_machine(pinned=True)
    bound: list[dict[str, Any]] = []

    def fake_add(**kwargs: object) -> object:
        bound.append(dict(kwargs))
        snapshot["lark"] = {  # type: ignore[arg-type]
            **snapshot["lark"],  # type: ignore[arg-type]
            "routes": 1,
        }
        return {"ok": True, "adapter": {"name": kwargs["name"], "routes": []}}

    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: snapshot)
    monkeypatch.setattr(_cli_get_services(), "_route_candidates", lambda _name: candidates)
    monkeypatch.setattr(
        "hyprial.daemon.list_gateway_routes",
        lambda **_kwargs: {"ok": True, "adapters": [{"name": "feishu"}]},
    )
    monkeypatch.setattr("hyprial.daemon.add_gateway_route", fake_add)
    return snapshot, bound


def test_cli_binds_the_observed_chat_as_the_first_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One observed chat is one answer: the route is bound without a person."""

    snapshot, bound = _first_route_cli(
        monkeypatch, tmp_path, [{"chatId": "oc_dm", "chatType": "p2p"}]
    )

    result = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "--auto", "--json"])

    assert result.exit_code == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["changed"] == ["lark-route"]
    assert payload["stopped"] is None
    assert len(bound) == 1
    assert bound[0]["name"] == "feishu"
    assert bound[0]["make_default"] is True
    assert bound[0]["route"].name == FIRST_RUN_ROUTE_NAME
    assert bound[0]["route"].native_id == "oc_dm"
    assert next(
        step for step in payload["plan"]["steps"] if step["id"] == "lark-route"
    )["state"] == "done"
    assert snapshot["lark"]["routes"] == 1  # type: ignore[index]


@pytest.mark.parametrize(
    "candidates",
    [
        pytest.param([], id="nobody-has-messaged-the-bot"),
        pytest.param(
            [
                {"chatId": "oc_dm", "chatType": "p2p"},
                {"chatId": "oc_group", "chatType": "group"},
            ],
            id="two-chats-observed",
        ),
    ],
)
def test_cli_never_invents_a_first_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidates: list[dict[str, object]]
) -> None:
    """Zero chats and several chats both have no unique answer to bind."""

    _snapshot_value, bound = _first_route_cli(monkeypatch, tmp_path, candidates)

    auto = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "--auto", "--json"])
    assert auto.exit_code == 0, auto.stderr
    assert json.loads(auto.stdout)["changed"] == []
    assert bound == []

    single = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "lark-route", "--json"])
    assert single.exit_code == 1
    payload: dict[str, Any] = json.loads(single.stdout)
    assert payload["ok"] is False
    assert payload["code"] == MANUAL_ACTION_REQUIRED
    assert "<native_chat_id>" in payload["data"]["action"]


def test_cli_auto_reports_the_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "hyprial"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"autoOnboarding": False}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setattr(_cli_get_services(), "_onboarding_snapshot", lambda: _snapshot())
    monkeypatch.setattr(_cli_get_services(), "_onboarding_apply_actions", lambda: {})

    result = CliRunner().invoke(_cli_root_app, ["init", "onboarding", "apply", "--auto", "--json"])

    assert result.exit_code == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["state"] == "disabled"
    assert payload["changed"] == []
