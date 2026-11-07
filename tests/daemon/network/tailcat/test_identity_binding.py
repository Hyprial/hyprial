"""F6: a Tailcat identity is stable on one machine and heals copied homes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import hyprial.daemon.impl.network.tailcat.keys as keys_module
from hyprial.daemon.impl.network.tailcat import (
    BINARY_ENV,
    ensure_device_key,
    reissue_device_key,
)
from hyprial.identity import machine_binding_path, read_device_record


def _rotating_genkey(tmp_path: Path) -> Path:
    """Fake genkey: ``--force`` returns distinct public halves, never secrets."""

    script = tmp_path / "hyprial-tailcat"
    script.write_text(
        "#!/bin/sh\n"
        "FORCE=0\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    --out) OUT="$2"; shift 2;;\n'
        "    --force) FORCE=1; shift;;\n"
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        'if [ "$FORCE" = 1 ]; then N=rotated; CODE=0; '
        'else N=original; [ -f "$OUT" ] && CODE=3 || CODE=0; fi\n'
        'printf \'{"v":3}\' > "$OUT"\n'
        'chmod 600 "$OUT"\n'
        'printf \'{"v":6,"serverPublic":"nodekey:server-%s",'
        '"clientPublic":"nodekey:client-%s"}\\n\' "$N" "$N"\n'
        "exit $CODE\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _provision(home: Path, binary: Path, monkeypatch, fingerprint: str):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(keys_module, "machine_fingerprint", lambda: fingerprint)
    return ensure_device_key(
        home,
        owner="tester",
        device_id="node-a",
        environ={BINARY_ENV: str(binary)},
    )


def test_copied_home_on_another_machine_reissues(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    binary = _rotating_genkey(tmp_path)
    source = tmp_path / "source"
    original = _provision(source, binary, monkeypatch, "a" * 64)
    clone = tmp_path / "clone"
    shutil.copytree(source, clone)

    rotated = _provision(clone, binary, monkeypatch, "b" * 64)

    assert rotated.server_public != original.server_public
    assert rotated.client_public != original.client_public
    assert rotated.key_generation == original.key_generation + 1
    binding = json.loads(machine_binding_path(clone).read_text("utf-8"))
    assert binding["machineFingerprint"] == "b" * 64
    assert binding["lastReason"] == "copied-home"
    assert binding["pendingEvent"]["reason"] == "copied-home"
    assert len(binding["pendingEvent"]["oldIdentityHash"]) == 12
    assert len(binding["pendingEvent"]["newIdentityHash"]) == 12


def test_same_machine_keeps_a_stable_identity(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    binary = _rotating_genkey(tmp_path)
    home = tmp_path / "home"
    first = _provision(home, binary, monkeypatch, "a" * 64)
    second = _provision(home, binary, monkeypatch, "a" * 64)

    assert second == first
    assert second.key_generation == 1


def test_legacy_identity_is_adopted_not_reissued(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    binary = _rotating_genkey(tmp_path)
    home = tmp_path / "home"
    first = _provision(home, binary, monkeypatch, "a" * 64)
    machine_binding_path(home).unlink()

    adopted = _provision(home, binary, monkeypatch, "b" * 64)

    assert adopted == first
    assert (
        json.loads(machine_binding_path(home).read_text("utf-8"))["machineFingerprint"]
        == "b" * 64
    )


def test_directory_signal_reissues_once_and_only_once(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    binary = _rotating_genkey(tmp_path)
    home = tmp_path / "home"
    first = _provision(home, binary, monkeypatch, "a" * 64)

    rotated = reissue_device_key(
        home,
        reason="directory-conflict",
        conflict_token="c" * 64,
        environ={BINARY_ENV: str(binary)},
    )
    duplicate = reissue_device_key(
        home,
        reason="directory-conflict",
        conflict_token="c" * 64,
        environ={BINARY_ENV: str(binary)},
    )

    assert rotated is not None
    assert rotated.key_generation == first.key_generation + 1
    assert duplicate is None
    assert read_device_record(home) == rotated
    event = json.loads(machine_binding_path(home).read_text("utf-8"))["pendingEvent"]
    assert event["reason"] == "directory-conflict"
    assert len(event["oldIdentityHash"]) == 12
    assert len(event["newIdentityHash"]) == 12
