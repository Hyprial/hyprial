"""``--provider hyprial``: the member entry, wired end to end on our side.

The gateway is the org's own endpoint, reached with a MEMBER credential, and it
routes on the model id's ``<vendor>/<model>`` prefix.  Three properties carry
the design, and each one fails silently if it regresses:

* the default endpoints are the gateway (a copy-paste of the vendor defaults
  would send member traffic to DeepSeek -- and it would work, which is worse);
* the member key goes to the child environment, never into argv;
* there is no fallback to a vendor key stored for another upstream.

Values are pinned as literals rather than against the module constants: a
constant compared with itself stays green however it is edited.
"""

from __future__ import annotations

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.model_provider import (
    ANTHROPIC_BASE_URL_ENV,
    DEEPSEEK_API_KEY_ENV,
    HYPRIAL_API_KEY_ENV,
    OPENAI_BASE_URL_ENV,
    AgentProviderCredentialMissing,
    ModelProviderError,
    claude_provider_environment,
    codex_provider_configuration,
    openai_base_url,
    validate_model_selection,
)

MEMBER_KEY = "member-gateway-key-never-log"
MODEL = "anthropic/claude-sonnet-4-6"


def spec(harness: str, *, model: str | None = MODEL, provider: str = "hyprial") -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        cwd="/workspace",
        model_provider=provider,
        model=model,
    )


def test_the_gateway_is_the_default_endpoint_for_both_surfaces() -> None:
    environment = {HYPRIAL_API_KEY_ENV: MEMBER_KEY}

    claude_env = claude_provider_environment(spec("claude"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == "https://model.hyprial.ai"

    argv, _ = codex_provider_configuration(spec("codex"), environment)
    assert 'model_providers.hyprial.base_url="https://model.hyprial.ai/v1"' in argv


def test_an_explicit_override_still_outranks_the_gateway_default() -> None:
    """The override is how a deployment points hyprial traffic at staging."""

    environment = {
        HYPRIAL_API_KEY_ENV: MEMBER_KEY,
        ANTHROPIC_BASE_URL_ENV: "https://staging.hyprial.test",
        OPENAI_BASE_URL_ENV: "https://staging.hyprial.test/v1",
    }

    claude_env = claude_provider_environment(spec("claude"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == "https://staging.hyprial.test"

    argv, _ = codex_provider_configuration(spec("codex"), environment)
    assert 'model_providers.hyprial.base_url="https://staging.hyprial.test/v1"' in argv


def test_naming_a_provider_picks_its_default_without_changing_the_old_one() -> None:
    """``openai_base_url`` keeps its historical default when no provider is named."""

    assert openai_base_url({}) == "https://api.deepseek.com/"
    assert openai_base_url({}, "hyprial") == "https://model.hyprial.ai/v1"


def test_the_member_key_reaches_the_child_environment_only() -> None:
    environment = {HYPRIAL_API_KEY_ENV: MEMBER_KEY}

    claude_env = claude_provider_environment(spec("claude"), environment)
    assert claude_env["ANTHROPIC_AUTH_TOKEN"] == MEMBER_KEY

    argv, codex_env = codex_provider_configuration(spec("codex"), environment)
    assert codex_env == {HYPRIAL_API_KEY_ENV: MEMBER_KEY}
    assert all(MEMBER_KEY not in part for part in argv), "the key must not enter argv"


def test_a_vendor_key_is_never_substituted_for_a_member_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A DeepSeek key in the environment (or Pi's auth.json) is not a login.

    Substituting it would bill the vendor account while the operator believes
    the launch went through the org gateway.
    """

    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "auth.json").write_text(
        '{"deepseek": {"key": "vendor-key-never-log"}}', encoding="utf-8"
    )
    environment = {
        DEEPSEEK_API_KEY_ENV: "vendor-key-never-log",
        "HOME": str(home),
    }

    with pytest.raises(AgentProviderCredentialMissing) as failure:
        claude_provider_environment(spec("claude"), environment)

    assert HYPRIAL_API_KEY_ENV in str(failure.value)
    assert "vendor-key-never-log" not in str(failure.value)


def test_a_bare_model_name_is_refused_and_the_refusal_shows_the_shape() -> None:
    with pytest.raises(ModelProviderError) as failure:
        validate_model_selection("claude", "hyprial", "claude-sonnet-4-6")

    assert "anthropic/claude-sonnet-4-6" in str(failure.value)


def test_a_missing_model_is_refused_rather_than_defaulted() -> None:
    """There is no hyprial default model: the vendor segment is the caller's."""

    with pytest.raises(ModelProviderError):
        validate_model_selection("codex", "hyprial", None)


def test_a_vendor_qualified_model_is_accepted_on_both_harnesses() -> None:
    validate_model_selection("claude", "hyprial", MODEL)
    validate_model_selection("codex", "hyprial", MODEL)


def test_codex_uses_the_chat_surface_not_the_responses_one() -> None:
    """The gateway serves chat completions; a copied "responses" breaks every turn."""

    argv, _ = codex_provider_configuration(spec("codex"), {HYPRIAL_API_KEY_ENV: MEMBER_KEY})

    assert 'model_providers.hyprial.wire_api="chat"' in argv
    assert 'model_providers.hyprial.requires_openai_auth=false' in argv
    assert 'model_providers.hyprial.env_key="HYPRIAL_API_KEY"' in argv


def test_pi_is_refused_with_the_gap_named() -> None:
    """pi needs a registered catalog entry; failing inside pi hides the cause."""

    with pytest.raises(ModelProviderError) as failure:
        validate_model_selection("pi", "hyprial", MODEL)

    assert "catalog" in str(failure.value)


def test_deepseek_selection_is_untouched() -> None:
    """The pre-existing path must be byte-identical (no default drift)."""

    environment = {DEEPSEEK_API_KEY_ENV: "secret"}

    claude_env = claude_provider_environment(spec("claude", model=None, provider="deepseek"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"

    argv, _ = codex_provider_configuration(spec("codex", model=None, provider="deepseek"), environment)
    assert 'model_providers.deepseek.base_url="https://api.deepseek.com/"' in argv
    assert 'model_providers.deepseek.wire_api="responses"' in argv
