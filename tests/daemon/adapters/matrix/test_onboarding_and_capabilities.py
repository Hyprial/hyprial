from __future__ import annotations

import asyncio

import pytest

from hyprial.daemon.impl.adapters.contracts import OnboardingAttempt
from hyprial.daemon.impl.adapters.matrix import (
    MatrixCredentials,
    MatrixOnboarding,
    normalize_homeserver,
)

from .support import Credentials, Crypto, Transport, instance


def test_homeserver_and_receiver_owned_credentials_are_strict_and_redacted() -> None:
    assert normalize_homeserver("https://matrix.example/") == "https://matrix.example"
    for invalid in (
        "http://matrix.example",
        "https://user:password@matrix.example",
        "https://matrix.example/?access_token=secret",
    ):
        with pytest.raises(ValueError):
            normalize_homeserver(invalid)
    credentials = MatrixCredentials(
        "matrix-main",
        "org-a",
        3,
        "https://matrix.example",
        "@bridge:example",
        "DEVICE",
        "receiver-secret-token",
        None,
    )
    assert "receiver-secret-token" not in repr(credentials)


def test_login_identity_fingerprint_and_e2e_capability_are_explicit() -> None:
    async def scenario() -> None:
        transport = Transport([])
        onboarding = MatrixOnboarding(
            transport,
            Credentials(),
            organization_id="org-a",
            instance_id="matrix-main",
            local_name="main",
            credential_ref="receiver:matrix",
            crypto=Crypto(),
            now_ms=lambda: 1_000,
        )
        assert (await onboarding.inspect({})).state == "ready"
        identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-1", {}, "receiver:matrix")
        )
        assert identity.account_scope == "https://matrix.example|@bridge:example"
        assert await onboarding.fingerprint(identity) == instance(generation=1)
        snapshot = await onboarding.probe_capabilities(identity)
        facts = {fact.name: fact for fact in snapshot.facts}
        assert facts["encrypted-messages"].support == "degraded"
        assert facts["encrypted-messages"].reason_code == "experimental-upstream-parity"
        assert facts["encrypted-attachments"].support == "unsupported"
        assert facts["media"].support == "degraded"
        assert facts["reactions"].support == "unsupported"
        assert facts["typing"].support == "unsupported"
        assert facts["threads"].support == "native"
        assert tuple(fact.name for fact in snapshot.facts) == tuple(
            sorted(facts, key=lambda value: value.encode("utf-8"))
        )

    asyncio.run(scenario())


def test_missing_e2e_dependency_is_a_typed_blocked_capability_not_plaintext_fallback() -> None:
    async def scenario() -> None:
        onboarding = MatrixOnboarding(
            Transport([]),
            Credentials(),
            organization_id="org-a",
            instance_id="matrix-main",
            local_name="main",
            credential_ref="receiver:matrix",
            crypto=None,
            now_ms=lambda: 1_000,
        )
        identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-1", {}, "receiver:matrix")
        )
        facts = {fact.name: fact for fact in (await onboarding.probe_capabilities(identity)).facts}
        assert facts["encrypted-messages"].support == "unsupported"
        assert facts["encrypted-messages"].permission == "unknown"
        assert facts["encrypted-messages"].reason_code == "matrix-e2e-dependency-unavailable"

    asyncio.run(scenario())


def test_arbitrary_crypto_descriptor_never_grants_encrypted_messages() -> None:
    async def scenario() -> None:
        crypto = Crypto(
            descriptor={
                "matrix_nio": {
                    "package": "lookalike",
                    "version": "99",
                    "sha256": "0" * 64,
                    "license": "unknown",
                },
                "vodozemac": {
                    "package": "lookalike",
                    "version": "99",
                    "sha256": "0" * 64,
                    "license": "unknown",
                },
            }
        )
        onboarding = MatrixOnboarding(
            Transport([]),
            Credentials(),
            organization_id="org-a",
            instance_id="matrix-main",
            local_name="main",
            credential_ref="receiver:matrix",
            crypto=crypto,
            now_ms=lambda: 1_000,
        )
        identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-1", {}, "receiver:matrix")
        )
        encrypted = {
            fact.name: fact
            for fact in (await onboarding.probe_capabilities(identity)).facts
        }["encrypted-messages"]
        assert encrypted.support == "unsupported"
        assert encrypted.permission == "unknown"
        assert encrypted.reason_code == "matrix-e2e-dependency-unreviewed"

    asyncio.run(scenario())
