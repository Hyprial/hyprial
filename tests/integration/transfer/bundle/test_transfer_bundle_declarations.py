"""AT06: declared dependencies and the credential envelope are enforced.

Card 355 item 1 asks the manifest to declare what the bundle does *not* carry
(an external config or cwd the target must supply, a platform binary), and
item 4 asks the pre-receive gate to refuse a bundle whose declared file is
missing instead of degrading into "this bundle simply has no credentials".
"""

from __future__ import annotations

import json

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundleDeclarationMissing,
    export_bundle,
    load_manifest,
    validate_bundle,
    write_manifest)
from hyprial.daemon.impl.transfer.archive.envelope import (
    generate_keypair,
    seal_envelope,
)
from hyprial.daemon.impl.transfer.landing.receive import receive_bundle
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "owner@example.com"
ACTOR = "codex-sw"
CREATED_AT = "2026-10-01T01:00:00+08:00"

#: A file that looks like an ELF binary: the export must not carry it.
_ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 24


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    (home / "bin").mkdir()
    (home / "bin" / "tool").write_bytes(_ELF)
    return home


def test_a_platform_binary_is_declared_not_carried(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"

    manifest = export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
    )

    assert "bin/tool" not in {entry.path for entry in manifest.entries}
    assert not (bundle_dir / "payload" / "bin" / "tool").exists()
    carried = {item["path"] for item in manifest.dependencies}
    assert carried == {"bin/tool"}
    binary = next(item for item in manifest.target_must_provide())
    assert binary["kind"] == "binary" and binary["present"] is False
    assert binary["path"] == "bin/tool"
    assert "not carried" in str(binary["note"])
    validate_bundle(bundle_dir)

    # The receive receipt repeats what the target has to provide itself.
    receipt = receive_bundle(
        bundle_dir,
        tmp_path / "landed",
        dry_run=True,
        signature=UNSIGNED_REHEARSAL,
    ).as_dict()
    assert receipt["target_must_provide"] == [dict(binary)]


def test_a_declared_dependency_the_bundle_lacks_refuses_validation(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        dependencies=(
            {"kind": "external-config", "present": True, "path": "config/agent.json"},
        ),
    )

    manifest = load_manifest(bundle_dir)
    assert manifest.dependencies[0]["kind"] == "external-config"

    # Present in the bundle: fine.  Swap it for a path nobody carries: refuse.
    validate_bundle(bundle_dir)
    write_manifest(
        bundle_dir,
        replace(
            manifest,
            dependencies=(
                {"kind": "external-config", "present": True, "path": "cwd/notes.md"},
            ),
        ),
    )

    with pytest.raises(BundleDeclarationMissing) as refused:
        validate_bundle(bundle_dir)

    assert refused.value.code == "bundle_declaration_missing"
    assert "cwd/notes.md" in str(refused.value)


def test_a_credential_envelope_has_to_be_where_the_manifest_says(
    tmp_path: Path,
) -> None:
    # A home with no platform binary: this case is about the declarations the
    # operator adds, not the one the export adds for a binary it skips.
    home = tmp_path / "source"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    # A declared credential envelope has to be one the exporter can bind: the
    # signature covers the envelope's authenticated header digest, so a
    # declaration that names something which is not a sealed envelope refuses
    # at export instead of shipping a box nobody can attest to (slice 2c).
    _sender_private, recipient_public = generate_keypair()
    (home / "config" / "envelope.json").write_text(
        json.dumps(
            seal_envelope(
                {"github": "secret-that-stays-sealed"},
                recipient_public_key=recipient_public,
                actor=ACTOR,
                owner=OWNER,
                created_at="2026-09-30T22:05:00+08:00",
            )
        )
        + "\n"
    )
    runner = CliRunner()
    bundle_dir = tmp_path / "bundle"

    exported = runner.invoke(
        _cli_root_app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(home),
            "--to",
            str(bundle_dir),
            "--owner",
            OWNER,
            "--actor",
            ACTOR,
            "--machine",
            "laptop",
            "--credential-envelope",
            "config/envelope.json",
            "--depends",
            "external-config=config/agent.json",
            "--depends",
            "binary=!ripgrep",
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    payload = json.loads(exported.stdout)
    assert payload["credential_envelope"] == "config/envelope.json"
    assert [item["kind"] for item in payload["target_must_provide"]] == ["binary"]

    # Declaring an envelope the bundle cannot honour refuses the export ...
    refused = runner.invoke(
        _cli_root_app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(home),
            "--to",
            str(tmp_path / "bundle-2"),
            "--owner",
            OWNER,
            "--actor",
            ACTOR,
            "--machine",
            "laptop",
            "--credential-envelope",
            "config/missing.json",
        ],
    )
    assert refused.exit_code != 0
    assert "credential envelope" in refused.output
    assert "config/missing.json" in refused.output

    # ... and a bundle that loses the file after export is refused on receipt.
    manifest = load_manifest(bundle_dir)
    write_manifest(
        bundle_dir,
        replace(manifest, credential_envelope="credentials/envelope.json"),
    )
    with pytest.raises(BundleDeclarationMissing) as unregistered:
        validate_bundle(bundle_dir)

    # A bundle-root path is not a registered payload entry: nothing hashed it,
    # so a declaration may not point at one.
    assert "not a registered payload entry" in str(unregistered.value)
