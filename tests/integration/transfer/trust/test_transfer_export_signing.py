"""AT06 slice 2c: the export side mints what the landing gate verifies.

The gate (slice 2b) refuses a bundle it cannot attribute; this is the other
half -- the machine key's own stamp, written beside the manifest at export and
covering the sealed manifest digest (plus the declared credential envelope's
header when the bundle carries one).  The arms below pin the wire shape the
gate consumes, and the two ways an export refuses instead of shipping a box
nobody can attest to.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from hyprial.kernel import configured_hyprial_home

from hyprial.daemon.impl.transfer.archive.attribution import SIGNATURE_FILENAME
from hyprial.daemon.impl.transfer.archive.envelope import (
    envelope_header_digest,
    generate_keypair,
    seal_envelope,
)
from hyprial.daemon.impl.transfer.archive.signature import (
    public_key_id,
    read_signing_key,
    signing_key_path,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "owner@example.com"
ACTOR = "codex-sw"
MACHINE = "devusr-dev"


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"model": "x"}\n')
    return home


def _export(tmp_path: Path, source: Path, extra: list[str] | None = None):
    runner = CliRunner()
    bundle_dir = tmp_path / "bundle"
    result = runner.invoke(
        _cli_root_app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(source),
            "--to",
            str(bundle_dir),
            "--owner",
            OWNER,
            "--actor",
            ACTOR,
            "--machine",
            MACHINE,
            "--json",
            *(extra or []),
        ],
    )
    return runner, bundle_dir, result


def _block(bundle_dir: Path) -> dict[str, object]:
    return json.loads((bundle_dir / SIGNATURE_FILENAME).read_text(encoding="utf-8"))


def test_a_standalone_export_stamps_the_sealed_digest(tmp_path: Path) -> None:
    source = _source_home(tmp_path)

    _runner, bundle_dir, result = _export(tmp_path, source)

    assert result.exit_code == 0, result.output
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    block = _block(bundle_dir)
    assert block["digest"] == f"sha256:{manifest['manifest_digest']}"
    assert block["algorithm"] == "ed25519"
    assert block["headerDigest"] is None
    # The key is the exporting machine's own, read back from its home -- the
    # same home the CLI resolved, not a value the caller could supply.
    _private_bytes, public_bytes = read_signing_key(
        signing_key_path(Path(configured_hyprial_home()[0]))
    )
    assert block["keyId"] == public_key_id(public_bytes)
    # ... and the receipt the CLI prints names the same key.
    assert json.loads(result.stdout)["signature"]["keyId"] == block["keyId"]


def test_the_stamp_binds_the_declared_envelope_header(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    _sender_private, recipient_public = generate_keypair()
    (source / "config" / "envelope.json").write_text(
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

    _runner, bundle_dir, result = _export(
        tmp_path, source, ["--credential-envelope", "config/envelope.json"]
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(
        (bundle_dir / "payload" / "config" / "envelope.json").read_text(
            encoding="utf-8"
        )
    )
    block = _block(bundle_dir)
    assert block["headerDigest"] == envelope_header_digest(envelope)
    assert block["headerDigest"] is not None


def test_an_export_refuses_a_declaration_it_cannot_bind(tmp_path: Path) -> None:
    """A "credential envelope" that is not one has no header to sign.

    The export refuses and leaves no stamp; the half-exported directory stays
    (the exporter never removes a destination it was told to fill), but nothing
    about it claims an attribution that could not be computed.
    """

    source = _source_home(tmp_path)

    _runner, bundle_dir, result = _export(
        tmp_path, source, ["--credential-envelope", "config/agent.json"]
    )

    assert result.exit_code != 0
    assert "credential envelope" in result.output
    assert "config/agent.json" in result.output
    assert not (bundle_dir / SIGNATURE_FILENAME).exists()


def test_the_machine_key_stays_outside_the_bundle(tmp_path: Path) -> None:
    """The stamp's own key must never travel inside the bytes it vouches for."""

    source = _source_home(tmp_path)
    (source / "secrets").mkdir()
    (source / "secrets" / "token.json").write_text('{"token": "synthetic"}\n')

    _runner, bundle_dir, result = _export(tmp_path, source)

    assert result.exit_code == 0, result.output
    payload = bundle_dir / "payload"
    assert (payload / "secrets" / "token.json").is_file()
    assert not (payload / "secrets" / "transfer" / "signing-key.json").exists()
    # The private key lives in the exporting machine's home instead.
    home_key = signing_key_path(Path(configured_hyprial_home()[0]))
    assert home_key.is_file()
    record = json.loads(home_key.read_text(encoding="utf-8"))
    assert "privateKey" in record
