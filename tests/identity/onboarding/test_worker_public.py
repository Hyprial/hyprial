"""The identity credential service's public protocol, in-process under the home lock.

The OIDC provider is the in-repo fake issuer on loopback.  This is a protocol
boundary test, not evidence of an external real login.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time

import pytest

from hyprial.identity.impl.identity_transaction import IdentityTransactionLock
from hyprial.identity.impl.onboarding.ports import (
    AuthProfile,
    CommitPermit,
    HttpTransport,
    IdentityBegin,
    IdentityPublicState,
    IdentitySnapshot,
)
from hyprial.identity.impl.onboarding.service import IdentityCredentialService
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _permit(
    home: Path, profile: AuthProfile, *, operation_id: str = "op-1"
) -> CommitPermit:
    return CommitPermit(
        operation_id=operation_id,
        nonce=f"nonce-{operation_id}",
        home=home,
        issuer=profile.issuer,
        client_id=profile.client_id,
        expected_identity=IdentitySnapshot(owner=None, mode=None, issuer=None),
        expires_at=int(time.time()) + 600,
        bootstrap=True,
    )


class _RecordingService(IdentityCredentialService):
    """Observe the public outputs without changing the service contract."""

    def __init__(self) -> None:
        super().__init__()
        self.outputs: list[dict[str, object]] = []

    def begin(
        self,
        *,
        profile: AuthProfile,
        home: Path,
        permit: CommitPermit | None,
        lock_fd: int | None = None,
        transport: HttpTransport | None = None,
    ) -> IdentityBegin:
        response = super().begin(
            profile=profile,
            home=home,
            permit=permit,
            lock_fd=lock_fd,
            transport=transport,
        )
        self.outputs.append({"op": "begin", "response": asdict(response)})
        return response

    def poll_public(self, operation_id: str) -> IdentityPublicState:
        response = super().poll_public(operation_id)
        self.outputs.append({"op": "poll-public", "response": asdict(response)})
        return response


def test_begin_poll_commit_exposes_only_public_identity_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=1)) as server:
        profile = AuthProfile(issuer=server.issuer, client_id="fake-client-id")
        lock = IdentityTransactionLock.acquire(home)
        service = _RecordingService()
        try:
            begin = service.begin(
                profile=profile,
                home=home,
                permit=_permit(home, profile),
                lock_fd=None,
            )
            assert begin.operation_id == "op-1"
            assert begin.user_code == "FAKE-CODE"
            assert begin.verification_uri.startswith(server.base_url)

            assert service.poll_public("op-1").state == "pending"
            committed = service.poll_public("op-1")
            assert committed.state == "committed"
            assert committed.owner == "alicesmith"
            assert committed.issuer == server.issuer
            assert committed.error_code is None

            credential = json.loads(
                (home / "secrets" / "login.json").read_text(encoding="utf-8")
            )
            assert credential["refreshToken"] == server.config.refresh_token
            assert "accessToken" not in credential
            settings = json.loads(
                (home / "settings.json").read_text(encoding="utf-8")
            )
            assert settings["owner"] == "alicesmith"
            assert settings["identityMode"] == "casdoor"
            assert settings["identityIssuer"] == server.issuer

            observed = json.dumps(service.outputs)
            for secret in (
                server.config.device_code,
                server.config.access_token,
                server.config.refresh_token,
                server.config.id_token,
            ):
                assert secret not in observed
            assert service.outputs[0]["op"] == "begin"
        finally:
            service.close()
            lock.close()
