"""Classification pins: A/B/C on verbatim production texts and C negatives.

The A and B anchors are the exact strings this feature was born from
(2026-09-14 incident); the C negatives are the nearby texts that a broadened
matcher would swallow — mutation ② widens the tables and these go red.
"""

from __future__ import annotations

from hyprial.identity.impl.provider_auth import ProviderFailureClass, classify_provider_failure

#: Verbatim from production worker logs / pi sources, 2026-09-14.
KIMI_REFRESH_DEAD = (
    "OAuth refresh failed for kimi-coding: Kimi Code token refresh "
    "unauthorized (status 400): The provided authorization grant is invalid"
)
CODEX_ENTITLEMENT = (
    "Codex error: The 'gpt-6-astra' model is not supported when using "
    "Codex with a ChatGPT account."
)


def test_kimi_refresh_grant_rejection_is_reloginable() -> None:
    assert (
        classify_provider_failure(KIMI_REFRESH_DEAD, provider="kimi-coding")
        is ProviderFailureClass.RELOGINABLE
    )


def test_codex_refresh_grant_rejection_is_reloginable() -> None:
    # pi-ai wraps every provider's refresh rejection the same way.
    text = "OAuth refresh failed for openai-codex: OpenAI Codex token refresh failed (401)"
    assert (
        classify_provider_failure(text, provider="openai-codex")
        is ProviderFailureClass.RELOGINABLE
    )


def test_invalid_grant_spelling_is_reloginable() -> None:
    text = 'token endpoint returned 400: {"error": "invalid_grant"}'
    assert (
        classify_provider_failure(text, provider="kimi-coding")
        is ProviderFailureClass.RELOGINABLE
    )


def test_reloginable_marker_on_non_device_provider_is_account_class() -> None:
    # The marker says "relogin fixes it", but this provider has no device
    # flow we can start — the honest outcome is the B path (alert a human).
    assert (
        classify_provider_failure(KIMI_REFRESH_DEAD, provider="deepseek")
        is ProviderFailureClass.ACCOUNT
    )


def test_entitlement_text_is_account_class() -> None:
    # Not in the terminal-auth table at all — the gate that would drop it.
    assert (
        classify_provider_failure(CODEX_ENTITLEMENT, provider="openai-codex")
        is ProviderFailureClass.ACCOUNT
    )
    assert (
        classify_provider_failure(CODEX_ENTITLEMENT, provider=None)
        is ProviderFailureClass.ACCOUNT
    )


def test_permission_denied_is_account_class() -> None:
    assert (
        classify_provider_failure("403 permission denied", provider="kimi-coding")
        is ProviderFailureClass.ACCOUNT
    )


def test_local_errno_permission_denied_is_other() -> None:
    # L1: a crashed worker's stderr carries the OS errno spelling, not a
    # provider rejection -- this must never become an account (B) alert.
    text = (
        "pi RPC process exited with status 1: EACCES: permission denied, "
        "open '/tmp/x'"
    )
    assert (
        classify_provider_failure(text, provider="kimi-coding")
        is ProviderFailureClass.OTHER
    )


def test_claude_sdk_auth_403_is_account_class() -> None:
    # 追加 2 样本(2026-09-14):claude harness, provider=None。claude 的
    # 重登录是浏览器流程,没有可非交互发起的设备码 ⇒ 只能走 B(告警)。
    text = "Failed to authenticate. API Error: 403 Request not allowed"
    assert (
        classify_provider_failure(text, provider=None)
        is ProviderFailureClass.ACCOUNT
    )


def test_weekly_quota_is_other() -> None:
    # 追加 2 额度样本:另一类,不触发登录也不告警。
    text = "You've hit your weekly limit · resets 6am (Asia/Shanghai)"
    assert (
        classify_provider_failure(text, provider=None)
        is ProviderFailureClass.OTHER
    )


def test_transient_network_is_other() -> None:
    for text in (
        "fetch failed",
        "ConnectionError",
        "getaddrinfo ENOTFOUND api.example.com",
        "socket hang up",
    ):
        assert (
            classify_provider_failure(text, provider="kimi-coding")
            is ProviderFailureClass.OTHER
        ), text


def test_abort_and_broken_install_are_other() -> None:
    for text in (
        "This operation was aborted",
        "Cannot find module '/opt/.../openai-completions-XYZ.js' imported from ...",
    ):
        assert (
            classify_provider_failure(text, provider="openai-codex")
            is ProviderFailureClass.OTHER
        ), text


def test_missing_api_key_is_other() -> None:
    # No OAuth grant involved; an api-key setup gap is setup, not this
    # feature's relogin flow.
    assert (
        classify_provider_failure(
            "No API key found for deepseek.", provider="deepseek"
        )
        is ProviderFailureClass.OTHER
    )


def test_usage_limit_is_other() -> None:
    assert (
        classify_provider_failure(
            "429 too many requests: usage limit reached", provider="kimi-coding"
        )
        is ProviderFailureClass.OTHER
    )


def test_redacted_log_line_would_not_match() -> None:
    # The log pipeline rewrites "token refresh" → "token [REDACTED]"; this
    # pins that classification must happen on the RAW string: the redacted
    # shape still matches via the outer wrapper, but a marker set built from
    # logged lines (e.g. relying on "token refresh unauthorized") would miss.
    redacted = (
        "OAuth refresh failed for kimi-coding: Kimi Code token [REDACTED] "
        "unauthorized (status 400): The provided authorization grant is invalid"
    )
    assert (
        classify_provider_failure(redacted, provider="kimi-coding")
        is ProviderFailureClass.RELOGINABLE  # outer wrapper still matches
    )
