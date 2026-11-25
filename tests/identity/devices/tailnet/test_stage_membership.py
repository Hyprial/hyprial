from dataclasses import replace

import pytest

from hyprial.identity import TailnetCredentialError
from .support import Context, inventory, stage


@pytest.mark.parametrize(
    "case",
    [
        "tailnet",
        "owner",
        "device",
        "unknown",
        "tombstone",
        "expired",
        "restart",
        "pin-tailnet",
        "pin-owner",
        "pin-id",
        "config-pin",
        "lookup-unavailable",
        "association-present",
    ],
)
def test_stage_gate_has_no_transport_files(tmp_path, case):
    c = Context(tmp_path)
    s = c.snapshot
    if case == "tailnet":
        c.snapshot = replace(s, tailnet_id="other")
    if case == "owner":
        c.snapshot = replace(s, local_member=replace(s.local_member, owner="other"))
    if case == "device":
        c.snapshot = replace(s, local_member=replace(s.local_member, device_id="other"))
    if case == "unknown":
        c.snapshot = replace(s, local_member=None)
    if case == "tombstone":
        c.snapshot = replace(s, local_member=replace(s.local_member, state="tombstone"))
    if case == "expired":
        c.snapshot = replace(s, lease_remaining_seconds=0)
    if case == "restart":
        c.snapshot = replace(s, member_ready=False, lease_remaining_seconds=0)
    if case == "pin-tailnet":
        c.snapshot = replace(
            s, authority_pin=replace(s.authority_pin, tailnet_id="other")
        )
    if case == "pin-owner":
        c.snapshot = replace(s, authority_pin=replace(s.authority_pin, owner=""))
    if case == "pin-id":
        c.snapshot = replace(s, authority_device_id="other")
    if case == "config-pin":
        c.binding = replace(
            c.binding, authority_pin=replace(s.authority_pin, master_public="other")
        )
    if case == "lookup-unavailable":
        c.lookup_error = True
    if case == "association-present":
        c.current = object()
    before = inventory(tmp_path)
    caught = None
    try:
        stage(c.workflow())
    except TailnetCredentialError as error:
        caught = error
    after = inventory(tmp_path)
    assert after == before, "unsafe stage created transport files"
    assert caught is not None


def test_authority_local_active_needs_no_self_lease(tmp_path):
    c = Context(tmp_path)
    pin = replace(
        c.snapshot.authority_pin, owner=c.record.owner, device_id=c.record.device_id
    )
    c.snapshot = replace(
        c.snapshot,
        role="authority",
        authority_device_id=c.record.device_id,
        authority_pin=pin,
        lease_remaining_seconds=None,
    )
    c.binding = replace(c.binding, authority_pin=pin)
    stage(c.workflow())
    assert (tmp_path / "state/transport/enrollment.json").exists()


def test_missing_context_fails_closed_without_files(tmp_path):
    c = Context(tmp_path)
    c.lookup_error = True
    before = inventory(tmp_path)
    with pytest.raises(TailnetCredentialError):
        stage(c.workflow())
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("role", ["member", "authority"])
def test_exact_sidecar_healthy_null_projection(tmp_path, role):
    c = Context(tmp_path)
    pin = c.snapshot.authority_pin
    if role == "authority":
        pin = replace(pin, owner=c.record.owner, device_id=c.record.device_id)
    c.snapshot = replace(
        c.snapshot,
        role=role,
        code=None,
        authority_device_id=pin.device_id,
        authority_pin=pin,
        lease_remaining_seconds=None if role == "authority" else 250,
    )
    c.binding = replace(c.binding, authority_pin=pin)
    stage(c.workflow())
    assert (tmp_path / "state/transport/enrollment.json").exists()


def test_adapter_exception_diagnostics_detach_secret_context(tmp_path):
    c = Context(tmp_path)

    def unavailable(_tailnet):
        raise RuntimeError("-----BEGIN synthetic secret private path")

    c.membership = unavailable
    with pytest.raises(TailnetCredentialError) as caught:
        stage(c.workflow())
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "BEGIN" not in repr(caught.value)


@pytest.mark.parametrize("case", ["config-type", "issuer-type", "bundle-type"])
def test_invalid_explicit_binding_rejected_before_staging(tmp_path, case):
    c = Context(tmp_path)
    if case == "config-type":
        c.binding = replace(c.binding, config=object())
    elif case == "issuer-type":
        c.binding = replace(c.binding, policy_issuer=True)
    else:
        c.binding = replace(c.binding, trust_bundle_id=True)
    before = inventory(tmp_path)
    caught = None
    try:
        stage(c.workflow())
    except Exception as error:
        caught = error
    assert inventory(tmp_path) == before, "malformed explicit binding wrote staged key"
    assert isinstance(caught, TailnetCredentialError)


