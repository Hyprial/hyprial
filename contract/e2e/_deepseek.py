"""Secret-safe DeepSeek setup shared by real-harness E2E runners."""

from __future__ import annotations

import json
import os
from pathlib import Path

PROVIDER = "deepseek"
MODEL = "deepseek-flash"
API_KEY_ENV = "DEEPSEEK_API_KEY"

# DeepSeek's official Claude Code integration does not name DeepSeek models in
# CC's model selection; it maps the Anthropic model families through ANTHROPIC_*
# environment variables, and the endpoint maps claude-opus* to pro and
# claude-sonnet*/haiku* to flash
# (https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code).
# The split matters because CC (>= 2.1.2xx) fatal-exits at startup when the
# *selected* model is absent from its model catalog — the raw "deepseek-*"
# ids are, so the interactive-CC scenarios select the catalog-known "haiku"
# alias and let ANTHROPIC_DEFAULT_HAIKU_MODEL carry the DeepSeek id at
# request time.
#
# There used to be a second id here (a "pro" tier) riding OPUS/SONNET/MODEL
# per the vendor's table. DeepSeek now publishes one name, so all four
# variables carry it; a constant still called *_PRO_MODEL would name a
# distinction that no longer exists, which is worse than no constant.
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
CC_WORKER_MODEL = "haiku"


class DeepSeekUnavailable(RuntimeError):
    """No usable DeepSeek credential is available to an E2E runner."""


def _pi_auth_candidates() -> tuple[Path, ...]:
    # PI_CODING_AGENT_DIR is pi's own root (the controlled CI job points it
    # at ~/.pi-ci/agent).  When it is set it is the ONLY candidate: an
    # isolated run must fail loudly rather than quietly read the
    # developer's real ~/.pi credential.
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if configured:
        return (Path(configured).expanduser() / "auth.json",)
    home = Path.home()
    return (
        home / ".pi" / "agent" / "auth.json",
        home / "my-pi-setup" / "agent" / "auth.json",
    )


def deepseek_api_key() -> str:
    """Resolve the key without printing it or writing a credential file."""

    configured = os.environ.get(API_KEY_ENV, "").strip()
    if configured:
        return configured
    for path in _pi_auth_candidates():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry = value.get(PROVIDER) if isinstance(value, dict) else None
        key = entry.get("key") if isinstance(entry, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    raise DeepSeekUnavailable(
        "DeepSeek credential unavailable: export DEEPSEEK_API_KEY or configure "
        "the deepseek entry in Pi's auth.json"
    )


def deepseek_environment() -> dict[str, str]:
    """Environment delta for an isolated daemon or direct harness process."""

    return {API_KEY_ENV: deepseek_api_key()}


def claude_code_environment() -> dict[str, str]:
    """DeepSeek's official Claude Code environment mapping, verbatim.

    Per DeepSeek's CC integration guide, a CC process is pointed at the
    Anthropic-compatible endpoint with the model families mapped pro/flash
    through these seven variables — never by naming a DeepSeek model in
    CC's own selection (see CC_WORKER_MODEL for why).

    The key is resolved by :func:`deepseek_api_key` and exists only in the
    returned mapping: runners put it on a launched process's environment
    line and must never print the mapping or write it anywhere.
    """

    key = deepseek_api_key()
    return {
        "ANTHROPIC_BASE_URL": DEEPSEEK_ANTHROPIC_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
        "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
    }


def selection_args() -> tuple[str, ...]:
    """Public hyprial agent start arguments selecting the shared E2E model."""

    return ("--provider", PROVIDER, "--model", MODEL)


def local_provider_environment(
    *, openai_base_url: str, anthropic_base_url: str
) -> dict[str, str]:
    """Point the harnesses at a local endpoint instead of the vendor's.

    Uses hyprial's own published override -- the same one a self-hosted
    deployment would set -- deliberately not a test-only injection path,
    because a route only tests take is a route production never proves.

    A key is still supplied: the harnesses refuse to start without one, and
    that refusal is itself under test in some scenarios.  It reaches nothing
    real, the endpoint being local.

    ⚠️ Covers claude and codex, whose endpoint hyprial supplies.  pi reads its
    endpoint from its own configuration, so a pi scenario redirects through
    pi's documented ``registerProvider`` override instead -- confirmed
    working against a local server, but a separate mechanism, not this one.
    """

    return {
        API_KEY_ENV: "e2e-local-provider-not-a-real-key",
        "HYPRIAL_OPENAI_BASE_URL": openai_base_url,
        "HYPRIAL_ANTHROPIC_BASE_URL": anthropic_base_url,
    }


if __name__ == "__main__":
    deepseek_api_key()
    print(f"DeepSeek credential ready for {PROVIDER}/{MODEL}")
