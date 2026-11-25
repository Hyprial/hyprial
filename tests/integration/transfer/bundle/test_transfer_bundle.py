"""AT06: bundle export / pre-receive validation (red-first mutants)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest


from hyprial.daemon.impl.transfer.archive.bundle import (
    BUNDLE_SCHEMA_VERSION,
    BundleManifestError,
    BundlePathError,
    BundleProtocolMismatch,
    BundleTamperError,
    export_bundle,
    load_manifest,
    validate_bundle)

CREATED_AT = "2026-09-30T21:55:00+08:00"


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "agent-home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"actor": "demo"}\n')
    (home / "notes.md").write_text("hello bundle\n")
    (home / "secrets").mkdir()
    (home / "secrets" / "private.json").write_text('{"token": "synthetic"}\n')
    (home / "__pycache__").mkdir()
    (home / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x01")
    (home / "user-provider.json").write_text('{"host": "projection"}\n')
    return home


def _export(tmp_path: Path, **overrides):
    source = overrides.pop("source", None) or _source_home(tmp_path)
    bundle_dir = overrides.pop("bundle_dir", None) or (tmp_path / "bundle")
    kwargs = dict(
        owner="amy.williams@example.com",
        actor="codex-sw",
        source_machine="devusr-dev",
        created_at=CREATED_AT,
        session_refs=["session-1"],
        grants=["tool-surface"],
    )
    kwargs.update(overrides)
    manifest = export_bundle(source, bundle_dir, **kwargs)
    return source, bundle_dir, manifest


def test_export_then_validate_roundtrip(tmp_path: Path) -> None:
    source, bundle_dir, manifest = _export(tmp_path)

    paths = sorted(entry.path for entry in manifest.entries)
    assert paths == [
        "config/agent.json",
        "notes.md",
        "secrets/private.json",
    ]

    validated = validate_bundle(bundle_dir)
    assert validated.actor == "codex-sw"
    assert validated.owner == "amy.williams@example.com"
    assert validated.source_machine == "devusr-dev"
    assert validated.created_at == CREATED_AT
    assert validated.schema_version == BUNDLE_SCHEMA_VERSION
    assert validated.session_refs == ("session-1",)
    assert validated.grants == ("tool-surface",)
    assert validated.entries == manifest.entries

    # The payload is a plain copy, and the excluded projections stayed home.
    assert (bundle_dir / "payload" / "notes.md").read_text() == "hello bundle\n"
    assert not (bundle_dir / "payload" / "user-provider.json").exists()
    assert not (bundle_dir / "payload" / "__pycache__").exists()
    assert (source / "user-provider.json").exists()


def test_export_never_carries_the_agent_secret_store(tmp_path: Path) -> None:
    """The per-agent credential DB, its key and SQLite sidecars stay home.

    ``agent ensure-home`` now materialises an empty ``secrets.sqlite``; a
    bundle that carried it would ship plaintext credentials to another host
    and plant a root-level entry the landing topology refuses.
    """

    source = _source_home(tmp_path)
    secret_files = {
        "secrets.sqlite": b"SQLite format 3\x00synthetic",
        ".secret-hmac-key": b"K" * 32,
        "secrets.sqlite-wal": b"synthetic-wal",
        "secrets.sqlite-shm": b"synthetic-shm",
        "secret-metrics.jsonl": b"{}\n",
    }
    for name, body in secret_files.items():
        (source / name).write_bytes(body)
    (source / "workspace").mkdir()
    (source / "workspace" / "secrets.sqlite").write_bytes(b"workspace user file")

    _, bundle_dir, manifest = _export(tmp_path, source=source)

    assert sorted(entry.path for entry in manifest.entries) == [
        "config/agent.json",
        "notes.md",
        "secrets/private.json",
        "workspace/secrets.sqlite",
    ]
    for name in secret_files:
        assert not (bundle_dir / "payload" / name).exists()
        assert (source / name).exists()
    assert (bundle_dir / "payload" / "workspace" / "secrets.sqlite").is_file()


def test_manifest_json_is_sorted_and_stable(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    raw = json.loads((bundle_dir / "manifest.json").read_text())
    assert raw["entries"] == sorted(raw["entries"], key=lambda item: item["path"])
    again = json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False)
    assert again == (bundle_dir / "manifest.json").read_text().rstrip("\n")


def test_tampered_byte_is_refused(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    target = bundle_dir / "payload" / "notes.md"
    target.write_text("hello bundlX\n")

    with pytest.raises(BundleTamperError) as error:
        validate_bundle(bundle_dir)
    assert "notes.md" in str(error.value)


def test_missing_payload_file_is_refused(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    (bundle_dir / "payload" / "notes.md").unlink()

    with pytest.raises(BundleManifestError):
        validate_bundle(bundle_dir)


def test_unregistered_payload_file_is_refused(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    (bundle_dir / "payload" / "stowaway.txt").write_text("not in manifest\n")

    with pytest.raises(BundleManifestError) as error:
        validate_bundle(bundle_dir)
    assert "stowaway.txt" in str(error.value)


def test_mode_change_is_refused(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    target = bundle_dir / "payload" / "notes.md"
    os.chmod(target, stat.S_IMODE(target.stat().st_mode) ^ 0o111)

    with pytest.raises(BundleTamperError):
        validate_bundle(bundle_dir)


def test_symlink_payload_entry_is_refused(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    link = bundle_dir / "payload" / "link.md"
    link.symlink_to(bundle_dir / "payload" / "notes.md")

    with pytest.raises(BundlePathError):
        validate_bundle(bundle_dir)


def test_symlink_in_source_is_refused(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    (source / "link.md").symlink_to(source / "notes.md")

    with pytest.raises(BundlePathError):
        export_bundle(
            source,
            tmp_path / "bundle",
            owner="owner",
            actor="actor",
            source_machine="machine",
            created_at=CREATED_AT,
        )


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.md", "/etc/passwd", "config/../../escape.md", "a\\b.md", ""],
)
def test_manifest_rejects_unsafe_paths(tmp_path: Path, bad_path: str) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    raw = json.loads((bundle_dir / "manifest.json").read_text())
    raw["entries"].append(
        {"path": bad_path, "size": 0, "sha256": "0" * 64, "mode": 0o644}
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(BundlePathError):
        validate_bundle(bundle_dir)


def test_manifest_rejects_unsupported_protocol(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    raw = json.loads((bundle_dir / "manifest.json").read_text())
    raw["protocol_version"] = 99
    (bundle_dir / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(BundleProtocolMismatch):
        validate_bundle(bundle_dir)


def test_manifest_rejects_unknown_schema(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    raw = json.loads((bundle_dir / "manifest.json").read_text())
    raw["schema_version"] = 7
    (bundle_dir / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(BundleManifestError):
        validate_bundle(bundle_dir)


def test_manifest_rejects_duplicate_entries(tmp_path: Path) -> None:
    _, bundle_dir, _ = _export(tmp_path)
    raw = json.loads((bundle_dir / "manifest.json").read_text())
    raw["entries"].append(dict(raw["entries"][0]))
    (bundle_dir / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(BundleManifestError) as error:
        load_manifest(bundle_dir)
    assert "duplicate" in str(error.value)


def test_missing_manifest_is_refused(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        source,
        bundle_dir,
        owner="owner",
        actor="actor",
        source_machine="machine",
        created_at=CREATED_AT,
    )
    (bundle_dir / "manifest.json").unlink()

    with pytest.raises(BundleManifestError):
        validate_bundle(bundle_dir)


def test_export_refuses_unknown_protocol(tmp_path: Path) -> None:
    with pytest.raises(BundleProtocolMismatch):
        export_bundle(
            _source_home(tmp_path),
            tmp_path / "bundle",
            owner="owner",
            actor="actor",
            source_machine="machine",
            created_at=CREATED_AT,
            protocol_version=42,
        )


# -- production entry points -------------------------------------------------


def test_export_refuses_a_live_source_home(tmp_path: Path) -> None:
    import socket

    from hyprial.daemon.impl.transfer.archive.bundle import BundleSourceLive

    # AF_UNIX paths are short; keep the socket off the deep pytest tmp_path.
    short_root = Path(tempfile.mkdtemp(prefix="at06-live-"))
    source = _source_home(short_root)
    (source / "state").mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(source / "state" / "daemon.sock"))
        with pytest.raises(BundleSourceLive) as caught:
            export_bundle(
                source,
                tmp_path / "bundle",
                owner="owner@example.com",
                actor="codex-sw",
                source_machine="devusr-dev",
                created_at=CREATED_AT,
            )
        assert caught.value.code == "bundle_source_live"
        assert not (tmp_path / "bundle").exists()

        manifest = export_bundle(
            source,
            tmp_path / "bundle-live",
            owner="owner@example.com",
            actor="codex-sw",
            source_machine="devusr-dev",
            created_at=CREATED_AT,
            allow_live=True,
        )
        assert len(manifest.entries) == 3
    finally:
        server.close()


def test_export_detects_a_source_change_during_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from hyprial.daemon.impl.transfer.archive.bundle import BundleSourceChanged

    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"

    real_copy = shutil.copyfile
    calls = {"count": 0}

    def racing_copy(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_copy(src, dst, *args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            (source / "notes.md").write_text("changed while packing\n")
        return result

    monkeypatch.setattr(shutil, "copyfile", racing_copy)

    with pytest.raises(BundleSourceChanged) as caught:
        export_bundle(
            source,
            bundle_dir,
            owner="owner@example.com",
            actor="codex-sw",
            source_machine="devusr-dev",
            created_at=CREATED_AT,
        )

    assert caught.value.code == "bundle_source_changed"
    assert not bundle_dir.exists()


def test_export_refuses_identical_source_and_destination(tmp_path: Path) -> None:
    from hyprial.daemon.impl.transfer.archive.bundle import BundleDestinationOverlapsSource

    home = _source_home(tmp_path)

    with pytest.raises(BundleDestinationOverlapsSource) as caught:
        export_bundle(
            home,
            home,
            owner="owner@example.com",
            actor="codex-sw",
            source_machine="devusr-dev",
            created_at=CREATED_AT,
        )

    assert caught.value.code == "bundle_destination_overlaps_source"
    assert (home / "notes.md").read_text() == "hello bundle\n"
    assert (home / "config" / "agent.json").exists()


def test_export_refuses_a_destination_that_contains_the_source(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.transfer.archive.bundle import BundleDestinationOverlapsSource

    parent = tmp_path / "agents"
    home = parent / "codex-sw"
    home.mkdir(parents=True)
    (home / "notes.md").write_text("mine\n")
    neighbour = parent / "other"
    neighbour.mkdir()
    (neighbour / "keep.md").write_text("not yours\n")

    with pytest.raises(BundleDestinationOverlapsSource):
        export_bundle(
            home,
            parent,
            owner="owner@example.com",
            actor="codex-sw",
            source_machine="devusr-dev",
            created_at=CREATED_AT,
        )

    assert (home / "notes.md").read_text() == "mine\n"
    assert (neighbour / "keep.md").read_text() == "not yours\n"


def test_export_refuses_a_destination_inside_the_source(tmp_path: Path) -> None:
    from hyprial.daemon.impl.transfer.archive.bundle import BundleDestinationOverlapsSource

    home = _source_home(tmp_path)

    with pytest.raises(BundleDestinationOverlapsSource):
        export_bundle(
            home,
            home / "bundle",
            owner="owner@example.com",
            actor="codex-sw",
            source_machine="devusr-dev",
            created_at=CREATED_AT,
        )

    assert not (home / "bundle").exists()
    assert (home / "notes.md").exists()


def test_export_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    from hyprial.daemon.impl.transfer.archive.bundle import BundleDestinationNotEmpty

    home = _source_home(tmp_path)
    dest = tmp_path / "unrelated"
    dest.mkdir()
    (dest / "precious.txt").write_text("do not delete me\n")

    with pytest.raises(BundleDestinationNotEmpty) as caught:
        export_bundle(
            home,
            dest,
            owner="owner@example.com",
            actor="codex-sw",
            source_machine="devusr-dev",
            created_at=CREATED_AT,
        )

    assert caught.value.code == "bundle_destination_not_empty"
    assert (dest / "precious.txt").read_text() == "do not delete me\n"


def test_export_accepts_an_empty_destination_directory(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    dest = tmp_path / "empty"
    dest.mkdir()

    manifest = export_bundle(
        home,
        dest,
        owner="owner@example.com",
        actor="codex-sw",
        source_machine="devusr-dev",
        created_at=CREATED_AT,
    )

    assert len(manifest.entries) == 3
    assert (dest / "manifest.json").exists()