@pytest.mark.parametrize(
    "case",
    [
        "authority-zero",
        "authority-float",
        "member-float",
        "member-bool",
        "revision-zero",
        "revision-big",
    ],
)
def test_exact_f2_lease_and_revision_types_reject_before_files(tmp_path, case):
    c = Context(tmp_path)
    if case.startswith("authority"):
        pin = replace(
            c.snapshot.authority_pin, owner=c.record.owner, device_id=c.record.device_id
        )
        c.snapshot = replace(
            c.snapshot,
            role="authority",
            authority_pin=pin,
            authority_device_id=pin.device_id,
            lease_remaining_seconds=0 if case == "authority-zero" else 1.5,
        )
        c.binding = replace(c.binding, authority_pin=pin)
    if case == "member-float":
        c.snapshot = replace(c.snapshot, lease_remaining_seconds=250.0)
    if case == "member-bool":
        c.snapshot = replace(c.snapshot, lease_remaining_seconds=True)
    if case == "revision-zero":
        c.snapshot = replace(c.snapshot, revision=0)
    if case == "revision-big":
        c.snapshot = replace(c.snapshot, revision=2**53)
    before = inventory(tmp_path)
    caught = None
    try:
        stage(c.workflow())
    except TailnetCredentialError as error:
        caught = error
    assert inventory(tmp_path) == before, (
        "invalid f2 lease/revision wrote actual staged key"
    )
    assert caught is not None


def test_authority_addresses_are_excluded_from_projection_repr(tmp_path):
    c = Context(tmp_path)
    pin = c.snapshot.authority_pin
    assert pin.address not in repr(pin)
    assert pin.address not in repr(c.snapshot)
    assert pin.address not in repr(c.binding)
    from tests.identity.devices.client.support import certificate_data

    c.accept(certificate_data())
    assert pin.address not in repr(c.current)


@pytest.mark.parametrize(
    "target,unsafe",
    [
        ("device", "symlink"),
        ("identity", "symlink"),
        ("parent", "symlink"),
        ("device", "hardlink"),
        ("identity", "hardlink"),
    ],
)
def test_unsafe_device_identity_is_rejected_before_any_content_read(
    tmp_path, monkeypatch, target, unsafe
):
    import os
    from pathlib import Path
    import shutil

    home = tmp_path / "home"
    c = Context(home)
    directory = home / "state/tailcat"
    device = directory / "device.json"
    identity = directory / "device-identity.json"
    if target == "parent":
        external = tmp_path / "outside-tailcat"
        shutil.copytree(directory, external)
        shutil.rmtree(directory)
        directory.symlink_to(external, target_is_directory=True)
        unsafe_paths = {device, identity}
    else:
        path = device if target == "device" else identity
        external = tmp_path / "outside-record.json"
        external.write_bytes(path.read_bytes())
        external.chmod(0o600)
        path.unlink()
        if unsafe == "symlink":
            path.symlink_to(external)
        else:
            os.link(external, path)
        unsafe_paths = {path}
    unsafe_reads = 0
    original_read = Path.read_bytes

    def trace_read(path):
        nonlocal unsafe_reads
        result = original_read(path)
        if path in unsafe_paths:
            unsafe_reads += 1
        return result

    monkeypatch.setattr(Path, "read_bytes", trace_read)
    caught = None
    try:
        stage(c.workflow())
    except Exception as error:
        caught = error
    assert unsafe_reads == 0, "unsafe device content was read before rejection"
    assert isinstance(caught, TailnetCredentialError)
    assert not (home / "secrets/transport").exists()


def test_public_face_declares_all_tailnet_workflow_exports():
    import hyprial.identity as identity

    names = {
        "TailnetAuthorityBinding",
        "TailnetAuthorityPin",
        "TailnetCredentialAssociation",
        "TailnetCredentialContext",
        "TailnetCredentialError",
        "TailnetMember",
        "TailnetMembership",
        "TailnetCredentialOperation",
        "TailnetCredentialWorkflow",
    }
    assert names <= set(identity.__all__)
    for name in names:
        assert getattr(identity, name).__name__ == name


def test_stage_uuid_failure_detaches_raw_credential_exception_chain(tmp_path):
    from hyprial.identity import TransportCredentialError

    c = Context(tmp_path)
    with pytest.raises(TransportCredentialError) as caught:
        c.workflow().stage("not-a-uuid", 0)
    error = caught.value
    has_context = error.__context__ is not None
    has_cause = error.__cause__ is not None
    assert error.code == "TRANSPORT_RESPONSE_INVALID"
    assert has_context is False
    assert has_cause is False
    assert not (tmp_path / "secrets/transport").exists()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_workflow_public_boundary_does_not_swallow_base_exceptions(
    tmp_path, exception_type
):
    c = Context(tmp_path)
    expected = exception_type()

    def interrupted(_tailnet):
        raise expected

    c.membership = interrupted
    with pytest.raises(exception_type) as caught:
        stage(c.workflow())
    assert caught.value is expected
