"""AT07 item 2, second phase: a key this machine holds installs what it opens."""

from __future__ import annotations

import base64
import builtins
from contextlib import contextmanager
import io
import json
import os
import stat
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, load_manifest
from hyprial.daemon.impl.transfer.archive.envelope import (
    CredentialInstall,
    CredentialPolicy,
    CredentialRecord,
    EnvelopeFormatError,
    RecipientKeyError,
    ensure_recipient_key,
    generate_keypair,
    plan_credential_seal,
    read_recipient_key,
    recipient_key_path,
    recipient_public_key_b64,
    seal_envelope,
    write_recipient_key,
)
from hyprial.daemon.impl.transfer.landing.credentials import (
    CREDENTIAL_REAUTH_REQUIRED,
    CREDENTIAL_RESTORABLE,
    CREDENTIAL_RESTORED,
    CredentialBinding,
    CredentialEnvelopeInvalid,
    CredentialInstallError,
    apply_credential_landing,
    plan_credential_landing,
)
from hyprial.identity import AgentSecretStore
from hyprial.kernel import PrivateAccessUnsupported, path_is_reparse

ACTOR = "codex-sw"
OWNER = "owner@example.com"
CREATED_AT = "2026-10-14T10:00:00+08:00"
ENVELOPE_PATH = "credentials/envelope.json"
CODEX_AUTH = '{"OPENAI_API_KEY": "sk-marker-value"}'
DEEPSEEK = "sk-deepseek-marker"
MARKER = "sk-marker-value"


def _sealed(
    records: Sequence[CredentialRecord],
    *,
    recipient_public_key: bytes,
    secrets: Mapping[str, object],
    policy: CredentialPolicy | None = None,
) -> dict[str, object]:
    return seal_envelope(
        dict(secrets),
        recipient_public_key=recipient_public_key,
        actor=ACTOR,
        owner=OWNER,
        created_at=CREATED_AT,
        records=records,
        policy=policy,
    )


def _bundle(root: Path, envelope: Mapping[str, object]) -> Path:
    home = root / "src" / "agents" / ACTOR
    (home / "sessions").mkdir(parents=True)
    (home / "notes.md").write_text("payload\n")
    target = home / ENVELOPE_PATH
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(envelope, sort_keys=True) + "\n")
    bundle_dir = root / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="devusr-dev",
        created_at=CREATED_AT,
        identity={"entityToken": "source-token"},
        credential_envelope=ENVELOPE_PATH,
    )
    return bundle_dir


def test_the_recipient_key_is_minted_once_at_0600(tmp_path: Path) -> None:
    home = tmp_path / "machine"
    path = recipient_key_path(home)

    private, public = ensure_recipient_key(path)

    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert ensure_recipient_key(path) == (private, public)
    assert read_recipient_key(path) == (private, public)
    assert recipient_public_key_b64(public)


def test_a_recipient_key_with_mismatched_halves_is_refused(tmp_path: Path) -> None:
    private, _public = generate_keypair()
    _other_private, other_public = generate_keypair()
    path = recipient_key_path(tmp_path / "machine")
    write_recipient_key(path, private, other_public)

    with pytest.raises(RecipientKeyError):
        read_recipient_key(path)


def test_recipient_key_write_refuses_symlinked_ancestor_directory(
    tmp_path: Path,
) -> None:
    empty_outside = tmp_path / "empty-outside"
    empty_outside.mkdir()
    home_one = tmp_path / "machine-one"
    home_one.mkdir()
    (home_one / "secrets").symlink_to(empty_outside)

    with pytest.raises(PrivateAccessUnsupported):
        ensure_recipient_key(recipient_key_path(home_one))
    assert list(empty_outside.iterdir()) == []

    nested_outside = tmp_path / "nested-outside"
    (nested_outside / "transfer").mkdir(parents=True)
    home_two = tmp_path / "machine-two"
    home_two.mkdir()
    (home_two / "secrets").symlink_to(nested_outside)
    private, public = generate_keypair()

    with pytest.raises(PrivateAccessUnsupported):
        write_recipient_key(recipient_key_path(home_two), private, public)
    assert list((nested_outside / "transfer").iterdir()) == []


