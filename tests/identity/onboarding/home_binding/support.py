"""Shared fixtures for home account binding tests (loopback fake OIDC only)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hyprial.identity import (
    AuthProfile,
    CommitPermit,
    HomeBinding,
    IdentityCredentialService,
    IdentityPublicState,
    IdentityTransactionLock,
    legacy_business_data_present,
    normalize_issuer,
    read_home_binding,
    read_identity_snapshot,
)
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

PRIMARY_ISSUER = "https://issuer.example.test"
OTHER_ISSUER = "https://other-issuer.example.test"
PRIMARY_UID = "ou_0123456789abcdef0123456789abcdef"
OTHER_UID = "ou_fedcba9876543210fedcba9876543210"
PRIMARY_OWNER = "alice-original"
RENAMED_OWNER = "alice-renamed"
CLIENT_ID = "fake-client-id"
PERMIT_LEDGER_NAME = ".identity-permit-uses.json"


def create_home(tmp_path: Path, name: str = "home") -> Path:
    """Create an isolated private home directory."""

    home = tmp_path / name
    home.mkdir(mode=0o700, parents=True)
    return home


def write_settings_json(home: Path, payload: dict[str, object]) -> Path:
    """Write ``settings.json`` in ``home`` with deterministic formatting."""

    path = home / "settings.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    """Read a JSON object file for assertions."""

    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_bytes(paths: list[Path]) -> dict[Path, bytes | None]:
    """Capture exact file contents (or None when absent) before an operation."""

    return {path: path.read_bytes() if path.exists() else None for path in paths}


def populate_legacy_item(home: Path, relpath: str, payload: bytes = b"legacy-data") -> Path:
    """Materialize a single legacy file under ``home`` for gate verification."""

    target = home / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def issue_permit(
    home: Path,
    profile: AuthProfile,
    *,
    operation_id: str = "op-home-binding",
    bootstrap: bool = True,
) -> CommitPermit:
    """Build a commit permit populated with daemon-computed binding metadata."""

    return CommitPermit(
        operation_id=operation_id,
        nonce=f"nonce-{operation_id}",
        home=home,
        issuer=profile.issuer,
        client_id=profile.client_id,
        expected_identity=read_identity_snapshot(home),
        expires_at=int(time.time()) + 600,
        bootstrap=bootstrap,
        home_binding=read_home_binding(home),
        legacy_business_data=legacy_business_data_present(home),
    )


def run_oidc_against_issuer(
    home: Path,
    server_issuer: str,
    *,
    operation_id: str = "op-home-binding",
    bootstrap: bool = True,
    with_trailing_slash: bool = False,
) -> tuple[IdentityPublicState, str]:
    """Drive one in-process ``IdentityCredentialService`` login against an active fake OIDC."""

    profile_issuer = (
        server_issuer.rstrip("/") + "/"
        if with_trailing_slash
        else server_issuer.rstrip("/")
    )
    profile = AuthProfile(issuer=profile_issuer, client_id=CLIENT_ID)
    permit = issue_permit(
        home,
        profile,
        operation_id=operation_id,
        bootstrap=bootstrap,
    )
    service = IdentityCredentialService()
    try:
        with IdentityTransactionLock.acquire(home):
            service.begin(
                profile=profile,
                home=home,
                permit=permit,
                lock_fd=None,
            )
            return service.poll_public(operation_id), normalize_issuer(profile_issuer)
    finally:
        service.close()


def run_oidc_login(
    home: Path,
    *,
    sub: str,
    username: str,
    operation_id: str = "op-home-binding",
    bootstrap: bool = True,
    with_trailing_slash: bool = False,
) -> tuple[IdentityPublicState, str]:
    """Spin up a loopback fake OIDC server and complete one in-process login."""

    config = FakeOidcConfig(
        pending_polls=0,
        sub=sub,
        preferred_username=username,
    )
    with fake_oidc(config) as server:
        return run_oidc_against_issuer(
            home,
            server.issuer,
            operation_id=operation_id,
            bootstrap=bootstrap,
            with_trailing_slash=with_trailing_slash,
        )


def expected_home_account(issuer: str, user_id: str, owner: str) -> dict[str, str]:
    """Build the normalized ``settings.homeAccount`` dictionary."""

    return HomeBinding(
        issuer=normalize_issuer(issuer),
        user_id=user_id,
        owner=owner,
    ).as_dict()
