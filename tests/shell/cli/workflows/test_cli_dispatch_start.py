"""Thin tier startup uses real daemon selection/audit and existing lifecycle fixture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel.impl.ipc.client import _DAEMON_IPC_ROUNDTRIP_SECONDS
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.kernel import TIERS
from hyprial.daemon.impl.squire.probe import ProbeRun

from tests.identity.agents.registry.entity.support import application
from tests.identity.agents.registry.entity.support import _close_test_applications as _close_test_applications
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import shutil as _cli_shutil
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    app = application(tmp_path / "state")
    calls = []

    def ipc(method, params=None, *, timeout=None):
        # Mirror the request port once: an omitted timeout means the single
        # ordinary round-trip default, never a per-candidate probe budget.
        calls.append(
            (
                method,
                params,
                _DAEMON_IPC_ROUNDTRIP_SECONDS if timeout is None else timeout,
            )
        )
        try:
            return app.handle(method, params or {})
        except DaemonRequestError as error:
            raise _cli_get_services().CliError(error.code, str(error), error.data) from error

    monkeypatch.setattr(_cli_support, "_daemon_request", ipc)
    monkeypatch.setattr(_cli_shutil, "which", lambda _: "/usr/bin/true")
    return app, calls, tmp_path / "state" / "logs" / "daemon.jsonl"


def events(path: Path):
    if not path.exists():
        return []
    return [e for line in path.read_text().splitlines()
            if (e := json.loads(line)).get("event") == "dispatch.matrix.resolved"]


@pytest.mark.parametrize("tier", ["fast", "strong", "super"])
def test_tier_start_resolves_static_choice_without_probing(wired, monkeypatch, tier):
    """Selection is configuration; the start path must not pay for a probe.

    Migration: this replaces ``test_tier_start_resolves_and_audits_native_readings``.
    It went red because ``start --tier`` no longer probes (no readings in the
    audit) and no longer budgets one probe timeout per candidate; the new
    purpose is to pin exactly those two facts.
    """

    import hyprial.daemon.impl.squire.probe as probe_module
    app, calls, log = wired
    probes = []

    def runner(command, timeout):
        probes.append(command)
        return ProbeRun(0, "hello\n")

    monkeypatch.setattr(probe_module, "_subprocess_runner", runner)
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--tier", tier, "--name", "foo", "--headless", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    selected = TIERS[tier][0]
    # No model process is spawned on the selection path at all, and the IPC
    # deadline is one ordinary round-trip rather than len(pool) probe budgets.
    assert probes == []
    assert calls[0][0] == "dispatch.matrix.resolve"
    assert calls[0][2] == _DAEMON_IPC_ROUNDTRIP_SECONDS
    launch = next(params for method, params, _ in calls if method == "lifecycle.start")
    assert launch["provider"] == selected.harness
    assert launch["model"] == selected.model
    caps = app.agents.require("foo").capabilities
    assert caps["model"] == selected.model
    assert caps["interactive"] is False
    audit, = events(log)
    assert audit["tier"] == tier
    assert audit["selected"]["model"] == selected.model
    assert audit["probes"] == []
    assert audit["probed"] is False
    assert len(audit["candidates"]) == len(TIERS[tier])

    listed = CliRunner().invoke(_cli_root_app, ["agent", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["agents"][0]["capabilities"] == caps


def test_tier_start_does_not_route_around_a_probed_dead_candidate(wired, monkeypatch):
    """A candidate that fails its probe must still be the selected one.

    Migration: this replaces
    ``test_tier_start_falls_through_to_pi_with_provider_and_model_tier``.  That
    test asserted the old "fall through to the first live candidate" behaviour;
    the new purpose is the opposite contract -- a dead probe changes nothing,
    because the selection path does not probe at all.
    """

    import hyprial.daemon.impl.squire.probe as probe_module
    app, calls, log = wired
    monkeypatch.setattr(probe_module, "_subprocess_runner", lambda *_: pytest.fail("selection probed a harness"))
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--tier", "strong", "--name", "foo", "--headless", "--json"])
    assert result.exit_code == 0, result.output
    selected = TIERS["strong"][0]
    launch = next(params for method, params, _ in calls if method == "lifecycle.start")
    assert launch["provider"] == selected.harness
    assert launch["model"] == selected.model
    assert app.agents.require("foo").capabilities["tier"] == "strong"
    assert events(log)[0]["probes"] == []


def test_start_without_cwd_uses_and_creates_agent_workspace(wired):
    app, calls, _log = wired

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "codex", "--name", "foo", "--headless", "--json"],
    )

    assert result.exit_code == 0, result.output
    expected = _cli_get_services()._hyprial_home() / "agents" / "foo" / "workspace"
    create = next(params for method, params, _ in calls if method == "agent.create")
    launch = next(params for method, params, _ in calls if method == "lifecycle.start")
    assert create["cwd"] == str(expected)
    assert launch["cwd"] == str(expected)
    assert app.agents.require("foo").cwd == str(expected)


@pytest.mark.parametrize("selection", [
    ["pi"],  # the 2026-09-21 incident: a pi with no provider/model -> global default -> 403
    ["pi", "--provider", "kimi-coding", "--model", "k3"],
    ["claude", "--model", "sonnet"],
    ["codex"],
    ["pi", "--", "--provider", "kimi-coding", "--model", "k3"],
])
def test_tier_with_an_explicit_selection_is_refused_without_side_effects(wired, monkeypatch, selection):
    """--tier chooses harness+provider+model; combined with an explicit one it was
    dropped silently.  Now it is refused before anything happens.

    Migration: this replaces ``test_explicit_selection_is_not_resolved_or_audited``,
    which pinned the silent drop.
    """

    import hyprial.daemon.impl.squire.probe as probe_module
    app, calls, log = wired
    monkeypatch.setattr(probe_module, "_subprocess_runner", lambda *_: pytest.fail("a refused start probed"))
    # hyprial options precede the native passthrough separator.
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--tier", "super", "--name", "foo", "--headless", "--json", *selection])
    assert result.exit_code == 1, result.output
    body = json.loads(result.output)
    assert body["code"] == ipc_errors.INVALID_ARGUMENT
    assert "--tier super" in body["error"]
    assert calls == []
    assert events(log) == []
    assert app.agents.get("foo") is None
    assert app.desired_state.load().harnesses == ()


def test_explicit_selection_without_tier_still_starts(wired):
    """Control: the refusal is about the combination, not about explicit selection."""

    app, calls, log = wired
    result = CliRunner().invoke(_cli_root_app, [
        "agent", "start", "pi", "--provider", "kimi-coding", "--model", "k3",
        "--name", "foo", "--headless", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert "dispatch.matrix.resolve" not in [method for method, _, _ in calls]
    assert app.agents.require("foo").capabilities["model"] == "k3"
    assert app.agents.require("foo").capabilities["provider"] == "kimi-coding"


@pytest.mark.parametrize("selection", [["--provider", "kimi-coding"], ["--model", "k3"], ["--", "--model", "k3"]])
def test_partial_explicit_selection_without_harness_is_not_guessed(wired, monkeypatch, selection):
    import hyprial.daemon.impl.squire.probe as probe_module
    app, calls, _ = wired
    monkeypatch.setattr(probe_module, "_subprocess_runner", lambda *_: pytest.fail("partial explicit selection was probed"))
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--tier", "fast", "--name", "foo", "--headless", "--json", *selection])
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == ipc_errors.INVALID_ARGUMENT
    assert calls == []
    assert app.agents.get("foo") is None


def test_dead_probe_readings_do_not_block_start(wired, monkeypatch):
    """The old all-dead rejection is gone: selection cannot fail on liveness.

    Migration: this replaces ``test_all_dead_start_has_audit_but_no_agent_or_launch``,
    which asserted a ``DISPATCH_NO_CAPABLE_HARNESS`` abort.  The static
    selection starts the declared head instead, and no probe runs.
    """

    import hyprial.daemon.impl.squire.probe as probe_module
    app, calls, log = wired
    monkeypatch.setattr(probe_module, "_subprocess_runner", lambda *_: pytest.fail("selection probed a harness"))
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--tier", "super", "--name", "foo", "--headless", "--json"])
    assert result.exit_code == 0, result.output
    selected = TIERS["super"][0]
    launch = next(params for method, params, _ in calls if method == "lifecycle.start")
    assert launch["provider"] == selected.harness
    assert launch["model"] == selected.model
    assert app.agents.require("foo") is not None
    audit, = events(log)
    assert audit["ok"] is True
    assert audit["selected"]["model"] == selected.model
    assert audit["probes"] == []


def test_bad_tier_rejected_without_daemon_or_probes(wired):
    _, calls, _ = wired
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--tier", "guess", "--name", "foo", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == ipc_errors.INVALID_ARGUMENT
    assert calls == []


def test_tier_view_and_tier_start_share_one_resolution_source(wired, monkeypatch):
    """B10: the read-only matrix and the launch path call the same function.

    The static ``agent tiers`` view and ``agent start --tier`` (through the
    daemon's ``dispatch.matrix.resolve``) must not grow a second selector:
    one spy on ``matrix.resolve`` sees both callers.
    """

    from hyprial.daemon.impl.dispatch import matrix

    seen = []
    real = matrix.resolve

    def spy(tier):
        seen.append(tier)
        return real(tier)

    monkeypatch.setattr(matrix, "resolve", spy)

    listed = CliRunner().invoke(_cli_root_app, ["agent", "tiers", "--json"])
    assert listed.exit_code == 0, listed.output
    assert seen == list(TIERS)

    seen.clear()
    started = CliRunner().invoke(
        _cli_root_app, ["agent", "start", "--tier", "fast", "--name", "foo", "--headless", "--json"]
    )
    assert started.exit_code == 0, started.output
    assert seen == ["fast"]
    selected = TIERS["fast"][0]
    launch = next(params for method, params, _ in wired[1] if method == "lifecycle.start")
    assert (launch["provider"], launch["model"]) == (selected.harness, selected.model)
