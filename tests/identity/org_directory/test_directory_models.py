"""DirectoryDevice round trips, org naming, and bad-data rejection (§4.1)."""

from __future__ import annotations

import base64

import pytest

from hyprial.identity.impl.org_directory.models import (
    MAX_PROTECTED_DOC_ID_LENGTH,
    ProtectedDocIdTooLongError,
    DirectoryDevice,
    directory_device_path,
    directory_owner_principal,
    is_org_acl_space,
    is_org_directory_space,
    org_from_space_name,
    org_space_name,
    parse_protected_directory_doc_id,
    protected_directory_doc_id,
)


def _device() -> DirectoryDevice:
    return DirectoryDevice(
        device_id="node-7f3a",
        owner="alicesmith@example.org",
        server_public="nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        client_public="nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
        client_public_by_server={
            "nodekey:" + "a" * 64: "nodekey:" + "c" * 64,
        },
        address="10.0.0.1:41271#psk-secret",
        key_generation=1,
        updated_at="2026-10-03T09:00:00Z",
    )


def test_round_trip_preserves_every_field() -> None:
    assert DirectoryDevice.from_record(_device().as_record()) == _device()


def test_device_path_normalizes_user_prefix_and_keeps_device_id_opaque() -> None:
    expected = "directory/devices/alicesmith@example.org/random-device-7.json"
    assert directory_device_path(
        "alicesmith@example.org", "random-device-7"
    ) == expected
    assert directory_device_path(
        "user:alicesmith@example.org", "random-device-7"
    ) == expected
    assert directory_owner_principal("alicesmith@example.org") == (
        "user:alicesmith@example.org"
    )


@pytest.mark.parametrize("device_id", ["", ".", "..", "a/b", r"a\b"])
def test_device_path_rejects_unsafe_opaque_component(device_id: str) -> None:
    with pytest.raises(ValueError, match="safe path component|non-empty"):
        directory_device_path("alicesmith@example.org", device_id)


def test_protected_doc_id_encoding_is_injective_and_round_trips_separators() -> None:
    """R3 deterministic ids preserve ambiguous-looking owner/id pairs."""

    space_id = "space:acme"
    first = protected_directory_doc_id(
        space_id, "directory/devices/a-b/c.json", space_owner="user:owner"
    )
    second = protected_directory_doc_id(
        space_id, "directory/devices/a/b-c.json", space_owner="user:owner"
    )

    assert first is not None and second is not None and first != second
    assert parse_protected_directory_doc_id(first) == (
        space_id,
        "devices",
        "user:a-b",
        "c.json",
    )
    assert parse_protected_directory_doc_id(second) == (
        space_id,
        "devices",
        "user:a",
        "b-c.json",
    )


def test_protected_doc_id_rejects_non_ascii_length_digits() -> None:
    """L1: Unicode digits cannot smuggle a non-canonical length prefix."""

    payload = "１:a7:devices6:user:a6:a.json".encode()
    encoded = base64.b32encode(payload).decode("ascii").rstrip("=").lower()
    with pytest.raises(ValueError, match="segment length"):
        parse_protected_directory_doc_id(f"doc-orgdir-v3-{encoded}")


def test_protected_doc_ids_are_single_case_and_differ_beyond_case() -> None:
    """Ids become file names; case-insensitive volumes must keep them apart."""

    upper = protected_directory_doc_id(
        "space-1", "directory/devices/alice/Node-A.json", space_owner="user:o"
    )
    lower = protected_directory_doc_id(
        "space-1", "directory/devices/alice/node-a.json", space_owner="user:o"
    )
    assert upper is not None and lower is not None
    assert upper == upper.lower() and lower == lower.lower()
    assert upper.lower() != lower.lower()
    assert parse_protected_directory_doc_id(upper)[3] == "Node-A.json"
    assert parse_protected_directory_doc_id(lower)[3] == "node-a.json"


