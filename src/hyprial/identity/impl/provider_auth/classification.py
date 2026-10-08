"""Provider-auth failure classification: reloginable, account-side, or other.

The closed marker tables below are the whole classifier — deliberately NOT a
regex over free-form error text (card 260's constraint, carried into this
spec).  Every marker is a verbatim substring of text pi itself produces; the
anchors are cited per marker so a future drift in pi's wording is a diffable
event, not a silent reclassification.

Three classes (spec 2026-09-14 §内容 2):

A  RELoginABLE   The provider rejected the OAuth *refresh grant*.  A fresh
                 device-code login fixes it.  Anchors: pi-ai wraps refresh
                 rejections as ``OAuth refresh failed for <providerId>``
                 (pi bundle chunk-IDDQWTHI.js); kimi's endpoint says
                 ``token refresh unauthorized (status 400): The provided
                 authorization grant is invalid``.
B  ACCOUNT       Terminal auth/permission text that a relogin cannot fix:
                 account entitlement (``... is not supported when using Codex
                 with a ChatGPT account.``), permission denied, invalid api
                 key.  Alert a human; do not start a login.
C  OTHER         Everything else: transient network, aborts, broken local
                 installs, usage limits.  Not this feature's scope — no alert,
                 no mark.

Ordering is the contract: A's reloginable markers run first; then B.  B is
EITHER a reloginable marker on a provider with no device flow we can start
(the honest fallback: alert a human), OR an explicit account/permission
marker.  The broad ``provider_failure_is_terminal`` terminal-auth table is
**not** reused for B (review round 2 L1): its bare "permission denied" /
"oauth" / "unauthorized" spellings also match a crashed worker's local
stderr ("EACCES: permission denied"), which is infrastructure, not the
account.  A marker must never be broadened into a generic "error"/"failed"
substring — that is the mutation the tests pin.

⚠️ Classify the RAW failure string at the streaming layer, never the logged
line: the log pipeline rewrites ``token refresh`` → ``token [REDACTED]`` and
URLs → ``[REDACTED_URL]`` (``hyprial.log``), so a log watcher sees a text the
markers were never written against.
"""

from __future__ import annotations

from enum import StrEnum


class ProviderFailureClass(StrEnum):
    RELOGINABLE = "reloginable"  # A: device-code relogin fixes it
    ACCOUNT = "account"  # B: account/permission, relogin cannot fix
    OTHER = "other"  # C: out of scope, never alerted


#: Providers whose pi OAuth login supports a non-interactive device-code flow
#: (verified against the installed pi: kimi-coding is device-only; openai-codex
#: offers device_code via its select prompt).  A provider outside this set can
#: never be class A from this daemon — the fallback for it is the B path.
DEVICE_CODE_PROVIDERS: frozenset[str] = frozenset({"kimi-coding", "openai-codex"})

#: A-class markers: each names a rejected refresh grant.  All are verbatim
#: substrings of pi/pi-ai error text; matching is case-insensitive
#: containment.
_RELOGINABLE_MARKERS: tuple[str, ...] = (
    # pi-ai ModelsError wrapper: `OAuth refresh failed for ${providerId}`.
    "oauth refresh failed for",
    # kimi token endpoint: "The provided authorization grant is invalid".
    "the provided authorization grant is invalid",
    # RFC 6749 §5.2 refresh-grant rejection, any provider spelling it out.
    "invalid_grant",
    # kimi refresh failure prefix: "Kimi Code token refresh unauthorized".
    "token refresh unauthorized",
)


#: B-class account markers: account/permission rejections a relogin cannot
#: fix.  Verbatim server text, case-insensitive containment.  Deliberately
#: NOT the broad terminal-auth table (L1): a bare "permission denied" is
#: gated below against local-errno spellings so a crashed worker is not
#: mistaken for an account problem.
_ACCOUNT_MARKERS: tuple[str, ...] = (
    # OpenAI entitlement rejection, observed verbatim 2026-09-14:
    # "Codex error: The '<model>' model is not supported when using Codex
    # with a ChatGPT account."
    "is not supported when using codex with a chatgpt account",
    # claude-sdk auth rejection, observed verbatim 2026-09-14 (追加 2):
    # "Failed to authenticate. API Error: 403 Request not allowed".
    # Claude's own relogin is browser-based with no non-interactive device
    # flow we can start, so this can only ever be the B path: alert.
    "failed to authenticate",
    # Key-based account rejection (any provider).
    "invalid api key",
    "invalid_api_key",
    # Generic auth rejection, provider-shaped enough to be B.
    "authentication failed",
    # Provider permission rejection ("403 permission denied") — gated below
    # against the local-errno spelling.
    "permission denied",
)


#: Local OS errno words that mark a crashed worker's own stderr, not a
#: provider rejection (L1): "EACCES: permission denied" is infrastructure.
_LOCAL_ERRNO_MARKERS: tuple[str, ...] = (
    "eacces",
    "eperm",
    "enoent",
    "enospc",
    "emfile",
    "enfile",
    "enodev",
    "enotdir",
    "eisdir",
    "eagain",
)


def classify_provider_failure(
    failure: str, *, provider: str | None
) -> ProviderFailureClass:
    """Classify one provider failure text into A (reloginable) / B (account)
    / C (other).

    ``provider`` is the worker's configured model provider
    (``HarnessLaunchSpec.model_provider``); class A additionally requires the
    provider to support a device-code relogin, because starting a login the
    provider cannot complete would be worse than the B-class alert.
    """

    detail = failure.casefold()
    reloginable = any(marker in detail for marker in _RELOGINABLE_MARKERS)
    if provider in DEVICE_CODE_PROVIDERS and reloginable:
        return ProviderFailureClass.RELOGINABLE
    if reloginable:
        # The grant is dead but this provider has no device flow we can
        # start — the honest outcome is the B path: alert a human.
        return ProviderFailureClass.ACCOUNT
    account = any(marker in detail for marker in _ACCOUNT_MARKERS)
    if account:
        # "permission denied" is an account judgment only in a provider
        # shape; the local-errno spelling ("EACCES: permission denied") is a
        # crashed worker, not the account (L1).
        if "permission denied" in detail and any(
            marker in detail for marker in _LOCAL_ERRNO_MARKERS
        ):
            return ProviderFailureClass.OTHER
        return ProviderFailureClass.ACCOUNT
    return ProviderFailureClass.OTHER
