"""Public DTO and slot safety contracts."""

import pytest

from hyprial.identity import TailnetCredentials

from .support import Context, state


@pytest.mark.parametrize("port", [True, 0, 65536])
def test_dial_target_rejects_mutable_authority_and_invalid_port(port):
    with pytest.raises(ValueError):
        TailnetCredentials.DialTarget({"owner": "alice"}, port)


def test_frozen_transport_rejects_mutable_descriptor():
    with pytest.raises(ValueError):
        TailnetCredentials.FrozenTransport({"ca": bytearray(b"mutable")}, None)


def test_tombstone_projection_can_represent_the_real_sidecar_shape():
    row = TailnetCredentials.MemberProjection("alice", "node", "master", "server", (), "", "tombstone", 1)
    assert row.address == ""


def test_descriptor_version_cannot_retain_a_mutable_equality_object(tmp_path):
    context = Context(tmp_path / "home")
    descriptor = context.descriptor(TailnetCredentials.DialTarget(context.pin("tailnet-A"), 7449))

    class MutableVersion:
        def __eq__(self, other):
            return True

    with pytest.raises(ValueError):
        TailnetCredentials.Descriptor(MutableVersion(), "tailnet-A",
            TailnetCredentials.DialTarget(context.pin("tailnet-A"), 7449),
            descriptor["origin"], descriptor["serverName"], context.ca.pem, descriptor["caSha256"])


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "fifo", "mode", "directory", "ancestor"])
def test_read_rejects_unsafe_active_paths(tmp_path, mutation):
    from hyprial.identity import TailnetCredentialError
    import os

    context = Context(tmp_path / "home")
    runtime = context.runtime()
    path = state(context.home) / "active.json"
    path.parent.mkdir(parents=True, mode=0o700)
    for parent in path.parents:
        if parent == context.home:
            break
        parent.chmod(0o700)
    if mutation in ("symlink", "hardlink"):
        target = tmp_path / "outside"
        target.write_bytes(b"outside")
        target.chmod(0o600)
        path.symlink_to(target) if mutation == "symlink" else os.link(target, path)
    elif mutation == "fifo":
        os.mkfifo(path, 0o600)
    elif mutation == "directory":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"{}")
        path.chmod(0o644 if mutation == "mode" else 0o600)
        if mutation == "ancestor":
            path.parent.chmod(0o755)
    with pytest.raises(TailnetCredentialError) as caught:
        runtime.read()
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"


def test_frozen_mtls_generation_rejects_bool_and_nested_values():
    with pytest.raises(ValueError):
        TailnetCredentials.MtlsReference(True, *(["a" * 64] * 4))
    context_pin = {"owner": []}
    with pytest.raises(ValueError):
        TailnetCredentials.DialTarget(context_pin, 7449)


def test_malformed_active_is_rejected_instead_of_absent_or_an_observed_active(tmp_path):
    from hyprial.identity import TailnetCredentialError
    context = Context(tmp_path / "home")
    path = state(context.home) / "active.json"
    path.parent.mkdir(parents=True, mode=0o700)
    for parent in path.parents:
        if parent == context.home:
            break
        parent.chmod(0o700)
    path.write_bytes(b'{"tailnetId":"wrong-tailnet"}')
    path.chmod(0o600)
    for method in (context.runtime().read, context.runtime().status):
        with pytest.raises(TailnetCredentialError) as caught:
            method()
        assert caught.value.code == "TAILNET_SLOT_INVALID"


def test_bound_home_disappearance_is_not_an_unconfigured_slot(tmp_path):
    from hyprial.identity import TailnetCredentialError
    context = Context(tmp_path / "home")
    runtime = context.runtime()
    assert runtime.read() is None  # True slot absence remains valid.
    context.home.rename(tmp_path / "moved-home")
    with pytest.raises(TailnetCredentialError) as caught:
        runtime.read()
    assert caught.value.code == "TAILNET_HOME_MISMATCH"


@pytest.mark.parametrize("port", [True, 0, 65536])
def test_invalid_remote_port_is_rejected_with_an_otherwise_valid_full_pin(tmp_path, port):
    context = Context(tmp_path / "home")
    with pytest.raises(ValueError):
        TailnetCredentials.DialTarget(context.pin("tailnet-A"), port)