@contextmanager
def _record_outside_opens(outside: Path) -> Iterator[list[str]]:
    resolved_outside = outside.resolve()
    opened: list[str] = []
    real_os_open = os.open
    real_io_open = io.open
    real_builtins_open = builtins.open

    def _check(candidate: object) -> None:
        if isinstance(candidate, (str, bytes, os.PathLike)):
            try:
                resolved = Path(os.fsdecode(os.fspath(candidate))).resolve()
            except OSError:
                return
            if resolved == resolved_outside or resolved.is_relative_to(
                resolved_outside
            ):
                opened.append(os.fsdecode(os.fspath(candidate)))

    def _guarded_os_open(path, flags, *args, **kwargs):
        _check(path)
        return real_os_open(path, flags, *args, **kwargs)

    def _guarded_io_open(file, *args, **kwargs):
        _check(file)
        return real_io_open(file, *args, **kwargs)

    def _guarded_builtins_open(file, *args, **kwargs):
        _check(file)
        return real_builtins_open(file, *args, **kwargs)

    os.open = _guarded_os_open
    io.open = _guarded_io_open
    builtins.open = _guarded_builtins_open
    try:
        yield opened
    finally:
        os.open = real_os_open
        io.open = real_io_open
        builtins.open = real_builtins_open


def test_recipient_key_read_refuses_symlinked_target_file(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-key"
    outside_key = outside / "recipient-key.json"
    write_recipient_key(outside_key, *generate_keypair())
    original_bytes = outside_key.read_bytes()
    original_stat = outside_key.stat()

    home = tmp_path / "machine"
    link = recipient_key_path(home)
    link.parent.mkdir(parents=True, mode=0o700)
    link.parent.parent.chmod(0o700)
    link.symlink_to(outside_key)

    dangling_outside = tmp_path / "dangling-outside"
    dangling_outside.mkdir()
    dangling_home = tmp_path / "machine-dangling"
    dangling_link = recipient_key_path(dangling_home)
    dangling_link.parent.mkdir(parents=True, mode=0o700)
    dangling_link.parent.parent.chmod(0o700)
    dangling_link.symlink_to(dangling_outside / "missing.json")

    with (
        _record_outside_opens(outside) as opened_outside,
        _record_outside_opens(dangling_outside) as opened_dangling,
    ):
        with pytest.raises(PrivateAccessUnsupported):
            read_recipient_key(link)
        with pytest.raises(PrivateAccessUnsupported):
            ensure_recipient_key(link)
        with pytest.raises(PrivateAccessUnsupported):
            read_recipient_key(dangling_link)
        with pytest.raises(PrivateAccessUnsupported):
            ensure_recipient_key(dangling_link)

    assert opened_outside == []
    assert opened_dangling == []
    assert outside_key.read_bytes() == original_bytes
    assert outside_key.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert outside_key.stat().st_ino == original_stat.st_ino
    assert list(outside.iterdir()) == [outside_key]
    assert list(dangling_outside.iterdir()) == []


def test_recipient_key_read_refuses_symlinked_ancestor_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-secrets"
    outside_key = outside / "transfer" / "recipient-key.json"
    write_recipient_key(outside_key, *generate_keypair())
    original_bytes = outside_key.read_bytes()
    original_stat = outside_key.stat()

    home_one = tmp_path / "machine-one"
    home_one.mkdir()
    (home_one / "secrets").symlink_to(outside)

    home_two = tmp_path / "machine-two"
    (home_two / "secrets").mkdir(parents=True, mode=0o700)
    (home_two / "secrets" / "transfer").symlink_to(outside / "transfer")

    with _record_outside_opens(outside) as opened_outside:
        for candidate_home in (home_one, home_two):
            with pytest.raises(PrivateAccessUnsupported):
                read_recipient_key(recipient_key_path(candidate_home))
            with pytest.raises(PrivateAccessUnsupported):
                ensure_recipient_key(recipient_key_path(candidate_home))

    assert opened_outside == []
    assert outside_key.read_bytes() == original_bytes
    assert outside_key.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert outside_key.stat().st_ino == original_stat.st_ino
    assert list((outside / "transfer").iterdir()) == [outside_key]


def test_plan_credential_seal_refuses_symlinked_ancestor_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-native"
    outside_auth = outside / "codex" / "auth.json"
    outside_auth.parent.mkdir(parents=True)
    outside_auth.write_text(CODEX_AUTH, encoding="utf-8")
    original_bytes = outside_auth.read_bytes()
    original_stat = outside_auth.stat()

    home_one = tmp_path / "agent-one"
    (home_one / "secrets").mkdir(parents=True)
    (home_one / "secrets" / "native").symlink_to(outside)

    home_two = tmp_path / "agent-two"
    (home_two / "secrets" / "native").mkdir(parents=True)
    (home_two / "secrets" / "native" / "codex").symlink_to(outside / "codex")

    with _record_outside_opens(outside) as opened_outside:
        for candidate_home in (home_one, home_two):
            with pytest.raises(PrivateAccessUnsupported):
                plan_credential_seal(candidate_home)

    assert opened_outside == []
    assert outside_auth.read_bytes() == original_bytes
    assert outside_auth.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert outside_auth.stat().st_ino == original_stat.st_ino
    assert list((outside / "codex").iterdir()) == [outside_auth]


def test_plan_credential_seal_refuses_symlinked_leaf_file(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-leaf"
    outside.mkdir()
    outside_auth = outside / "auth.json"
    outside_auth.write_text(CODEX_AUTH, encoding="utf-8")
    original_bytes = outside_auth.read_bytes()
    original_stat = outside_auth.stat()

    home = tmp_path / "agent"
    leaf = home / "secrets" / "native" / "codex" / "auth.json"
    leaf.parent.mkdir(parents=True)
    leaf.symlink_to(outside_auth)

    with _record_outside_opens(outside) as opened_outside:
        with pytest.raises(PrivateAccessUnsupported):
            plan_credential_seal(home)

    assert opened_outside == []
    assert outside_auth.read_bytes() == original_bytes
    assert outside_auth.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert outside_auth.stat().st_ino == original_stat.st_ino
    assert list(outside.iterdir()) == [outside_auth]


def _installable_bundle(
    root: Path, *, policy: CredentialPolicy | None = None
) -> tuple[Path, bytes]:
    private, public = generate_keypair()
    envelope = _sealed(
        [
            CredentialRecord(
                name="codex-auth",
                machine_bound=False,
                install=CredentialInstall(
                    kind="native-file", harness="codex", path="auth.json"
                ),
            ),
            CredentialRecord(
                name="deepseek",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("DEEPSEEK_API_KEY",),
                    field="API_KEY",
                ),
            ),
            CredentialRecord(name="github"),
        ],
        recipient_public_key=public,
        secrets={"codex-auth": CODEX_AUTH, "deepseek": DEEPSEEK, "github": "ghp-x"},
        policy=policy,
    )
    return _bundle(root, envelope), private


def test_a_sealed_credential_with_an_install_target_becomes_restorable(
    tmp_path: Path,
) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    by_name = {item.name: item for item in plan.credentials}
    assert by_name["codex-auth"].disposition == CREDENTIAL_RESTORABLE
    assert by_name["deepseek"].disposition == CREDENTIAL_RESTORABLE
    assert by_name["github"].disposition == CREDENTIAL_REAUTH_REQUIRED
    assert "machine-bound" in by_name["github"].notes
    assert plan.needs_reauth == ("github",)
    assert plan.payload == {"codex-auth": CODEX_AUTH, "deepseek": DEEPSEEK}
    # The receipt never carries a value, only dispositions and install targets.
    receipt = json.dumps([item.as_dict() for item in plan.credentials])
    assert MARKER not in receipt
    assert DEEPSEEK not in receipt


def test_apply_writes_the_native_file_and_the_env_entry(tmp_path: Path) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)
    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )
    home = tmp_path / "landed"

    result = apply_credential_landing(home, plan)

    auth = home / "secrets" / "native" / "codex" / "auth.json"
    assert auth.read_text("utf-8") == CODEX_AUTH
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    entry = home / "secrets" / "deepseek"
    assert AgentSecretStore(home, read_only=True).get("deepseek", "API_KEY").value == DEEPSEEK
    assert not entry.exists()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700

    by_name = {item.name: item for item in result.plan.credentials}
    assert by_name["codex-auth"].disposition == CREDENTIAL_RESTORED
    assert by_name["deepseek"].disposition == CREDENTIAL_RESTORED
    assert by_name["github"].disposition == CREDENTIAL_REAUTH_REQUIRED
    assert result.bindings == (
        CredentialBinding(
            entry_id="deepseek",
            environment=("DEEPSEEK_API_KEY",),
            field="API_KEY",
        ),
    )
    assert set(result.written) == {auth, home / "secrets.sqlite"}


