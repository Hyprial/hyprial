"""Harness-local model-provider selection without persisting credentials.

``HarnessLaunchSpec`` stores only the provider id and model id.  Secrets stay
in the daemon environment and are translated into each vendor harness at the
last responsible moment:

* Claude Code receives DeepSeek's Anthropic-compatible environment.
* Codex receives command-line config overrides that name an environment key.
* Pi receives its native ``--provider`` / ``--model`` flags.
* DSH selects the model through its session-scoped HTTP API.

The helpers in this module never return a secret in argv or desired state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from hyprial.daemon.desired_state import HarnessLaunchSpec

DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_MODEL = "deepseek-flash"

# Retired model names, mapped to what replaces them.  The old name is refused
# loudly rather than aliased: an alias keeps every out-of-tree caller working
# until the vendor drops the name, and on that day they all fail at once with
# no list of who they are.  A refusal costs one broken launch per caller, and
# buys the list -- each one names itself the first time it runs.
RETIRED_MODEL_NAMES: Mapping[str, str] = {
    "deepseek-v4-flash": DEEPSEEK_MODEL,
    "deepseek-v4-pro": DEEPSEEK_MODEL,
}
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DSH_DEEPSEEK_PROVIDER = "deepseek-official"
DEEPSEEK_OPENAI_BASE_URL = "https://api.deepseek.com/"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

# Endpoint overrides.  Absent these, every value below is the vendor URL this
# module has always used, so an unconfigured launch is byte-identical to the
# one before this seam existed.
#
# The override exists because the endpoint was previously unreachable: a
# deployment could not point hyprial's harnesses at its own inference service,
# and a test could not point them at a local one.  Those are the same missing
# capability, which is why this is a normal lookup rather than a test-only
# branch -- a path only tests take is a path production never proves.
OPENAI_BASE_URL_ENV = "HYPRIAL_OPENAI_BASE_URL"
ANTHROPIC_BASE_URL_ENV = "HYPRIAL_ANTHROPIC_BASE_URL"


def openai_base_url(environment: Mapping[str, str]) -> str:
    """OpenAI-compatible endpoint for ``environment`` (vendor URL by default)."""

    configured = environment.get(OPENAI_BASE_URL_ENV, "").strip()
    return configured or DEEPSEEK_OPENAI_BASE_URL


def anthropic_base_url(environment: Mapping[str, str]) -> str:
    """Anthropic-compatible endpoint for ``environment`` (vendor URL by default)."""

    configured = environment.get(ANTHROPIC_BASE_URL_ENV, "").strip()
    return configured or DEEPSEEK_ANTHROPIC_BASE_URL


class ModelProviderError(ValueError):
    """A harness/provider/model combination cannot be launched safely."""


def validate_model_selection(
    harness: str,
    provider: str | None,
    model: str | None,
    *,
    context: str | None = None,
) -> None:
    """Validate the public provider/model vocabulary for one harness.

    ``context`` names whoever asked, when the caller knows.  It is optional
    because the useful half of a refusal is the part the caller cannot supply:
    the old name still arrives from places this repository cannot enumerate --
    a hand-written shell line, a tmux recipe, a saved command.  The message
    below therefore names the three producers we do know about, so a reader
    who has no context line still knows where to look.
    """

    if provider is not None and not provider.strip():
        raise ModelProviderError("--provider must not be empty")
    if model is not None and not model.strip():
        raise ModelProviderError("--model must not be empty")
    replacement = RETIRED_MODEL_NAMES.get(model or "")
    if replacement is not None:
        raise ModelProviderError(
            f"model {model!r} is retired; use {replacement!r}. "
            "This is a deliberate refusal, not a transient failure: the old "
            "name is not aliased, so whatever still sends it has to be found "
            "and changed. "
            f"Asked by: {context or 'caller did not identify itself'}. "
            "Look for the old name in the launch command (after '--'), in "
            "the agent's stored harnessArgs, or in a saved script or recipe."
        )
    if provider is None:
        return
    if harness == "claude" and provider not in {"anthropic", DEEPSEEK_PROVIDER}:
        raise ModelProviderError(
            "claude supports model providers 'anthropic' and 'deepseek'"
        )
    if harness == "codex" and provider not in {"openai", DEEPSEEK_PROVIDER}:
        raise ModelProviderError(
            "codex supports model providers 'openai' and 'deepseek'"
        )
    if (
        harness == "codex"
        and provider == DEEPSEEK_PROVIDER
        and model not in {None, DEEPSEEK_MODEL}
    ):
        raise ModelProviderError(
            "DeepSeek's Codex Responses endpoint currently supports only "
            f"{DEEPSEEK_MODEL!r}"
        )


def require_deepseek_key(
    environment: Mapping[str, str], *, allow_legacy_home_fallback: bool = True
) -> str:
    """Return the DeepSeek key or fail without including it in the error."""

    key = environment.get(DEEPSEEK_API_KEY_ENV, "").strip()
    if key:
        return key
    if not allow_legacy_home_fallback:
        raise ModelProviderError(
            "provider 'deepseek' requires an agent-owned DEEPSEEK_API_KEY; "
            "agent-home P2 never falls back to Pi auth under HOME"
        )
    home = Path(environment.get("HOME") or Path.home())
    for path in (
        home / ".pi" / "agent" / "auth.json",
        home / "my-pi-setup" / "agent" / "auth.json",
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry = value.get(DEEPSEEK_PROVIDER) if isinstance(value, dict) else None
        stored = entry.get("key") if isinstance(entry, dict) else None
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    raise ModelProviderError(
        "provider 'deepseek' requires DEEPSEEK_API_KEY or a deepseek API-key "
        "entry in Pi's auth.json"
    )


def claude_provider_environment(
    spec: HarnessLaunchSpec,
    environment: Mapping[str, str],
    *,
    allow_legacy_home_fallback: bool = True,
) -> dict[str, str]:
    """Return the per-process Claude environment delta for ``spec``."""

    validate_model_selection(spec.harness, spec.model_provider, spec.model)
    if spec.model_provider in {None, "anthropic"}:
        return {}
    key = require_deepseek_key(
        environment,
        allow_legacy_home_fallback=allow_legacy_home_fallback,
    )
    model = spec.model or DEEPSEEK_MODEL
    return {
        "ANTHROPIC_BASE_URL": anthropic_base_url(environment),
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }


def codex_provider_configuration(
    spec: HarnessLaunchSpec,
    environment: Mapping[str, str],
    *,
    allow_legacy_home_fallback: bool = True,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return Codex global config argv and environment delta for ``spec``."""

    validate_model_selection(spec.harness, spec.model_provider, spec.model)
    if spec.model_provider in {None, "openai"}:
        return (), {}
    key = require_deepseek_key(
        environment,
        allow_legacy_home_fallback=allow_legacy_home_fallback,
    )
    model = spec.model or DEEPSEEK_MODEL
    # Codex's official config reference recommends ``env_key`` over embedding
    # bearer tokens.  The key is copied only into the child environment; argv
    # and desired-state remain secret-free.
    return (
        (
            "-c",
            'model_provider="deepseek"',
            "-c",
            f'model="{model}"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            'model_providers.deepseek.name="deepseek"',
            "-c",
            f'model_providers.deepseek.base_url="{openai_base_url(environment)}"',
            "-c",
            'model_providers.deepseek.wire_api="responses"',
            "-c",
            f'model_providers.deepseek.env_key="{DEEPSEEK_API_KEY_ENV}"',
            "-c",
            "model_providers.deepseek.requires_openai_auth=false",
        ),
        {DEEPSEEK_API_KEY_ENV: key},
    )


def pi_model_args(spec: HarnessLaunchSpec) -> tuple[str, ...]:
    """Translate explicit selection to Pi's native arguments."""

    validate_model_selection(spec.harness, spec.model_provider, spec.model)
    def has_option(name: str) -> bool:
        return any(value == name or value.startswith(f"{name}=") for value in spec.args)

    if spec.model_provider is not None and has_option("--provider"):
        raise ModelProviderError(
            "--provider was supplied both as a hyprial option and after '--'"
        )
    if spec.model is not None and has_option("--model"):
        raise ModelProviderError(
            "--model was supplied both as a hyprial option and after '--'"
        )
    result: list[str] = []
    if spec.model_provider is not None:
        result.extend(("--provider", spec.model_provider))
    if spec.model is not None:
        result.extend(("--model", spec.model))
    return tuple(result)


def dsh_provider_id(provider: str) -> str:
    """Translate HYPRIAL's vendor id to DSH's routable provider id."""

    return DSH_DEEPSEEK_PROVIDER if provider == DEEPSEEK_PROVIDER else provider
