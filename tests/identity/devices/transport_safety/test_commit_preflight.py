from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from hyprial.identity import (
    TransportCredentialError,
    commit_transport_credential,
    read_transport_credential,
    stage_transport_credential,
)
from tests.identity.devices.test_transport_credential import _response, _stage, _validate
from tests.identity.devices.transport_safety.inventory import inventory


def _pending_renewal(home: Path):
    first = _stage(home)
    response, bundle = _response(first)
    previous = _validate(first, response, bundle)
    commit_transport_credential(
        home, staged=first, credential=previous, pinned_trust_bundle_pem=bundle
    )
    assert read_transport_credential(home) == previous
    staged = stage_transport_credential(
        home,
        operation_id=str(uuid4()),
        owner=first.owner,
        device_uid=first.device_uid,
        expected_credential_generation=previous.credential_generation,
    )
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    return staged, credential, bundle, previous


@pytest.mark.parametrize(
    "target",
    ["bundle-temp", "credential-temp", "key", "credential", "bundle"],
)
@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink", "directory", "fifo"])
def test_commit_rejects_unsafe_paths_without_mutation(
    tmp_path: Path, target: str, unsafe: str
) -> None:
    home = tmp_path / "home"
    staged, credential, bundle, previous = _pending_renewal(home)
    state = home / "state/transport"
    paths = {
        "bundle-temp": (state / f".trust-bundle.{staged.operation_id}.tmp", 0o644),
        "credential-temp": (state / f".credential.{staged.operation_id}.tmp", 0o600),
        "key": (home / "secrets/transport/device-key.pem", 0o600),
        "credential": (state / "credential.json", 0o600),
        "bundle": (state / "trust-bundle.pem", 0o644),
    }
    # Safe leftovers must survive too when any other path is already unsafe.
    for name in ("bundle-temp", "credential-temp"):
        path, mode = paths[name]
        path.write_bytes(b"recoverable synthetic leftover")
        path.chmod(mode)
    path, mode = paths[target]
    if unsafe == "mode":
        path.chmod(0o600 if mode == 0o644 else 0o644)
    else:
        path.unlink()
        if unsafe in {"symlink", "hardlink"}:
            external = tmp_path / "external"
            external.write_bytes(b"untouched synthetic external content")
            external.chmod(mode)
            if unsafe == "symlink":
                path.symlink_to(external)
            else:
                os.link(external, path)
        elif unsafe == "directory":
            path.mkdir(mode=0o700)
        else:
            os.mkfifo(path, mode)
    before = inventory(tmp_path)
    staged_bytes = staged._key_path.read_bytes()
    with pytest.raises(TransportCredentialError) as caught:
        commit_transport_credential(
            home,
            staged=staged,
            credential=credential,
            pinned_trust_bundle_pem=bundle,
        )

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert inventory(tmp_path) == before, "unsafe preflight changed filesystem inventory"
    assert staged._key_path.read_bytes() == staged_bytes
    if target.endswith("-temp"):
        assert read_transport_credential(home) == previous


def test_commit_recovers_safe_temporary_files(tmp_path: Path) -> None:
    staged, credential, bundle, _previous = _pending_renewal(tmp_path)
    state = tmp_path / "state/transport"
    for name, mode in (("credential", 0o600), ("trust-bundle", 0o644)):
        path = state / f".{name}.{staged.operation_id}.tmp"
        path.write_bytes(b"recoverable synthetic leftover")
        path.chmod(mode)

    commit_transport_credential(
        tmp_path, staged=staged, credential=credential, pinned_trust_bundle_pem=bundle
    )

    assert read_transport_credential(tmp_path) == credential
    assert not list(state.glob(".*.tmp"))