def _base64url_case_twins() -> tuple[str, str]:
    """Two device leaves whose base64url ids differ only in letter case."""

    head = "7:space-15:devices10:user:alice"
    for stem in range(1000):
        leaf = f"dev{stem:03d}.json"
        token = base64.urlsafe_b64encode(f"{head}{len(leaf)}:{leaf}".encode())
        text = token.decode("ascii")
        for index in range(len(text) - 1, len(text) // 2, -1):
            if not text[index].isalpha():
                continue
            twin = text[:index] + text[index].swapcase() + text[index + 1 :]
            try:
                raw = base64.urlsafe_b64decode(twin).decode("utf-8")
            except ValueError:
                continue
            prefix = f"{head}{len(leaf)}:"
            other = raw[len(prefix):] if raw.startswith(prefix) else ""
            if (
                len(other) == len(leaf)
                and other != leaf
                and other.endswith(".json")
                and "/" not in other
                and other.isprintable()
            ):
                return leaf, other
    raise AssertionError("no base64url case twins found")


def test_base64url_case_twins_stay_distinct_files_on_this_volume(tmp_path) -> None:
    """Review 953 follow-up: case-only twins under base64url must not share a file."""

    first, second = _base64url_case_twins()
    ids = [
        protected_directory_doc_id(
            "space-1", f"directory/devices/alice/{leaf}", space_owner="user:o"
        )
        for leaf in (first, second)
    ]
    assert ids[0] is not None and ids[1] is not None
    # The property itself, independent of the host file system.
    assert ids[0].lower() != ids[1].lower()
    probe = tmp_path / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("volume is case-sensitive; the on-disk collision cannot occur here")
    probe.unlink()
    for doc_id in ids:
        (tmp_path / f"{doc_id}.loro").write_text(doc_id, encoding="utf-8")
    assert sorted(path.read_text() for path in tmp_path.glob("*.loro")) == sorted(ids)


def test_protected_doc_id_length_boundary_is_explicit() -> None:
    """The longest accepted id fits; one more byte is refused, never truncated."""

    def doc_id(length: int) -> str | None:
        return protected_directory_doc_id(
            "space-1",
            f"directory/devices/alice/{'d' * (length - 5)}.json",
            space_owner="user:o",
        )

    longest = 0
    for length in range(6, 1200):
        try:
            accepted = doc_id(length)
        except ProtectedDocIdTooLongError:
            break
        assert accepted is not None
        assert len(accepted) <= MAX_PROTECTED_DOC_ID_LENGTH
        longest = length
    # The bound is the protocol's, not a file name's (the store hashes ids).
    assert len(doc_id(longest) or "") > 255
    assert longest > 0
    with pytest.raises(ProtectedDocIdTooLongError):
        doc_id(longest + 1)


def test_record_is_camel_case_with_exactly_the_frozen_keys() -> None:
    assert _device().as_record() == {
        "deviceId": "node-7f3a",
        "owner": "alicesmith@example.org",
        "serverPublic": "nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        "clientPublic": "nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
        "clientPublicByServer": {
            "nodekey:" + "a" * 64: "nodekey:" + "c" * 64,
        },
        "address": "10.0.0.1:41271#psk-secret",
        "keyGeneration": 1,
        "updatedAt": "2026-10-03T09:00:00Z",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda r: r.pop("address"), id="missing-address"),
        pytest.param(
            lambda r: r.pop("clientPublicByServer"),
            id="missing-clientPublicByServer",
        ),
        pytest.param(lambda r: r.pop("updatedAt"), id="missing-updatedAt"),
        pytest.param(lambda r: r.update({"extra": True}), id="unknown-key"),
        pytest.param(lambda r: r.update({"address": ""}), id="empty-address"),
        pytest.param(lambda r: r.update({"owner": 3}), id="owner-not-a-string"),
        pytest.param(lambda r: r.update({"serverPublic": "nodekey:"}), id="bare-prefix"),
        pytest.param(
            lambda r: r.update({"clientPublicByServer": {"bad": "nodekey:key"}}),
            id="bad-derived-key-map",
        ),
        pytest.param(lambda r: r.update({"keyGeneration": True}), id="generation-bool"),
        pytest.param(lambda r: r.update({"keyGeneration": 0}), id="generation-zero"),
        pytest.param(lambda r: r.update({"updatedAt": "n/a"}), id="updated-garbage"),
        pytest.param(
            lambda r: r.update({"updatedAt": "2026-10-03T09:00:00"}), id="updated-no-offset"
        ),
        pytest.param(
            lambda r: r.update({"updatedAt": "2026-10-03T09:00:00+02:00"}),
            id="updated-non-utc-offset",
        ),
    ],
)
def test_bad_data_is_rejected(mutation) -> None:
    payload = _device().as_record()
    mutation(payload)
    with pytest.raises(ValueError):
        DirectoryDevice.from_record(payload)


