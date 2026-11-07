"""A Claude worker on a vendor model id must state its own context window.

2026-10-17 runtime incident (six daemon-managed Claude workers on Mini,
claude-agent-sdk 0.2.125 / bundled CC 2.1.217): a conversation on
``deepseek-flash`` grew to ~1,017,000 input tokens plus a 32,000-token output
reservation and was rejected by the provider at the model's 1,048,576 limit.
No compact boundary existed anywhere in the transcript.  Claude Code sizes
proactive auto-compaction from a context window it derives from the model id,
and ``deepseek-flash`` is not an id it can resolve, so the launch said nothing
and the session had no boundary to compact at.

The remedy is the two documented Claude Code variables, applied where the
product already translates a vendor selection into the harness environment:

* ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` states the window the model actually has,
  so the session's accounting matches the provider's limit instead of an
  assumption.
* ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` puts the compaction boundary inside it,
  ahead of that limit.

Both values are pinned here as literals rather than compared against the
module constants: a constant compared with itself stays green however it is
edited, and the incident evidence is what fixes these numbers.

Auto-compact itself is left alone.  Claude Code enables it by default
(``autoCompactEnabled: true``; only ``DISABLE_COMPACT``/``DISABLE_AUTO_COMPACT``
turn it off), so the launch states a window and does not force the switch --
a user who disabled auto-compaction keeps that choice.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.claude import ClaudeConnector
from hyprial.daemon.impl.harnesses.codex import CodexAppServerClient
from hyprial.daemon.impl.harnesses.model_provider import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_MODEL,
    HYPRIAL_API_KEY_ENV,
    claude_provider_environment,
    codex_provider_configuration,
)
from hyprial.daemon.impl.harnesses.protocol.common import (
    ConnectorOptions,
    PtyHarnessProcess,
)
from hyprial.kernel import HarnessLaunchSpec

KEY = "test-deepseek-key-never-log"

#: The window Claude Code is told the model has: the provider rejects a
#: conversation at 1,048,576 tokens (incident, 2026-10-17).
MODEL_CONTEXT_TOKENS = "1048576"
#: Where auto-compaction is told to run.  Deliberately well inside the model
#: window: the compact boundary has to be reached before the provider's limit,
#: and both the output reservation and the boundary itself move the point at
#: which a session is over the line.
COMPACT_WINDOW = "200000"

WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
MAX_CONTEXT_ENV = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def spec(
    harness: str = "claude",
    *,
    provider: str | None = "deepseek",
    model: str | None = DEEPSEEK_MODEL,
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        cwd="/workspace",
        model_provider=provider,
        model=model,
    )


def environment(**extra: str) -> dict[str, str]:
    return {DEEPSEEK_API_KEY_ENV: KEY, **extra}


# ── the default itself ───────────────────────────────────────────────────────


def test_a_deepseek_worker_states_the_window_and_the_compaction_boundary() -> None:
    result = claude_provider_environment(spec(), environment())

    assert result[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS
    assert result[WINDOW_ENV] == COMPACT_WINDOW


def test_the_default_model_selection_gets_the_same_boundary() -> None:
    """``--provider deepseek`` with no ``--model`` is the same model."""

    result = claude_provider_environment(spec(model=None), environment())

    assert result[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS
    assert result[WINDOW_ENV] == COMPACT_WINDOW


def test_the_managed_sdk_worker_launches_with_the_boundary() -> None:
    """The daemon-managed launch (the six Mini roles) is the incident's path."""

    client = IsolatedAgentSdkClient(
        spec(),
        env=environment(),
        worker_command=("/fake/agent-sdk-worker",),
    )

    assert client._env[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS
    assert client._env[WINDOW_ENV] == COMPACT_WINDOW


def test_a_resumed_launch_carries_the_boundary_too() -> None:
    """Restart/resume must not be the one launch shape without a boundary.

    A resumed session is the one most likely to already be enormous, so the
    default cannot be tied to a fresh session or to a settings file being
    present on disk: it is computed for every launch.
    """

    client = IsolatedAgentSdkClient(
        spec(),
        env=environment(),
        worker_command=("/fake/agent-sdk-worker",),
        resume="0f9b4c1e-3a4d-4f6e-9b1a-8d2f5c7e0a11",
    )

    assert client.options["resume"] == "0f9b4c1e-3a4d-4f6e-9b1a-8d2f5c7e0a11"
    assert client._env[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS
    assert client._env[WINDOW_ENV] == COMPACT_WINDOW


def test_the_pty_connector_launch_carries_the_boundary(monkeypatch) -> None:
    """The non-SDK Claude launch is the same provider seam, not a parallel one.

    An operator's own terminal launch is the other way a session grows past a
    window, so it must size itself from the same table.  The spawn is captured
    rather than executed: this pins what the connector hands the child, not
    what a real claude does with it.
    """

    captured: dict[str, object] = {}

    def fake_spawn(harness, argv, **kwargs):
        captured["harness"] = harness
        captured["env"] = dict(kwargs.get("env") or {})
        return object()

    monkeypatch.setattr(PtyHarnessProcess, "spawn", fake_spawn)
    connector = ClaudeConnector(
        ConnectorOptions(("claude",), {DEEPSEEK_API_KEY_ENV: KEY})
    )
    connector.launch(spec())

    assert captured["harness"] == "claude"
    environment = captured["env"]
    assert environment[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS
    assert environment[WINDOW_ENV] == COMPACT_WINDOW


# ── precedence: an explicit value is never overwritten ───────────────────────


def test_an_explicit_window_in_the_launch_environment_is_not_overwritten() -> None:
    """A deployment that chose its own boundary keeps it.

    The value arrives in the same mapping the endpoint overrides arrive in, so
    the rule is the one this module already uses: read the configuration the
    caller handed over, and only fill what it did not state.  Asserted on the
    composed environment -- ``{**base, **delta}``, the merge both launch
    callers perform -- because that is what the child actually receives; a
    delta that merely omits the name cannot override anything.
    """

    base = environment(**{WINDOW_ENV: "500000"})
    delta = claude_provider_environment(spec(), base)

    assert {**base, **delta}[WINDOW_ENV] == "500000"
    # ...and the name that was not stated still gets its default.
    assert {**base, **delta}[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS


def test_an_explicit_model_window_is_not_overwritten() -> None:
    base = environment(**{MAX_CONTEXT_ENV: "400000"})
    delta = claude_provider_environment(spec(), base)

    assert {**base, **delta}[MAX_CONTEXT_ENV] == "400000"
    assert {**base, **delta}[WINDOW_ENV] == COMPACT_WINDOW


def test_an_operator_value_reaches_the_managed_worker_it_configured() -> None:
    """End to end through the incident's own launch path.

    The SDK client merges base then delta, so this is the property that
    decides what the six Mini roles would have run with: a deployment that
    exported its own window gets that window, and one that exported nothing
    gets the default.
    """

    configured = environment(**{WINDOW_ENV: "300000"})
    client = IsolatedAgentSdkClient(
        spec(),
        env=configured,
        worker_command=("/fake/agent-sdk-worker",),
    )
    assert client._env[WINDOW_ENV] == "300000"
    assert client._env[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_value_is_unset_not_an_instruction(blank: str) -> None:
    """Exported-but-empty is a shell accident, not a request for a broken launch.

    Same rule as the endpoint overrides: an empty variable falls back instead
    of being handed to the harness as the literal value.
    """

    result = claude_provider_environment(
        spec(),
        environment(**{WINDOW_ENV: blank, MAX_CONTEXT_ENV: blank}),
    )

    assert result[WINDOW_ENV] == COMPACT_WINDOW
    assert result[MAX_CONTEXT_ENV] == MODEL_CONTEXT_TOKENS


# ── isolation: nothing else is affected ──────────────────────────────────────


def test_native_anthropic_claude_is_untouched() -> None:
    """An unset/``anthropic`` provider is Claude on Claude; leave it alone."""

    assert claude_provider_environment(spec(provider=None), {}) == {}
    assert claude_provider_environment(spec(provider="anthropic"), {}) == {}


def test_the_gateway_provider_is_untouched() -> None:
    """``hyprial`` models are Anthropic ids the gateway routes; not sized here.

    The gateway's windows are the vendor's, not this table's: guessing one
    here would silently compact a 1M model at 200k, or the reverse.
    """

    result = claude_provider_environment(
        spec(provider="hyprial", model="anthropic/claude-sonnet-4-6"),
        {HYPRIAL_API_KEY_ENV: "member-key-never-log"},
    )

    assert MAX_CONTEXT_ENV not in result
    assert WINDOW_ENV not in result


def test_a_model_without_a_known_window_is_not_guessed() -> None:
    """The table is keyed by model: no entry, no invented numbers."""

    result = claude_provider_environment(
        spec(model="deepseek-some-other-model"), environment()
    )

    assert MAX_CONTEXT_ENV not in result
    assert WINDOW_ENV not in result


def test_codex_never_receives_claude_variables() -> None:
    """The other harnesses on the same provider are not touched."""

    argv, codex_environment = codex_provider_configuration(
        spec("codex"), environment()
    )

    assert codex_environment == {DEEPSEEK_API_KEY_ENV: KEY}
    assert all(WINDOW_ENV not in part and MAX_CONTEXT_ENV not in part for part in argv)

    client = CodexAppServerClient(
        spec("codex"),
        command=("/fake/codex",),
        env=environment(),
    )
    assert WINDOW_ENV not in client.command
    assert MAX_CONTEXT_ENV not in client.command


def test_the_key_is_still_the_only_secret_in_the_environment() -> None:
    """The new names carry numbers, never anything credential-shaped."""

    result = claude_provider_environment(spec(), environment())

    for name in (WINDOW_ENV, MAX_CONTEXT_ENV):
        assert result[name].isdigit()
