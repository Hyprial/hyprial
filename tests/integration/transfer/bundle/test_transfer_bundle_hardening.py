"""AT06 hardening: host-owned exclusions, manifest seal, expected identity.

Card 355 items 1 and 4 ask the export to keep host-owned paths out of a
bundle and a production entry point to refuse a wrong owner, a missing
dependency and an old version.  These tests cover the gaps the first slice
left:

* the operator's HOME and Keychain never ride a bundle;
* the manifest is sealed, so editing ``owner``/``actor``/``entries`` after
  export is refused instead of silently accepted;
* ``transfer-bundle-validate`` refuses a bundle whose claimed identity is not
  the one the operator asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.transfer.archive.bundle import (
    BUNDLE_SCHEMA_VERSION,
    BundleActorMismatch,
    BundleManifestError,
    BundleOwnerMismatch,
    BundlePathError,
    BundleTamperError,
    assert_bundle_identity,
    export_bundle,
    load_manifest,
    validate_bundle)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

CREATED_AT = "2026-09-30T22:40:00+08:00"


def _home(tmp_path: Path, name: str = "agent-home") -> Path:
    home = tmp_path / name
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"actor": "demo"}\n')
    (home / "notes.md").write_text("hello bundle\n")
    return home


def _export(source: Path, bundle_dir: Path, **overrides):
    kwargs = dict(
        owner="owner@example.com",
        actor="demo",
        source_machine="dev",
        created_at=CREATED_AT,
    )
    kwargs.update(overrides)
    return export_bundle(source, bundle_dir, **kwargs)


# -- host-owned paths (card 355 item 1) --------------------------------------


def test_the_operator_keychain_is_never_exported(tmp_path: Path) -> None:
    home = _home(tmp_path)
    keychain = home / "Library" / "Keychains"
    keychain.mkdir(parents=True)
    (keychain / "login.keychain-db").write_bytes(b"keychain-bytes")
    (home / "Library" / "Preferences").mkdir()
    (home / "Library" / "Preferences" / "keep.txt").write_text("agent's own\n")

    bundle_dir = tmp_path / "bundle"
    manifest = _export(home, bundle_dir)

    paths = sorted(entry.path for entry in manifest.entries)
    assert paths == [
        "Library/Preferences/keep.txt",
        "config/agent.json",
        "notes.md",
    ]
    assert not (bundle_dir / "payload" / "Library" / "Keychains").exists()


def test_the_host_home_is_never_a_bundle_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / "notes.md").write_text("operator's own\n")
    monkeypatch.setenv("HOME", str(host_home))

    with pytest.raises(BundlePathError):
        _export(host_home, tmp_path / "bundle-1")

    # An ancestor of the host HOME would carry it too.
    with pytest.raises(BundlePathError):
        _export(tmp_path, tmp_path / "bundle-2")

    # An agent home *below* the host HOME is still exportable.
    nested = host_home / "agents" / "demo"
    nested.mkdir(parents=True)
    (nested / "notes.md").write_text("agent's own\n")
    manifest = _export(nested, tmp_path / "bundle-3")
    assert [entry.path for entry in manifest.entries] == ["notes.md"]


# -- the manifest is sealed (card 355 item 2) --------------------------------


def test_an_edited_identity_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    _export(home, bundle_dir)

    manifest_path = bundle_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["actor"] = "someone-else"
    manifest_path.write_text(json.dumps(raw, indent=2, sort_keys=True), "utf-8")

    with pytest.raises(BundleTamperError):
        validate_bundle(bundle_dir)


def test_a_manifest_without_a_seal_is_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    _export(home, bundle_dir)

    manifest_path = bundle_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    del raw["manifest_digest"]
    manifest_path.write_text(json.dumps(raw, indent=2, sort_keys=True), "utf-8")

    with pytest.raises(BundleManifestError):
        validate_bundle(bundle_dir)


def test_a_round_trip_manifest_carries_its_seal(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    manifest = _export(home, bundle_dir)

    assert manifest.schema_version == BUNDLE_SCHEMA_VERSION
    raw = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert raw["manifest_digest"]
    reloaded = load_manifest(bundle_dir)
    assert reloaded == manifest
    assert validate_bundle(bundle_dir) == manifest


# -- expected identity at the pre-receive gate (card 355 item 4) -------------


def test_assert_bundle_identity_refuses_a_wrong_owner_and_actor(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    manifest = _export(home, tmp_path / "bundle")

    assert_bundle_identity(manifest, expect_owner="owner@example.com", expect_actor="demo")

    with pytest.raises(BundleOwnerMismatch):
        assert_bundle_identity(manifest, expect_owner="other@example.com")
    with pytest.raises(BundleActorMismatch):
        assert_bundle_identity(manifest, expect_actor="other")


def test_validate_cli_refuses_a_wrong_owner(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    _export(home, bundle_dir)
    runner = CliRunner()

    ok = runner.invoke(
        _cli_root_app,
        [
            "agent", "transfer", "bundle-validate",
            str(bundle_dir),
            "--expect-owner",
            "owner@example.com",
            "--json",
        ],
    )
    assert ok.exit_code == 0, ok.stdout
    assert json.loads(ok.stdout)["ok"] is True

    wrong = runner.invoke(
        _cli_root_app,
        [
            "agent", "transfer", "bundle-validate",
            str(bundle_dir),
            "--expect-owner",
            "other@example.com",
            "--json",
        ],
    )
    assert wrong.exit_code != 0
    assert json.loads(wrong.stdout)["code"] == "bundle_owner_mismatch"

    wrong_actor = runner.invoke(
        _cli_root_app,
        [
            "agent", "transfer", "bundle-validate",
            str(bundle_dir),
            "--expect-actor",
            "other",
            "--json",
        ],
    )
    assert wrong_actor.exit_code != 0
    assert json.loads(wrong_actor.stdout)["code"] == "bundle_actor_mismatch"


def test_historical_unsealed_schema_one_bundle_is_explicitly_refused(tmp_path):
    bundle_dir = tmp_path / "bundle"
    _export(_home(tmp_path), bundle_dir)
    manifest_path = bundle_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["schema_version"] = 1
    raw.pop("manifest_digest")
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(BundleManifestError, match="unsupported schema_version: 1"):
        validate_bundle(bundle_dir)
