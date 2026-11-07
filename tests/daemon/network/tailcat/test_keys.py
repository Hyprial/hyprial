"""Device key provisioning through the sidecar's genkey subcommand."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from hyprial.identity.impl.device import (
    DeviceRecord,
    device_key_path,
    read_device_record,
)

from hyprial.daemon.impl.network.tailcat import (
    BINARY_ENV,
    TailcatSidecarError,
    ensure_device_key,
)

_SERVER_PUBLIC = "nodekey:server-fixture"
_CLIENT_PUBLIC = "nodekey:client-fixture"


def _genkey_script(
    tmp_path: Path,
    *,
    corrupt: bool = False,
    version: int | float = 6,
    server_public: str = _SERVER_PUBLIC,
    client_public: str = _CLIENT_PUBLIC,
    filename: str = "hyprial-tailcat",
) -> Path:
    """A fake ``hyprial-tailcat``: genkey writes a key file and prints publics.

    Exit 0 when the key file was minted, 3 when it already existed -- the two
    success shapes of the real subcommand.
    """

    public = json.dumps(
        {"v": version, "serverPublic": server_public, "clientPublic": client_public}
    )
    if corrupt:
        body = "echo 'corrupt key file' >&2\nexit 1\n"
    else:
        body = (
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--out" ]; then OUT="$2"; shift 2; else shift; fi\n'
            "done\n"
            'if [ -f "$OUT" ]; then CODE=3; else CODE=0; fi\n'
            'printf \'{"v":3,"server":{"private":"privkey:x","presharedKey":"p"},'
            '"client":{"private":"privkey:y"}}\' > "$OUT"\n'
            "chmod 600 \"$OUT\"\n"
            f"echo '{public}'\n"
            "exit $CODE\n"
        )
    script = tmp_path / filename
    script.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.mark.parametrize('version', [5, 6.0])
def test_genkey_refuses_legacy_or_noninteger_version(tmp_path, version):
    binary = _genkey_script(tmp_path, version=version)
    with pytest.raises(TailcatSidecarError) as caught:
        ensure_device_key(tmp_path / 'home', owner='tester', device_id='node-a',
                          environ={BINARY_ENV: str(binary)})
    assert caught.value.code == 'KEYGEN_FAILED'
    assert read_device_record(tmp_path / 'home') is None


def test_genkey_exit_zero_mints_the_key_and_publishes_the_record(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)

    record = ensure_device_key(
        home, owner="tester", device_id="node-a", environ={BINARY_ENV: str(binary)}
    )

    assert record == DeviceRecord(
        device_id="node-a",
        device_uid=record.device_uid,
        name=record.name,
        owner="tester",
        server_public=_SERVER_PUBLIC,
        client_public=_CLIENT_PUBLIC,
        key_generation=1,
    )
    key_file = device_key_path(home)
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    # The private key material never enters the record.
    assert "privkey" not in json.dumps(record.as_record())
    assert read_device_record(home) == record
    assert record.device_uid is not None
    assert len(record.device_uid) == 32
    assert record.name


def test_genkey_exit_three_keeps_the_key_and_republishes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)
    environ = {BINARY_ENV: str(binary)}

    first = ensure_device_key(home, owner="tester", device_id="node-a", environ=environ)
    key_bytes = device_key_path(home).read_bytes()
    second = ensure_device_key(
        home, owner="tester", device_id="node-a", environ={BINARY_ENV: str(binary)}
    )

    # The key file is untouched (exit 3: no overwrite without --force) and
    # the same publics keep the same generation.
    assert device_key_path(home).read_bytes() == key_bytes
    assert second == first
    assert second.key_generation == 1
    assert second.device_uid == first.device_uid


def test_device_uid_survives_key_rotation_and_record_reload(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first_binary = _genkey_script(tmp_path, filename="tailcat-one")
    first = ensure_device_key(
        home,
        owner="tester",
        device_id="node-a",
        environ={BINARY_ENV: str(first_binary)},
    )
    # Model a real rotation by removing only the private key and returning new
    # public halves. The public record remains and must carry the stable UID.
    device_key_path(home).unlink()
    second_binary = _genkey_script(
        tmp_path,
        server_public="nodekey:rotated-server",
        client_public="nodekey:rotated-client",
        filename="tailcat-two",
    )
    rotated = ensure_device_key(
        home,
        owner="tester",
        device_id="node-a",
        environ={BINARY_ENV: str(second_binary)},
    )
    assert rotated.key_generation == first.key_generation + 1
    assert rotated.device_uid == first.device_uid
    assert rotated.device_id == first.device_id == "node-a"
    assert read_device_record(home) == rotated


def test_legacy_record_gains_one_stable_device_uid_on_next_login_stage(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)
    environ = {BINARY_ENV: str(binary)}
    legacy = DeviceRecord(
        device_id="node-a",
        owner="tester",
        server_public=_SERVER_PUBLIC,
        client_public=_CLIENT_PUBLIC,
        key_generation=1,
    )
    from hyprial.identity.impl.device import write_device_record

    write_device_record(home, legacy)
    upgraded = ensure_device_key(
        home, owner="tester", device_id="node-a", environ=environ
    )
    again = ensure_device_key(
        home, owner="tester", device_id="node-a", environ=environ
    )
    assert upgraded.device_uid is not None
    assert again.device_uid == upgraded.device_uid
    assert upgraded.device_id == again.device_id == legacy.device_id


def test_reserved_node_id_is_not_a_device_name_gate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)
    transitional_id = "d." + "b" * 32
    record = ensure_device_key(
        home,
        owner="tester",
        device_id=transitional_id,
        name="workstation",
        environ={BINARY_ENV: str(binary)},
    )
    assert record.device_id == transitional_id
    assert record.name == "workstation"


@pytest.mark.parametrize("hostname", ["D." + "B" * 32, "A." + "A" * 26, "U.OU_" + "B" * 32])
def test_uppercase_id_shaped_hostname_is_reserved_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostname: str
) -> None:
    """Case-insensitive reservation (APFS): an uppercase id shape is refused."""

    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)
    monkeypatch.setattr("socket.gethostname", lambda: hostname)
    with pytest.raises(ValueError, match="HYPRIAL_DEVICE_NAME"):
        ensure_device_key(
            home,
            owner="tester",
            device_id="d." + "b" * 32,
            environ={BINARY_ENV: str(binary)},
        )
    assert read_device_record(home) is None


def test_reserved_hostname_names_the_device_name_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)
    reserved_id = "d." + "b" * 32
    monkeypatch.setattr("socket.gethostname", lambda: reserved_id)

    with pytest.raises(ValueError, match="HYPRIAL_DEVICE_NAME"):
        ensure_device_key(
            home,
            owner="tester",
            device_id=reserved_id,
            environ={BINARY_ENV: str(binary)},
        )
    assert read_device_record(home) is None

    record = ensure_device_key(
        home,
        owner="tester",
        device_id=reserved_id,
        environ={
            BINARY_ENV: str(binary),
            "HYPRIAL_DEVICE_NAME": "workstation",
        },
    )
    assert record.device_id == reserved_id
    assert record.name == "workstation"


@pytest.mark.parametrize("name", ["d.lane@example.com", "a.smith@x", "u.zhang"])
def test_id_prefix_hostname_is_accepted_as_device_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path)
    monkeypatch.setattr("socket.gethostname", lambda: name)
    record = ensure_device_key(
        home,
        owner="tester",
        device_id="transitional-node",
        environ={BINARY_ENV: str(binary)},
    )
    assert record.name == name


def test_genkey_failure_is_keygen_failed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary = _genkey_script(tmp_path, corrupt=True)

    with pytest.raises(TailcatSidecarError) as raised:
        ensure_device_key(
            home, owner="tester", device_id="node-a", environ={BINARY_ENV: str(binary)}
        )
    assert raised.value.code == "KEYGEN_FAILED"
    assert read_device_record(home) is None


def test_genkey_without_public_json_is_keygen_failed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    script = tmp_path / "hyprial-tailcat"
    script.write_text("#!/bin/sh\necho 'not json'\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(TailcatSidecarError) as raised:
        ensure_device_key(
            home, owner="tester", device_id="node-a", environ={BINARY_ENV: str(script)}
        )
    assert raised.value.code == "KEYGEN_FAILED"


def test_genkey_requires_a_locatable_sidecar(tmp_path: Path) -> None:
    with pytest.raises(TailcatSidecarError) as raised:
        ensure_device_key(
            tmp_path / "home", owner="tester", device_id="node-a", environ={}
        )
    assert raised.value.code == "SIDECAR_MISSING"
