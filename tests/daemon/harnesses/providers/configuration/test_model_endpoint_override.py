"""The provider endpoint is configurable, and defaults to what it always was.

Two separate needs meet here.  A deployment could not point hyprial's harnesses
at its own inference service, because the vendor URL was a module constant;
and E2E could not point them at a local server, for the same reason.  Both
are the same missing lookup, so it is an ordinary configuration path rather
than a test-only branch -- a route only tests take is a route production
never proves.

The load-bearing property is therefore not "the override works" but "not
setting it changes nothing": every existing deployment must launch exactly
the environment and argv it launched before the seam existed.
"""

from __future__ import annotations

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.model_provider import (
    ANTHROPIC_BASE_URL_ENV,
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_MODEL,
    DEEPSEEK_OPENAI_BASE_URL,
    OPENAI_BASE_URL_ENV,
    claude_provider_environment,
    codex_provider_configuration,
)


def _spec(harness: str) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        cwd="/workspace",
        model_provider="deepseek",
        model=DEEPSEEK_MODEL,
    )


def test_unset_override_keeps_the_exact_vendor_endpoints() -> None:
    """No override configured must reproduce the pre-seam values verbatim.

    Pinned as literal strings rather than against the module constants: a
    constant compared to itself passes however it is edited, which would let
    the default drift while this test stayed green.
    """

    environment = {DEEPSEEK_API_KEY_ENV: "secret"}

    claude_env = claude_provider_environment(_spec("claude"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"

    argv, _ = codex_provider_configuration(_spec("codex"), environment)
    assert (
        'model_providers.deepseek.base_url="https://api.deepseek.com/"' in argv
    )


def test_override_redirects_both_formats() -> None:
    environment = {
        DEEPSEEK_API_KEY_ENV: "secret",
        ANTHROPIC_BASE_URL_ENV: "http://127.0.0.1:8931/anthropic",
        OPENAI_BASE_URL_ENV: "http://127.0.0.1:8931/openai",
    }

    claude_env = claude_provider_environment(_spec("claude"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8931/anthropic"

    argv, _ = codex_provider_configuration(_spec("codex"), environment)
    assert (
        'model_providers.deepseek.base_url="http://127.0.0.1:8931/openai"' in argv
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_override_falls_back_rather_than_launching_at_nothing(
    blank: str,
) -> None:
    """An empty variable is an unset one, not an instruction to use "".

    Exported-but-empty is a normal shell accident; treating it literally would
    point a production harness at an unusable endpoint on a typo.
    """

    environment = {
        DEEPSEEK_API_KEY_ENV: "secret",
        ANTHROPIC_BASE_URL_ENV: blank,
        OPENAI_BASE_URL_ENV: blank,
    }

    claude_env = claude_provider_environment(_spec("claude"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL

    argv, _ = codex_provider_configuration(_spec("codex"), environment)
    assert (
        f'model_providers.deepseek.base_url="{DEEPSEEK_OPENAI_BASE_URL}"' in argv
    )


def test_override_is_read_from_the_passed_environment_not_the_process() -> None:
    """Launches are configured per-spec; ambient os.environ must not leak in.

    A daemon serving several workers would otherwise redirect all of them
    because one launch was configured, which is the same class of bug as the
    ambient worker-identity variables this repo already strips.
    """

    environment = {DEEPSEEK_API_KEY_ENV: "secret"}
    claude_env = claude_provider_environment(_spec("claude"), environment)
    assert claude_env["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL
