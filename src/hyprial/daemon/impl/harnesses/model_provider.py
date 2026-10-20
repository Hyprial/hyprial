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

from hyprial.kernel import HarnessLaunchSpec

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

# The member entry: agents reach the org's own gateway with their own Casdoor
# identity, and the gateway routes to a vendor by the model id's
# ``<vendor>/<model>`` prefix.  Two consequences the rest of this module obeys:
#
#   * the credential is a MEMBER credential, not a vendor key -- it comes from
#     ``hyprial login`` and lives in HYPRIAL_API_KEY.  It must not fall back to
#     Pi's auth.json, which holds a vendor key for a different upstream: an
#     unnoticed substitution would bill the wrong account (and hide a broken
#     login behind a working launch).
#   * a vendor-qualified model is mandatory: without the prefix the gateway
#     cannot route anywhere, so a bare model name is refused at launch (see
#     ``validate_model_selection``) instead of becoming a 400 one hop away.
HYPRIAL_PROVIDER = "hyprial"
HYPRIAL_API_KEY_ENV = "HYPRIAL_API_KEY"
# Anthropic's client appends its own versioned path, so the base is the bare
# origin; the OpenAI-compatible surface is addressed under /v1.
HYPRIAL_OPENAI_BASE_URL_DEFAULT = "https://model.hyprial.ai/v1"
HYPRIAL_ANTHROPIC_BASE_URL_DEFAULT = "https://model.hyprial.ai"


def openai_base_url(
    environment: Mapping[str, str], provider: str = DEEPSEEK_PROVIDER
) -> str:
    """OpenAI-compatible endpoint, defaulting to the selected provider's own.

    ``provider`` only chooses the DEFAULT: an explicit override still wins for
    every provider, which is what lets a deployment point a hyprial launch at a
    staging gateway (or a test at a local one).
    """

    configured = environment.get(OPENAI_BASE_URL_ENV, "").strip()
    if configured:
        return configured
    if provider == HYPRIAL_PROVIDER:
        return HYPRIAL_OPENAI_BASE_URL_DEFAULT
    return DEEPSEEK_OPENAI_BASE_URL


def anthropic_base_url(
    environment: Mapping[str, str], provider: str = DEEPSEEK_PROVIDER
) -> str:
    """Anthropic-compatible endpoint; ``provider`` chooses the default."""

    configured = environment.get(ANTHROPIC_BASE_URL_ENV, "").strip()
    if configured:
        return configured
    if provider == HYPRIAL_PROVIDER:
        return HYPRIAL_ANTHROPIC_BASE_URL_DEFAULT
    return DEEPSEEK_ANTHROPIC_BASE_URL


class ModelProviderError(ValueError):
    """A harness/provider/model combination cannot be launched safely."""


