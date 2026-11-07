"""Matrix selection is static configuration; probing has one referee.

Policy (2026-09-21): availability is the user's guarantee, never a dispatch
input.  ``resolve``/``resolve_minimum_tier`` return the first declared
candidate and read no runtime fact.  Recorded probe facts are displayed by
``hyprial agent tiers``; fresh probes run only through
``hyprial agent tiers probe`` and :func:`classify_run`.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.dispatch import matrix
from hyprial.daemon.impl.squire import probe as probe_module
from hyprial.daemon.impl.squire.probe import (
    ProbeRun,
    RuntimeProber,
    build_probe_command,
    classify_run,
)
from hyprial.identity import RuntimeCapability, UserProfileStore
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_matrix_exact_order_and_native_commands():
    expected = {
        "fast": [("codex", None, "gpt-5.6-luna"), ("claude", None, "sonnet"),
                 ("pi", "deepseek", "deepseek-flash"), ("pi", "zai-coding-cn", "glm-5.3-flash")],
        "strong": [("codex", None, "gpt-5.6-sol"),
                   ("pi", "openai-codex", "gpt-5.6-sol"), ("claude", None, "fable"),
                   ("pi", "openai-codex", "gpt-6-astra"), ("claude", None, "opus"),
                   ("pi", "kimi-coding", "k3")],
        "super": [("claude", None, "fable"), ("pi", "openai-codex", "gpt-6-astra")],
    }
    assert {tier: [c[:3] for c in candidates] for tier, candidates in matrix.TIERS.items()} == expected
    for candidates in matrix.TIERS.values():
        for c in candidates:
            assert c.probe_cmd == build_probe_command(c.harness, c.model, c.provider)
            assert "--list-models" not in c.probe_cmd
    assert matrix.TIERS["fast"][0].probe_cmd == (
        "codex", "exec", "--skip-git-repo-check", "--model", "gpt-5.6-luna", "hello"
    )


@pytest.mark.parametrize("tier", ["fast", "strong", "super"])
def test_resolve_selects_first_candidate_without_probing(tier):
    """PC1's unit half: no probe call may happen on a selection path."""

    choice = matrix.resolve(tier)
    assert choice.selected == matrix.TIERS[tier][0]
    assert choice.probed is False
    assert choice.readings == ()
    assert choice.considered == matrix.TIERS[tier]
    # The old probe hook is gone from the signature; nothing on this path can
    # reach a runner even if one is lying around.
    assert "runner" not in matrix.resolve.__code__.co_varnames


def test_minimum_tier_keeps_declared_pool_order():
    for required in ("fast", "strong", "super"):
        pool = matrix.minimum_pool(required)
        choice = matrix.resolve_minimum_tier(required)
        assert choice.selected == pool[0]
        assert choice.considered == pool
        # The exact-tier answer is the head of the minimum pool it belongs to.
        assert choice.selected == matrix.resolve(required).selected


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (ProbeRun(None, "", timed_out=True), ("unavailable", "probe-timeout", None)),
        (ProbeRun(7, "plain failure"), ("unavailable", "probe-error", None)),
        (
            ProbeRun(1, "rate limit; try again at 2026-10-11T02:30:00+08:00"),
            ("unavailable", "usage-limit", "2026-10-10T18:30:00Z"),
        ),
        (ProbeRun(0, ""), ("available", None, None)),
    ],
)
def test_runtime_prober_and_referee_agree_for_every_discriminating_run(run, expected):
    """Timeout, failure, quota and empty output have one exact verdict."""

    assert classify_run(run) == expected
    result = RuntimeProber(
        runner=lambda *_: run,
        now=lambda: datetime(2026, 10, 10, tzinfo=UTC),
    ).probe("claude", "sonnet")
    assert (result.status, result.reason, result.retry_at) == expected


def test_runtime_prober_delegates_the_verdict_to_classify_run(monkeypatch):
    calls = []

    def referee(run):
        calls.append(run)
        return "unavailable", "probe-error", "owner-referee"

    run = ProbeRun(0, "non-empty output")
    monkeypatch.setattr(probe_module, "classify_run", referee)

    result = RuntimeProber(runner=lambda *_: run).probe("claude", "sonnet")

    assert calls == [run]
    assert (result.status, result.reason, result.retry_at) == (
        "unavailable",
        "probe-error",
        "owner-referee",
    )


def profile_store():
    store = UserProfileStore(Path(os.environ["HARNESS_STATE_DIR"]) / "users.json")
    store.ensure(owner=os.environ["HYPRIAL_OWNER"], owner_key="owner-key", login_name="unit",
                 machine="test-node", machine_key="machine-key")
    return store


def test_mark_is_recorded_and_cannot_change_selection():
    """A marked provider is still the selected one, and the mark is readable.

    This is the unit half of PC3: marking every candidate must not change who
    is chosen, and the mark must still be written and readable (it is kept for
    diagnosis).
    """

    store = profile_store()
    marked = matrix.TIERS["super"][0]
    store.set_runtime_capability("owner-key", RuntimeCapability(
        harness=marked.harness, provider=marked.provider, model=marked.model,
        status="unavailable", reason="usage-limit", detail="quota readout",
        probed_at="2026-09-07T00:00:00Z",
    ))
    for candidate in matrix.TIERS["super"][1:]:
        store.set_runtime_capability("owner-key", RuntimeCapability(
            harness=candidate.harness, provider=candidate.provider,
            model=candidate.model, status="unavailable", reason="usage-limit",
            probed_at="2026-09-07T00:00:00Z",
        ))
    assert matrix.resolve("super").selected == marked
    assert store.get("owner-key").runtime_capability(
        marked.harness, marked.model, marked.provider
    ).status == "unavailable"


def test_agent_tiers_reads_recorded_availability_without_probing(monkeypatch):
    store = profile_store()
    selected = matrix.TIERS["super"][0]
    store.set_runtime_capability("owner-key", RuntimeCapability(
        harness=selected.harness, provider=selected.provider, model=selected.model,
        status="unavailable", reason="usage-limit", detail="quota readout",
        retry_at="2026-10-11T00:00:00Z", probed_at="2026-10-10T00:00:00Z",
    ))
    monkeypatch.setattr(
        probe_module,
        "_subprocess_runner",
        lambda *_: pytest.fail("agent tiers must not run a probe"),
    )

    result = CliRunner().invoke(_cli_root_app, ["agent", "tiers", "--tier", "super", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert list(document["tiers"]) == ["super"]
    assert document["selection"]["super"]["model"] == selected.model
    assert document["tiers"]["super"][0]["availability"] == {
        "harness": selected.harness,
        "model": selected.model,
        "status": "unavailable",
        "reason": "usage-limit",
        "detail": "quota readout",
        "retryAt": "2026-10-11T00:00:00Z",
        "probedAt": "2026-10-10T00:00:00Z",
    }
    assert document["tiers"]["super"][1]["availability"] is None


def test_agent_tiers_rejects_unknown_tier():
    result = CliRunner().invoke(_cli_root_app, ["agent", "tiers", "--tier", "guess", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == ipc_errors.INVALID_ARGUMENT
