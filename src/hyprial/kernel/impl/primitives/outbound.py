"""What this product calls itself when it makes an outbound HTTP request.

Some of the endpoints Hyprial talks to sit behind an edge that refuses
urllib's stock ``Python-urllib/<version>`` agent.  Measured 2026-10-20 against
the default profile's issuer (Cloudflare): the token grant answered ``403``
with ``error_code 1010`` / ``browser_signature_banned`` for the stock agent and
``200`` for ``hyprial-daemon/<version>``, ``curl/<version>`` and a browser
agent alike.  ``org create`` therefore failed on every node whose
``settings.json`` named an ``orgSync.casdoorCredentialFile``.

The same edge fronts the identity issuer, so the rule cannot live in one
transport: every call site names an agent built by :func:`user_agent` and merges
it with :func:`with_user_agent`, which leaves a caller-supplied agent alone --
HTTP header names are case-insensitive, so ``user-agent`` counts as supplied.
"""

from __future__ import annotations

from collections.abc import Mapping

from hyprial import __version__

__all__ = ["USER_AGENT_PREFIX", "user_agent", "with_user_agent"]

USER_AGENT_PREFIX = "hyprial"
"""Every agent this product sends starts with ``hyprial-``."""


def user_agent(component: str) -> str:
    """``hyprial-<component>/<version>`` -- the agent one component sends."""

    if not component:
        raise ValueError("a user agent needs a component name")
    return f"{USER_AGENT_PREFIX}-{component}/{__version__}"


def with_user_agent(headers: Mapping[str, str] | None, agent: str) -> dict[str, str]:
    """A copy of ``headers`` that carries ``agent`` unless one is already named.

    The check is case-insensitive: ``User-Agent``, ``user-agent`` and
    ``USER-AGENT`` are the same header to every receiver, so a caller that
    names any of them keeps its value.
    """

    merged = dict(headers or {})
    if not any(name.lower() == "user-agent" for name in merged):
        merged["User-Agent"] = agent
    return merged
