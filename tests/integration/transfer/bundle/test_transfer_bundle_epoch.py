"""AT06 item 2: the bundle carries its source incarnation as a digest only.

The manifest has to say *which generation* of the agent it came from, and it
must be able to say so without ever carrying the entity token.  An offline
export has nothing to read, so it writes nothing -- and an ``--expect-epoch``
check must refuse that instead of treating "no epoch" as a match.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundleEpochMismatch,
    BundleManifest,
    BundleManifestError,
    _manifest_digest,
    assert_bundle_identity,
    export_bundle)
from hyprial.daemon.impl.transfer.landing.receive import receive_bundle
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "owner@example.com"
ACTOR = "codex-sw"
CREATED_AT = "2026-10-01T02:00:00+08:00"
TOKEN = "entity-token-6f2a1c"


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    return home


def test_the_manifest_keeps_only_a_digest_of_the_source_incarnation(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"

    manifest = export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        source_epoch=TOKEN,
    )

    assert manifest.source_epoch == hashlib.sha256(TOKEN.encode()).hexdigest()[:16]

    # The token is nowhere in the bundle, not even in the manifest we wrote.
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path

    assert_bundle_identity(manifest, expect_epoch=TOKEN)  # the same generation
    with pytest.raises(BundleEpochMismatch) as other:
        assert_bundle_identity(manifest, expect_epoch="another-incarnation")
    assert other.value.code == "bundle_epoch_mismatch"

    # The receipt repeats the digest, so the target can record the generation.
    receipt = receive_bundle(
        bundle_dir,
        tmp_path / "landed",
        dry_run=True,
        signature=UNSIGNED_REHEARSAL,
    )
    assert receipt.manifest.source_epoch == manifest.source_epoch


def test_an_offline_export_never_matches_an_expected_epoch(tmp_path: Path) -> None:
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

    assert manifest.source_epoch == ""
    assert "source_epoch" not in json.loads((bundle_dir / "manifest.json").read_text())
    with pytest.raises(BundleEpochMismatch) as refused:
        assert_bundle_identity(manifest, expect_epoch=TOKEN)
    assert "no source epoch" in str(refused.value)

    via_cli = CliRunner().invoke(
        _cli_root_app,
        ["agent", "transfer", "bundle-validate", str(bundle_dir), "--expect-epoch", TOKEN, "--json"],
    )
    assert via_cli.exit_code != 0
    assert "bundle_epoch_mismatch" in via_cli.output


def test_an_identity_bag_may_not_contradict_the_manifest(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    manifest = export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine="laptop",
        created_at=CREATED_AT,
        identity={"owner": OWNER, "actor": ACTOR, "epoch_note": "same generation"},
    )

    # A consistent bag parses (the bag is free-form, the fields are not).
    assert manifest.identity["epoch_note"] == "same generation"

    raw = dict(manifest.as_dict())
    raw["identity"] = {"owner": "someone-else"}
    raw["manifest_digest"] = _manifest_digest(raw)

    with pytest.raises(BundleManifestError) as refused:
        BundleManifest.from_dict(raw)

    assert "contradicts" in str(refused.value)
    assert "someone-else" in str(refused.value)