def test_apply_credential_landing_refuses_symlinked_ancestor_directory(
    tmp_path: Path,
) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)
    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    empty_outside = tmp_path / "empty-outside"
    empty_outside.mkdir()
    home_one = tmp_path / "landed-one"
    home_one.mkdir()
    (home_one / "secrets").symlink_to(empty_outside)

    with pytest.raises(CredentialInstallError):
        apply_credential_landing(home_one, plan)
    assert list(empty_outside.iterdir()) == []

    nested_outside = tmp_path / "nested-outside"
    (nested_outside / "native").mkdir(parents=True)
    home_two = tmp_path / "landed-two"
    home_two.mkdir()
    (home_two / "secrets").symlink_to(nested_outside)

    with pytest.raises(CredentialInstallError):
        apply_credential_landing(home_two, plan)
    assert list((nested_outside / "native").iterdir()) == []


def test_apply_credential_landing_refuses_reparse_secrets_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = generate_keypair()
    envelope = _sealed(
        [
            CredentialRecord(
                name="deepseek",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("DEEPSEEK_API_KEY",),
                    field="API_KEY",
                ),
            ),
        ],
        recipient_public_key=public,
        secrets={"deepseek": DEEPSEEK},
    )
    bundle_dir = _bundle(tmp_path, envelope)
    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )
    home = tmp_path / "landed"
    home.mkdir(mode=0o700)
    sqlite_path = home / "secrets.sqlite"
    sqlite_path.write_bytes(b"")
    sqlite_path.chmod(0o600)
    sqlite_stat = sqlite_path.lstat()
    real_path_is_reparse = path_is_reparse

    def _fake_path_is_reparse(metadata: os.stat_result) -> bool:
        if not isinstance(metadata, os.stat_result):
            raise TypeError(f"expected os.stat_result, got {type(metadata)!r}")
        if (metadata.st_dev, metadata.st_ino) == (
            sqlite_stat.st_dev,
            sqlite_stat.st_ino,
        ):
            return True
        return real_path_is_reparse(metadata)

    monkeypatch.setattr(
        "hyprial.daemon.impl.transfer.landing.credentials.path_is_reparse",
        _fake_path_is_reparse,
    )

    with pytest.raises(CredentialInstallError, match="unsafe-file"):
        apply_credential_landing(home, plan)
    assert sqlite_path.read_bytes() == b""


