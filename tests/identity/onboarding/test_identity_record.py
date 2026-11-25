"""The single settings-identity record authority and its daemon delegation.

Decision: record reads/writes moved to the identity domain; the daemon keeps
public aliases.  These tests pin both the on-disk record semantics and the
identity of the daemon public functions, so a second copy cannot reappear.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hyprial.identity as identity_face
from hyprial.daemon.impl.configuration import identity as daemon_identity
from hyprial.identity.impl.onboarding import identity_record
from hyprial.identity.impl.onboarding.identity_record import (
    IDENTITY_RESIDUE_FIELDS,
    IdentityRecordError,
    node_owner_or_none,
    read_identity_admission,
    read_identity_snapshot,
    read_settings_identity,
    read_settings_identity_metadata,
    resolve_node_owner,
    resolve_node_user_id,
    settings_path,
    write_settings_identity,
    write_settings_owner,
)
from hyprial.identity.impl.onboarding.ports import IdentitySnapshot


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, parents=True)
    return home


def _binding() -> dict[str, str]:
    return {
        "user": "allenwoods",
        "larkUnionId": "on_union_123",
        "proof": "proof-blob",
        "publishedAt": "2026-11-05T00:00:00Z",
    }


def test_record_round_trip_preserves_unrelated_settings(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text(
        json.dumps({"version": 3, "daemon": {"port": 4321}}), encoding="utf-8"
    )
    path = write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        user_id="u-1",
        username="Allen",
        hyprial_home=home,
    )
    assert path == home / "settings.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["version"] == 3
    assert record["daemon"] == {"port": 4321}
    assert record["owner"] == "allenwoods"
    assert record["identityMode"] == "casdoor"
    assert record["identityIssuer"] == "https://issuer.example"
    assert record["userId"] == "u-1"
    assert record["username"] == "Allen"
    assert "identityClaims" not in record
    assert read_settings_identity(hyprial_home=home) == (
        "allenwoods",
        "casdoor",
        "https://issuer.example",
    )
    assert read_settings_identity_metadata(hyprial_home=home) == (
        "casdoor",
        "https://issuer.example",
    )


def test_identical_write_is_byte_stable(tmp_path: Path) -> None:
    home = _home(tmp_path)
    first = write_settings_identity(
        "allenwoods", mode="casdoor", issuer="https://issuer.example", hyprial_home=home
    )
    before = first.read_bytes()
    second = write_settings_identity(
        "allenwoods", mode="casdoor", issuer="https://issuer.example", hyprial_home=home
    )
    assert second == first
    assert second.read_bytes() == before
    assert sorted(path.name for path in home.iterdir()) == ["settings.json"]


def test_writers_do_not_accept_an_identity_binding(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with pytest.raises(TypeError):
        write_settings_identity(
            "allenwoods",
            mode="casdoor",
            issuer="https://issuer.example",
            identity_binding=_binding(),
            hyprial_home=home,
        )
    with pytest.raises(TypeError):
        write_settings_owner(
            "allenwoods", identity_binding=_binding(), hyprial_home=home
        )
    assert not (home / "settings.json").exists()


def test_writers_leave_a_stored_binding_untouched(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text(
        json.dumps({"owner": "someone-else", "identityBinding": _binding()}),
        encoding="utf-8",
    )
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=home,
    )
    write_settings_owner("allenwoods", hyprial_home=home)
    record = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert record["identityBinding"] == _binding()
    assert record["owner"] == "allenwoods"


def test_owner_only_write_keeps_identity_metadata(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_identity(
        "allenwoods", mode="casdoor", issuer="https://issuer.example", hyprial_home=home
    )
    write_settings_owner("allenwoods", hyprial_home=home)
    assert read_settings_identity(hyprial_home=home) == (
        "allenwoods",
        "casdoor",
        "https://issuer.example",
    )


@pytest.mark.parametrize(
    ("mode", "issuer", "message"),
    [
        ("casdoor", None, "requires a non-empty issuer"),
        ("casdoor", "   ", "requires a non-empty issuer"),
        ("local-usage", "https://issuer.example", "must not retain an issuer"),
        ("legacy", None, "unsupported identity mode"),
    ],
)
def test_identity_mode_validation_refuses_partial_records(
    tmp_path: Path, mode: str, issuer: str | None, message: str
) -> None:
    home = _home(tmp_path)
    with pytest.raises(ValueError, match=message):
        write_settings_identity(
            "allenwoods", mode=mode, issuer=issuer, hyprial_home=home
        )
    assert not (home / "settings.json").exists()


def test_owner_grammar_matches_both_write_surfaces(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reserved = "u.ou_" + "b" * 32
    for writer in (
        lambda: write_settings_owner(reserved, hyprial_home=home),
        lambda: write_settings_identity(
            reserved,
            mode="casdoor",
            issuer="https://issuer.example",
            hyprial_home=home,
        ),
    ):
        with pytest.raises(ValueError, match="identity id shape"):
            writer()
    with pytest.raises(ValueError, match="contain no ':'"):
        write_settings_identity(
            "bad:owner", mode="casdoor", issuer="https://issuer.example", hyprial_home=home
        )
    assert not (home / "settings.json").exists()


def test_resolve_owner_precedence_and_loud_failure(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_owner("settings-owner", hyprial_home=home)
    assert node_owner_or_none({}, hyprial_home=home) == "settings-owner"
    assert resolve_node_owner({}, hyprial_home=home) == "settings-owner"
    assert (
        resolve_node_owner({"HYPRIAL_OWNER": "override-owner"}, hyprial_home=home)
        == "override-owner"
    )
    assert node_owner_or_none({"HYPRIAL_OWNER": "  "}, hyprial_home=home) == "settings-owner"
    with pytest.raises(ValueError, match="must not contain ':'"):
        node_owner_or_none({"HYPRIAL_OWNER": "bad:owner"}, hyprial_home=home)

    empty = _home(tmp_path / "empty")
    with pytest.raises(ValueError, match="hyprial login"):
        resolve_node_owner({}, hyprial_home=empty)
    assert node_owner_or_none({}, hyprial_home=empty) is None


def test_malformed_settings_never_rewrites_the_file(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text("{not-json", encoding="utf-8")
    before = (home / "settings.json").read_bytes()
    assert read_settings_identity(hyprial_home=home) is None
    with pytest.raises(ValueError, match="cannot parse"):
        write_settings_identity(
            "allenwoods",
            mode="casdoor",
            issuer="https://issuer.example",
            hyprial_home=home,
        )
    assert (home / "settings.json").read_bytes() == before


def test_settings_path_prefers_explicit_home() -> None:
    explicit = Path("/tmp/explicit-home")
    environ = {"HYPRIAL_HOME": "/tmp/environment-home"}
    assert settings_path(environ, explicit) == explicit / "settings.json"
    assert settings_path(environ, None) == Path(
        "/tmp/environment-home/settings.json"
    ).resolve()
    assert identity_record._settings_path(environ, explicit) == settings_path(
        environ, explicit
    )


def test_daemon_public_aliases_are_the_identity_authority(tmp_path: Path) -> None:
    assert daemon_identity.resolve_node_owner is identity_face.resolve_node_owner
    assert daemon_identity.node_owner_or_none is identity_face.node_owner_or_none
    assert daemon_identity.read_settings_identity is identity_face.read_settings_identity
    assert daemon_identity.write_settings_identity is identity_face.write_settings_identity
    assert daemon_identity.write_settings_owner is identity_face.write_settings_owner
    assert daemon_identity.settings_path is identity_face.settings_path
    assert daemon_identity._settings_path is identity_face.settings_path

    home = _home(tmp_path)
    daemon_identity.write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=home,
    )
    assert identity_face.read_settings_identity(hyprial_home=home) == (
        "allenwoods",
        "casdoor",
        "https://issuer.example",
    )


def test_strict_admission_distinguishes_missing_empty_and_owned(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    missing = read_identity_admission(home)
    assert missing.present is False
    assert missing.snapshot == IdentitySnapshot()

    (home / "settings.json").write_text(
        json.dumps({"version": 3, "daemon": {"port": 4321}}), encoding="utf-8"
    )
    unowned = read_identity_admission(home)
    assert unowned.present is True
    assert unowned.snapshot == IdentitySnapshot()

    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=home,
    )
    owned = read_identity_admission(home)
    assert owned.present is True
    assert owned.snapshot == IdentitySnapshot(
        owner="allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
    )
    assert read_identity_snapshot(home) == owned.snapshot


def test_strict_admission_accepts_a_legacy_owner_only_record(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text(
        json.dumps({"owner": "allenwoods", "identityIssuer": ""}), encoding="utf-8"
    )
    admission = read_identity_admission(home)
    assert admission.present is True
    assert admission.snapshot == IdentitySnapshot(
        owner="allenwoods", mode=None, issuer=""
    )
    assert read_settings_identity(hyprial_home=home) == (
        "allenwoods",
        None,
        "",
    )


@pytest.mark.parametrize(
    ("raw", "code", "residue"),
    [
        ("{not-json", "IDENTITY_SETTINGS_CORRUPT", ()),
        ("[]", "IDENTITY_SETTINGS_NOT_OBJECT", ()),
        (
            '{"identityMode": "casdoor", "identityIssuer": "https://issuer.example"}',
            "IDENTITY_RESIDUE_PRESENT",
            ("identityIssuer", "identityMode"),
        ),
        ('{"owner": 7}', "IDENTITY_OWNER_INVALID", ()),
        ('{"owner": ""}', "IDENTITY_OWNER_INVALID", ()),
        ('{"owner": "bad:owner"}', "IDENTITY_OWNER_INVALID", ()),
        (
            '{"owner": "allenwoods", "identityMode": 7}',
            "IDENTITY_METADATA_INVALID",
            (),
        ),
        (
            '{"owner": "allenwoods", "identityIssuer": []}',
            "IDENTITY_METADATA_INVALID",
            (),
        ),
    ],
)
def test_strict_admission_refuses_corrupt_objects_residue_and_invalid_identity(
    tmp_path: Path, raw: str, code: str, residue: tuple[str, ...]
) -> None:
    home = _home(tmp_path)
    path = home / "settings.json"
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(IdentityRecordError) as caught:
        read_identity_admission(home)
    assert caught.value.code == code
    assert path.read_bytes() == before
    if residue:
        assert tuple(caught.value.data["residueFields"]) == residue


@pytest.mark.parametrize("field", IDENTITY_RESIDUE_FIELDS)
def test_each_declared_residue_field_without_owner_is_refused(
    tmp_path: Path, field: str
) -> None:
    home = _home(tmp_path)
    path = home / "settings.json"
    path.write_text(json.dumps({field: "residue"}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(IdentityRecordError) as caught:
        read_identity_admission(home)
    assert caught.value.code == "IDENTITY_RESIDUE_PRESENT"
    assert tuple(caught.value.data["residueFields"]) == (field,)
    assert path.read_bytes() == before


def test_strict_admission_refuses_symlinked_settings_without_following(
    tmp_path: Path,
) -> None:
    source = _home(tmp_path / "source")
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=source,
    )
    other = _home(tmp_path / "other")
    (other / "settings.json").symlink_to(source / "settings.json")
    before = (source / "settings.json").read_bytes()
    with pytest.raises(IdentityRecordError) as caught:
        read_identity_admission(other)
    assert caught.value.code == "IDENTITY_SETTINGS_SYMLINK"
    assert (source / "settings.json").read_bytes() == before
    assert read_identity_admission(_home(tmp_path / "wrong-home")).present is False


def test_strict_admission_refuses_non_regular_settings(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").mkdir()
    with pytest.raises(IdentityRecordError) as caught:
        read_identity_admission(home)
    assert caught.value.code == "IDENTITY_SETTINGS_UNREADABLE"


def test_strict_admission_reads_only_the_explicit_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _home(tmp_path / "source")
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=source,
    )
    wrong = _home(tmp_path / "wrong-home")
    monkeypatch.setenv("HYPRIAL_HOME", str(source))
    admission = read_identity_admission(wrong)
    assert admission.present is False
    assert admission.snapshot == IdentitySnapshot()
    assert read_identity_snapshot(source).owner == "allenwoods"


def test_legacy_lenient_reader_still_hides_strict_failures(tmp_path: Path) -> None:
    home = _home(tmp_path)
    path = home / "settings.json"
    path.write_text('{"identityMode": "casdoor"}', encoding="utf-8")
    assert read_settings_identity(hyprial_home=home) is None
    path.write_text("{not-json", encoding="utf-8")
    assert read_settings_identity(hyprial_home=home) is None
    path.write_text("[]", encoding="utf-8")
    assert read_settings_identity(hyprial_home=home) is None
    path.write_text('{"owner": 7}', encoding="utf-8")
    assert read_settings_identity(hyprial_home=home) is None


def test_strict_admission_public_exports_are_the_record_authority() -> None:
    assert identity_face.read_identity_admission is read_identity_admission
    assert identity_face.read_identity_snapshot is read_identity_snapshot
    assert identity_face.IdentityRecordError is IdentityRecordError
    assert identity_face.IDENTITY_RESIDUE_FIELDS == IDENTITY_RESIDUE_FIELDS


def _device(
    home: Path,
    device_uid: str = "12345678123442348234123456789abc",
    *,
    owner: str = "allenwoods",
) -> None:
    from hyprial.identity.impl.device import DeviceRecord, write_device_record

    write_device_record(
        home,
        DeviceRecord(
            device_id="device-1",
            owner=owner,
            server_public="nodekey:server",
            client_public="nodekey:client",
            key_generation=1,
            device_uid=device_uid,
            name="laptop",
        ),
    )


def test_onboarding_subject_requires_issuer_user_and_device(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        user_id="u-1",
        hyprial_home=home,
    )
    _device(home)
    subject = identity_record.read_onboarding_subject(home)
    assert subject is not None
    assert (subject.issuer, subject.user_id, subject.device_uid) == (
        "https://issuer.example",
        "u-1",
        "12345678123442348234123456789abc",
    )


def test_onboarding_subject_never_falls_back_to_owner_or_a_shared_partition(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        hyprial_home=home,
    )
    _device(home)
    assert identity_record.read_onboarding_subject(home) is None

    other = _home(tmp_path / "other")
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        user_id="u-1",
        hyprial_home=other,
    )
    assert identity_record.read_onboarding_subject(other) is None


def test_onboarding_subject_reports_corrupt_settings(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(IdentityRecordError):
        identity_record.read_onboarding_subject(home)


def test_onboarding_subject_requires_the_device_owner_to_match(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        user_id="u-1",
        hyprial_home=home,
    )
    _device(home, owner="someone-else")

    assert identity_record.read_onboarding_subject(home) is None
    snapshot, device, subject = identity_record.read_onboarding_identity(home)
    assert snapshot.owner == "allenwoods"
    assert device.owner == "someone-else"
    assert subject is None


def test_onboarding_identity_returns_one_consistent_view(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_identity(
        "allenwoods",
        mode="casdoor",
        issuer="https://issuer.example",
        user_id="u-1",
        hyprial_home=home,
    )
    _device(home)

    snapshot, device, subject = identity_record.read_onboarding_identity(home)
    assert snapshot.owner == "allenwoods"
    assert snapshot.issuer == "https://issuer.example"
    assert device.device_uid == "12345678123442348234123456789abc"
    assert subject is not None
    assert (subject.issuer, subject.user_id, subject.device_uid) == (
        "https://issuer.example",
        "u-1",
        "12345678123442348234123456789abc",
    )


UID = "ou_" + "ab" * 16
UUID_UID = "0b7c4f0e-1d2a-4c3b-9e8f-7a6b5c4d3e2f"


def _settings(home: Path, **fields: object) -> None:
    (home / "settings.json").write_text(json.dumps({"version": 1, **fields}), encoding="utf-8")


def test_user_id_source_order_is_env_then_settings_then_none(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert resolve_node_user_id({}, hyprial_home=home) is None
    _settings(home, owner="alice", userId=UID)
    assert resolve_node_user_id({}, hyprial_home=home) == UID
    assert resolve_node_user_id({"HYPRIAL_USER_ID": UUID_UID}, hyprial_home=home) == UUID_UID
    assert (
        resolve_node_user_id(
            {"HYPRIAL_OWNER": "carol", "HYPRIAL_USER_ID": UUID_UID}, hyprial_home=home
        )
        == UUID_UID
    )


def test_an_owner_override_without_a_user_id_never_reads_settings(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _settings(home, owner="alice", userId=UID)
    assert resolve_node_user_id({"HYPRIAL_OWNER": "carol"}, hyprial_home=home) is None


@pytest.mark.parametrize("value", ["alice", "u.ou_" + "ab" * 16, "ou_short", "OU_" + "AB" * 16])
def test_an_unusable_user_id_raises_instead_of_falling_back(tmp_path: Path, value: str) -> None:
    home = _home(tmp_path)
    with pytest.raises(IdentityRecordError) as from_env:
        resolve_node_user_id({"HYPRIAL_USER_ID": value}, hyprial_home=home)
    assert from_env.value.data["field"] == "HYPRIAL_USER_ID"
    _settings(home, owner="alice", userId=value)
    with pytest.raises(IdentityRecordError) as from_settings:
        resolve_node_user_id({}, hyprial_home=home)
    assert from_settings.value.data["field"] == "userId"
    assert from_env.value.code == from_settings.value.code == "IDENTITY_METADATA_INVALID"


def test_a_non_string_settings_user_id_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _settings(home, owner="alice", userId=7)
    with pytest.raises(IdentityRecordError) as raised:
        resolve_node_user_id({}, hyprial_home=home)
    assert raised.value.code == "IDENTITY_METADATA_INVALID"


@pytest.mark.parametrize("owner", ["u.ou_" + "ab" * 16, "d." + "0" * 32, "a." + "b" * 26])
def test_an_id_shaped_owner_override_is_refused(tmp_path: Path, owner: str) -> None:
    home = _home(tmp_path)
    with pytest.raises(ValueError, match="identity id shape"):
        node_owner_or_none({"HYPRIAL_OWNER": owner}, hyprial_home=home)
    with pytest.raises(ValueError, match="identity id shape"):
        resolve_node_owner({"HYPRIAL_OWNER": owner}, hyprial_home=home)


def test_the_public_faces_export_the_user_id_reader() -> None:
    assert identity_face.resolve_node_user_id is resolve_node_user_id
    assert "resolve_node_user_id" in identity_face.__all__


def test_the_serving_uid_pairs_with_an_explicit_owner_and_the_settings_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _settings(home, owner="alice", userId=UID, identityMode="casdoor",
              identityIssuer="https://id.example")
    resolve = daemon_identity.resolve_user_identity
    common = {"hyprial_home": home, "settings_issuer": "https://id.example"}
    assert resolve(owner="alice", user_id=None, **common) == (None, "")
    assert resolve(owner="alice", user_id=UUID_UID, **common) == (UUID_UID, "")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.delenv("HYPRIAL_USER_ID", raising=False)
    assert resolve(owner=None, user_id=None, **common) == (UID, "https://id.example")
    monkeypatch.setenv("HYPRIAL_USER_ID", UUID_UID)
    assert resolve(owner=None, user_id=None, **common) == (UUID_UID, "")
    monkeypatch.setenv("HYPRIAL_USER_ID", "not-a-sub")
    assert resolve(owner=None, user_id=None, **common) == (None, "")