def test_client_key_roles_cannot_alias() -> None:
    payload = _device().as_record()
    payload["clientPublicByServer"] = {
        "nodekey:" + "a" * 64: payload["clientPublic"],
    }
    with pytest.raises(ValueError, match="master clientPublic"):
        DirectoryDevice.from_record(payload)


def test_per_server_client_keys_cannot_alias_each_other() -> None:
    payload = _device().as_record()
    shared = "nodekey:" + "c" * 64
    payload["clientPublicByServer"] = {
        "nodekey:" + "a" * 64: shared,
        "nodekey:" + "b" * 64: shared,
    }
    with pytest.raises(ValueError, match="must be unique"):
        DirectoryDevice.from_record(payload)


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        DirectoryDevice.from_record("nope")  # type: ignore[arg-type]


# -- org naming -----------------------------------------------------------------


@pytest.mark.parametrize(
    "org", ["group-a", "group-acme", "group-acme-lab-2", "group-0org"]
)
def test_org_space_name_accepts_the_documented_shape(org: str) -> None:
    assert org_space_name(org) == org
    assert is_org_directory_space(org)
    assert not is_org_acl_space(org)


@pytest.mark.parametrize(
    "org",
    [
        "",
        "acme",
        "org-acme",
        "Acme",
        "-acme",
        "group-acme_acl",
        "group-acme lab",
        "group-acme.lab",
        "group-acme-acl",
        "a" * 64,
        "../escape",
        "group/../x",
    ],
)
def test_org_space_name_rejects_everything_else(org: str) -> None:
    with pytest.raises(ValueError, match=r"\[a-z0-9\]"):
        org_space_name(org)


def test_org_space_name_rejects_non_strings() -> None:
    with pytest.raises(ValueError):
        org_space_name(123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "space,expected",
    [
        ("group-acme", "group-acme"),
        ("group-a", "group-a"),
        ("group-acme-lab-2", "group-acme-lab-2"),
        ("group-" + "b" * 57, "group-" + "b" * 57),
    ],
)
def test_org_from_space_name_round_trips(space: str, expected: str) -> None:
    assert org_from_space_name(space) == expected
    assert org_space_name(expected) == space


@pytest.mark.parametrize(
    "space",
    [
        "acme",
        "org-acme",
        "Group-acme",
        "group-",
        "group-Acme",
        "group-acme_lab",
        "group-acme-acl",
        "group-" + "b" * 58,
        "personal",
        "",
    ],
)
def test_org_from_space_name_returns_none_for_non_org_spaces(space: str) -> None:
    assert org_from_space_name(space) is None


@pytest.mark.parametrize("space", ["group-acme-acl", "group-a-acl"])
def test_org_acl_space_is_distinct_from_the_directory(space: str) -> None:
    assert is_org_acl_space(space)
    assert not is_org_directory_space(space)
    assert org_from_space_name(space) is None