def test_apply_normalizes_hyphenated_env_field_and_fallbacks(tmp_path: Path) -> None:
    private, public = generate_keypair()
    envelope = _sealed(
        [
            CredentialRecord(
                name="deepseek",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("DEEPSEEK_API_KEY",),
                    field="api-key",
                ),
            ),
            CredentialRecord(
                name="numeric",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("NUMERIC_API_KEY",),
                    field="1api-key",
                ),
            ),
            CredentialRecord(
                name="reserved",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("RESERVED_API_KEY",),
                    field="path",
                ),
            ),
            CredentialRecord(
                name="plain",
                machine_bound=False,
                install=CredentialInstall(
                    kind="env",
                    environment=("PLAIN_API_KEY",),
                    field=None,
                ),
            ),
        ],
        recipient_public_key=public,
        secrets={
            "deepseek": DEEPSEEK,
            "numeric": "sk-numeric-marker-value",
            "reserved": "sk-reserved-marker-value",
            "plain": "sk-plain-marker-value",
        },
    )
    bundle_dir = _bundle(tmp_path, envelope)
    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )
    home = tmp_path / "landed"

    result = apply_credential_landing(home, plan)

    store = AgentSecretStore(home, read_only=True)
    assert store.get("deepseek", "API_KEY").value == DEEPSEEK
    assert store.get("numeric", "VALUE").value == "sk-numeric-marker-value"
    assert store.get("reserved", "VALUE").value == "sk-reserved-marker-value"
    assert store.get("plain", "VALUE").value == "sk-plain-marker-value"
    assert result.bindings == (
        CredentialBinding(
            entry_id="deepseek",
            environment=("DEEPSEEK_API_KEY",),
            field="API_KEY",
        ),
        CredentialBinding(
            entry_id="numeric",
            environment=("NUMERIC_API_KEY",),
            field="VALUE",
        ),
        CredentialBinding(
            entry_id="reserved",
            environment=("RESERVED_API_KEY",),
            field="VALUE",
        ),
        CredentialBinding(
            entry_id="plain",
            environment=("PLAIN_API_KEY",),
            field=None,
        ),
    )