class AgentProviderCredentialMissing(ModelProviderError):
    """The resolved P2 launch has no required key; retry cannot change its grants."""

    # Keep legacy HOME reads (including transient I/O failures) on their existing
    # path. This refusal inspects only the immutable, fully composed P2 launch.
    permanent_start_failure = True


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
    if harness == "claude" and provider not in {
        "anthropic",
        DEEPSEEK_PROVIDER,
        HYPRIAL_PROVIDER,
    }:
        raise ModelProviderError(
            "claude supports model providers 'anthropic', 'deepseek' and 'hyprial'"
        )
    if harness == "codex" and provider not in {
        "openai",
        DEEPSEEK_PROVIDER,
        HYPRIAL_PROVIDER,
    }:
        raise ModelProviderError(
            "codex supports model providers 'openai', 'deepseek' and 'hyprial'"
        )
    if provider == HYPRIAL_PROVIDER and harness == "pi":
        # pi resolves a provider through a registered catalog entry, and the
        # gateway's entry (its models and their ``cost``) is not registered
        # yet.  Refusing here names that gap; launching anyway would fail
        # inside pi with a catalog error that says nothing about the cause.
        raise ModelProviderError(
            "provider 'hyprial' is not available to pi yet: pi needs a "
            "registered provider catalog (see hq/notes/"
            "impl-gateway-quota-and-harness-wiring-2026-10-12.md, P4). "
            "Use --harness claude or --harness codex for the gateway."
        )
    if provider == HYPRIAL_PROVIDER and (not model or "/" not in model):
        raise ModelProviderError(
            "provider 'hyprial' requires a vendor-qualified model: the gateway "
            "routes on the '<vendor>/<model>' prefix, so a bare name reaches "
            "no upstream. Pass --model <vendor>/<model>, for example "
            "'anthropic/claude-sonnet-4-6'"
            f" (got {model!r})."
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


def _pi_auth_key(root: Path, provider: str = DEEPSEEK_PROVIDER) -> str | None:
    """The API key for ``provider`` in one Pi native root, or None.

    One reader for every Pi credential source (the legacy HOME fallback and
    the agent's own native root), so the two cannot drift into disagreeing
    about what "a configured pasted credential" is.
    """

    try:
        value = json.loads((Path(root) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = value.get(provider) if isinstance(value, dict) else None
    stored = entry.get("key") if isinstance(entry, dict) else None
    return stored.strip() if isinstance(stored, str) and stored.strip() else None


def require_deepseek_key(
    environment: Mapping[str, str], *, allow_legacy_home_fallback: bool = True
) -> str:
    """Return the DeepSeek key or fail without including it in the error."""

    key = environment.get(DEEPSEEK_API_KEY_ENV, "").strip()
    if key:
        return key
    if not allow_legacy_home_fallback:
        raise AgentProviderCredentialMissing(
            "provider 'deepseek' requires an agent-owned DEEPSEEK_API_KEY; "
            "agent-home P2 never falls back to Pi auth under HOME"
        )
    home = Path(environment.get("HOME") or Path.home())
    for root in (home / ".pi" / "agent", home / "my-pi-setup" / "agent"):
        stored = _pi_auth_key(root)
        if stored:
            return stored
    raise ModelProviderError(
        "provider 'deepseek' requires DEEPSEEK_API_KEY or a deepseek API-key "
        "entry in Pi's auth.json"
    )


#: The settings file pi projects beside a launch, plus the trusted project's
#: own settings directory.  Both are part of pi's effective provider default.
PI_SETTINGS_NAME = "settings.json"
PI_PROJECT_SETTINGS_DIR = ".pi"


def _pi_settings_defaults(root: Path) -> dict[str, str]:
    """The ``defaultProvider``/``defaultModel`` in one pi settings file."""

    try:
        body = json.loads(
            (Path(root) / PI_SETTINGS_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(body, dict):
        return {}
    defaults: dict[str, str] = {}
    for key in ("defaultProvider", "defaultModel"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            defaults[key] = value.strip()
    return defaults


def pi_effective_model_selection(
    spec: HarnessLaunchSpec, *, settings_root: Path | None = None
) -> tuple[str | None, str | None]:
    """The provider/model the controlled loader will actually use.

    Precedence mirrors the loader exactly: the launch's explicit selection
    first, otherwise pi's effective settings -- the projected global settings
    merged with the trusted project's (project wins).  One resolution here
    keeps the credential boundary from disagreeing with the loader about
    which vendor a launch is about to bill.
    """

    provider = spec.model_provider
    model = spec.model
    if provider is not None and model is not None:
        return provider, model
    defaults: dict[str, str] = {}
    if settings_root is not None:
        defaults.update(_pi_settings_defaults(Path(settings_root)))
    if spec.cwd:
        defaults.update(
            _pi_settings_defaults(Path(spec.cwd) / PI_PROJECT_SETTINGS_DIR)
        )
    if provider is None:
        provider = defaults.get("defaultProvider")
    if model is None:
        model = defaults.get("defaultModel")
    return provider, model


def pi_provider_environment(
    spec: HarnessLaunchSpec,
    environment: Mapping[str, str],
    *,
    native_root: Path | None = None,
    settings_root: Path | None = None,
    allow_legacy_home_fallback: bool = True,
) -> dict[str, str]:
    """The per-process credential delta for one Pi launch.

    Pi is the only managed harness whose provider credential is not consumed
    from an environment variable exclusively, so its launch boundary had no
    provider step while claude/codex had one.  This is that step, and it is
    the one implementation -- it reuses :func:`require_deepseek_key` rather
    than restating the env-then-auth.json preference.

    The credential source stays the per-agent authorization: an agent secret
    granted as ``DEEPSEEK_API_KEY``.  Resolution order:

    * the launch environment -- the granted agent secret (the only source a
      P2 agent may use), or, on a legacy launch, an operator-exported key;
    * the agent's own Pi native root: pi's controlled loader already reads
      ``nativeRoot/auth.json``, so an environment copy would only shadow the
      link; this returns ``{}``;
    * ``allow_legacy_home_fallback`` (legacy launches only): the operator's
      ``HOME/.pi/agent/auth.json``, exactly what pi itself would read before
      agent-home P2 existed.

    A P2 launch with none of these refuses here: a secretless pi start is a
    permanent failure, and letting it run means pi spends its first turns
    failing ``PROVIDER_AUTHENTICATION_FAILED`` until the daemon blocks the
    agent as credential-invalid.  Permission to use the operator's credential
    is never invented here -- a P2 agent must be granted one explicitly.

    The check runs against the EFFECTIVE provider: a launch that names no
    provider still selects one from the agent's own pi settings, and the
    controlled loader would use that vendor.  Resolving it through
    :func:`pi_effective_model_selection` keeps this boundary and the loader
    from disagreeing about which vendor is about to be billed.
    """

    validate_model_selection(spec.harness, spec.model_provider, spec.model)
    effective_provider, _effective_model = pi_effective_model_selection(
        spec, settings_root=settings_root
    )
    if effective_provider in {None, "openai"}:
        # Pi's built-in vendors authenticate from its own login state.
        return {}
    if effective_provider not in {DEEPSEEK_PROVIDER, None, "openai"}:
        # Any other vendor is one of pi's own built-in catalog providers
        # (kimi-coding, zai, ...): pi authenticates it from its own login
        # state, exactly as it did before this seam existed.  Only the vendors
        # hyprial names itself need a translated credential delta.
        return {}
    key = environment.get(DEEPSEEK_API_KEY_ENV, "").strip()
    if not key and native_root is not None and _pi_auth_key(Path(native_root)):
        return {}
    if not key and allow_legacy_home_fallback:
        try:
            key = require_deepseek_key(
                environment, allow_legacy_home_fallback=True
            )
        except ModelProviderError:
            key = ""
    if not key:
        raise AgentProviderCredentialMissing(
            "provider 'deepseek' requires an agent-owned DEEPSEEK_API_KEY; "
            "grant one with 'hyprial agent secret grant <agent> ... "
            "--environment-name DEEPSEEK_API_KEY' before starting pi, then "
            "restart and unblock the agent"
        )
    return {DEEPSEEK_API_KEY_ENV: key}


def require_hyprial_key(environment: Mapping[str, str]) -> str:
    """Return the member's gateway key, or fail without echoing it.

    Deliberately has no Pi auth.json fallback (unlike ``require_deepseek_key``):
    that file holds a vendor key, and quietly billing a vendor key for a launch
    the member asked to route through the org gateway is exactly the confusion
    this seam exists to prevent.  A missing key here is a missing login.
    """

    key = environment.get(HYPRIAL_API_KEY_ENV, "").strip()
    if key:
        return key
    raise AgentProviderCredentialMissing(
        f"provider 'hyprial' requires {HYPRIAL_API_KEY_ENV} (the member "
        "credential written by `hyprial login`); this provider never falls "
        "back to a vendor key"
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
    if spec.model_provider == HYPRIAL_PROVIDER:
        # The model is already known to be vendor-qualified (validate above),
        # so it is passed through unchanged: the gateway is what owns the
        # vendor prefix, and rewriting it here would hide a caller's mistake.
        return {
            "ANTHROPIC_BASE_URL": anthropic_base_url(environment, HYPRIAL_PROVIDER),
            "ANTHROPIC_AUTH_TOKEN": require_hyprial_key(environment),
            "ANTHROPIC_MODEL": spec.model or "",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": spec.model or "",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": spec.model or "",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": spec.model or "",
            "CLAUDE_CODE_SUBAGENT_MODEL": spec.model or "",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
        }
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
    if spec.model_provider == HYPRIAL_PROVIDER:
        # ⚠️ wire_api="chat", NOT the "responses" the DeepSeek block below uses:
        # the gateway serves the chat-completions surface, and a copied
        # "responses" value would fail on every turn while looking correct.
        return (
            (
                "-c",
                'model_provider="hyprial"',
                "-c",
                f'model="{spec.model or ""}"',
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                'model_providers.hyprial.name="hyprial"',
                "-c",
                "model_providers.hyprial.base_url="
                f'"{openai_base_url(environment, HYPRIAL_PROVIDER)}"',
                "-c",
                'model_providers.hyprial.wire_api="chat"',
                "-c",
                f'model_providers.hyprial.env_key="{HYPRIAL_API_KEY_ENV}"',
                "-c",
                "model_providers.hyprial.requires_openai_auth=false",
            ),
            {HYPRIAL_API_KEY_ENV: require_hyprial_key(environment)},
        )
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


def pi_model_definitions(environment: Mapping[str, str]) -> dict[str, dict[str, object]]:
    """The DeepSeek provider definition pi needs to run ``DEEPSEEK_MODEL``.

    pi's built-in catalog only has ``deepseek-v4-*`` -- names this module
    retires -- and pi puts a model's ``id`` on the wire, so the canonical name
    must be defined, not aliased to a built-in.  A P2 agent's native root holds
    only its linked auth.json, so the controlled loader registers this
    definition (pi ``ModelRuntime.registerProvider``) on every P2 launch.
    Registering replaces pi's built-in DeepSeek list for that launch
    (``deepseek-v4-flash``, ``deepseek-v4-pro`` and
    ``deepseek-v4-flash-vision-exp``): the first two are retired here and
    hyprial never selects the third.
    Values follow pi's published catalog entry for this model.  ``cost`` is
    required: a registered provider gets no defaults, and pi prices every
    stream's usage chunk (a missing cost fails every turn).  The catalog's
    ``inputLimits`` is left out: the pinned pi does not read it from a
    registered provider.
    """

    return {
        DEEPSEEK_PROVIDER: {
            "api": "openai-completions",
            "baseUrl": openai_base_url(environment).rstrip("/"),
            "models": [
                {
                    "id": DEEPSEEK_MODEL,
                    "name": "DeepSeek V4.1 Flash",
                    "reasoning": True,
                    "thinkingLevelMap": {
                        "minimal": None,
                        "low": "low",
                        "medium": None,
                        "high": "high",
                        "max": "max",
                    },
                    "input": ["text", "image"],
                    "cost": {
                        "input": 0.3,
                        "output": 1.2,
                        "cacheRead": 0.006,
                        "cacheWrite": 0,
                    },
                    "contextWindow": 1_000_000,
                    "maxTokens": 384_000,
                    "compat": {
                        "supportsStore": False,
                        "supportsDeveloperRole": False,
                        "maxTokensField": "max_tokens",
                        "requiresReasoningContentOnAssistantMessages": True,
                        "thinkingFormat": "deepseek",
                        "supportsStrictMode": True,
                    },
                }
            ],
        }
    }


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
