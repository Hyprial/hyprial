"""AT06: bundle export / pre-receive validation (red-first mutants)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

import hyprial.kernel.impl.primitives.node_identity as node_identity_module

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


def test_cli_export_and_validate_roundtrip(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "cli-bundle"
    runner = CliRunner()

    exported = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(source),
            "--to",
            str(bundle_dir),
            "--owner",
            "owner@example.com",
            "--actor",
            "codex-sw",
            "--machine",
            "devusr-dev",
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    payload = json.loads(exported.stdout)
    assert payload["entries"] == 3
    assert payload["protocol_version"] == 1
    assert payload["source_machine"] == "devusr-dev"

    validated = runner.invoke(
        cli.app, ["agent", "transfer", "bundle-validate", str(bundle_dir), "--json"]
    )
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.stdout)["ok"] is True


def test_cli_validate_refuses_tampered_bundle(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "cli-bundle"
    runner = CliRunner()
    assert (
        runner.invoke(
            cli.app,
            [
                "agent", "transfer", "bundle-export",
                "--from",
                str(source),
                "--to",
                str(bundle_dir),
                "--owner",
                "owner@example.com",
                "--actor",
                "codex-sw",
                "--json",
            ],
        ).exit_code
        == 0
    )

    (bundle_dir / "payload" / "notes.md").write_text("tampered\n")
    refused = runner.invoke(
        cli.app, ["agent", "transfer", "bundle-validate", str(bundle_dir), "--json"]
    )
    assert refused.exit_code != 0
    body = json.loads(refused.stdout)
    assert body["ok"] is False
    assert body["code"] == "bundle_tampered"


def test_cli_export_records_session_refs_and_grants(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "cli-bundle"
    runner = CliRunner()

    exported = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(source),
            "--to",
            str(bundle_dir),
            "--owner",
            "owner@example.com",
            "--actor",
            "codex-sw",
            "--session-ref",
            "session-1",
            "--session-ref",
            "session-2",
            "--grant",
            "tool-surface",
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    payload = json.loads(exported.stdout)
    assert payload["session_refs"] == ["session-1", "session-2"]
    assert payload["grants"] == ["tool-surface"]

    validated = runner.invoke(
        cli.app, ["agent", "transfer", "bundle-validate", str(bundle_dir), "--json"]
    )
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.stdout)["ok"] is True


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


def test_cli_export_passes_allow_live(tmp_path: Path) -> None:
    import socket

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    short_root = Path(tempfile.mkdtemp(prefix="at06-live-"))
    source = _source_home(short_root)
    (source / "state").mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    runner = CliRunner()
    try:
        server.bind(str(source / "state" / "daemon.sock"))
        blocked = runner.invoke(
            cli.app,
            [
                "agent", "transfer", "bundle-export",
                "--from",
                str(source),
                "--to",
                str(tmp_path / "blocked"),
                "--owner",
                "owner@example.com",
                "--actor",
                "codex-sw",
            ],
        )
        assert blocked.exit_code != 0

        allowed = runner.invoke(
            cli.app,
            [
                "agent", "transfer", "bundle-export",
                "--from",
                str(source),
                "--to",
                str(tmp_path / "allowed"),
                "--owner",
                "owner@example.com",
                "--actor",
                "codex-sw",
                "--allow-live",
                "--json",
            ],
        )
        assert allowed.exit_code == 0, allowed.output
        assert json.loads(allowed.stdout)["allow_live"] is True
    finally:
        server.close()


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


def test_cli_export_refuses_to_clobber_a_destination(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    home = _source_home(tmp_path)
    dest = tmp_path / "unrelated"
    dest.mkdir()
    (dest / "precious.txt").write_text("keep\n")
    runner = CliRunner()

    refused = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(home),
            "--to",
            str(dest),
            "--owner",
            "owner@example.com",
            "--actor",
            "codex-sw",
            "--json",
        ],
    )

    assert refused.exit_code != 0
    assert json.loads(refused.stdout)["code"] == "bundle_destination_not_empty"
    assert (dest / "precious.txt").read_text() == "keep\n"


def test_cli_export_strips_whitespace_node_id_before_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    home = _source_home(tmp_path)
    dest = tmp_path / "bundle"
    monkeypatch.setenv("HYPRIAL_NODE_ID", " \t ")
    monkeypatch.setattr(
        node_identity_module.socket, "gethostname", lambda: " transfer-host "
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(home),
            "--to",
            str(dest),
            "--owner",
            "owner@example.com",
            "--actor",
            "codex-sw",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert load_manifest(dest).source_machine == "transfer-host"