def test_a_first_phase_plan_installs_nothing(tmp_path: Path) -> None:
    bundle_dir, _private = _installable_bundle(tmp_path)
    plan = plan_credential_landing(load_manifest(bundle_dir), bundle_dir)
    home = tmp_path / "landed"

    result = apply_credential_landing(home, plan)

    assert result.written == ()
    assert result.bindings == ()
    assert result.plan.credentials == plan.credentials
    assert not home.exists()


def test_a_wrong_recipient_key_degrades_to_reauth_required(tmp_path: Path) -> None:
    bundle_dir, _private = _installable_bundle(tmp_path)
    other_private, _other_public = generate_keypair()

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=other_private
    )

    assert plan.needs_reauth == ("codex-auth", "deepseek", "github")
    assert plan.payload == {}
    assert all("could not be opened" in item.notes for item in plan.credentials)


def test_tampered_ciphertext_degrades_instead_of_crashing(tmp_path: Path) -> None:
    bundle_dir, private = _installable_bundle(tmp_path)
    path = bundle_dir / "payload" / ENVELOPE_PATH
    envelope = json.loads(path.read_text("utf-8"))
    envelope["ciphertext"] = base64.b64encode(b"not the real plaintext").decode()
    path.write_text(json.dumps(envelope, sort_keys=True) + "\n")

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    assert plan.needs_reauth == ("codex-auth", "deepseek", "github")
    assert plan.payload == {}


def test_a_granted_host_policy_installs_nothing_even_with_the_key(
    tmp_path: Path,
) -> None:
    bundle_dir, private = _installable_bundle(
        tmp_path,
        policy=CredentialPolicy(strategy="host", host_owner_grant="grant-9"),
    )

    plan = plan_credential_landing(
        load_manifest(bundle_dir), bundle_dir, recipient_key=private
    )

    assert plan.policy.strategy == "host"
    assert plan.payload == {}
    assert plan.needs_reauth == ("codex-auth", "deepseek", "github")


def test_duplicate_credential_names_are_refused(tmp_path: Path) -> None:
    _private, public = generate_keypair()
    envelope = _sealed(
        [CredentialRecord(name="github"), CredentialRecord(name="github")],
        recipient_public_key=public,
        secrets={"github": "ghp-x"},
    )
    bundle_dir = _bundle(tmp_path, envelope)

    with pytest.raises(CredentialEnvelopeInvalid):
        plan_credential_landing(load_manifest(bundle_dir), bundle_dir)


def test_install_metadata_round_trips_and_is_validated() -> None:
    native = CredentialInstall(kind="native-file", harness="codex", path="auth.json")
    assert CredentialInstall.from_dict(native.as_dict()) == native
    env = CredentialInstall(
        kind="env", environment=("DEEPSEEK_API_KEY",), field="api_key"
    )
    assert CredentialInstall.from_dict(env.as_dict()) == env

    with pytest.raises(EnvelopeFormatError):
        CredentialInstall.from_dict(
            {"kind": "native-file", "harness": "codex", "path": "../escape"}
        )
    with pytest.raises(EnvelopeFormatError):
        CredentialInstall.from_dict({"kind": "env", "environment": ["lowercase"]})
    with pytest.raises(EnvelopeFormatError):
        CredentialInstall.from_dict({"kind": "unknown"})
    with pytest.raises(EnvelopeFormatError):
        CredentialRecord.from_dict(
            {
                "name": "bad/name",
                "install": {"kind": "env", "environment": ["DEEPSEEK_API_KEY"]},
            }
        )
