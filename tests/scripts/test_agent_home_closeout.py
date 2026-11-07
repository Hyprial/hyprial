"""End-to-end safety checks use only synthetic homes below pytest's tmp_path."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "closeout_migrate", SCRIPTS / "agent-home-migrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> dict[str, tuple[int, bytes | str | None]]:
    """Compare names, modes, symlink destinations and every regular file byte."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        mode = path.lstat().st_mode
        payload = (
            os.readlink(path) if stat.S_ISLNK(mode)
            else path.read_bytes() if stat.S_ISREG(mode)
            else None
        )
        result[path.relative_to(root).as_posix()] = (mode, payload)
    return result


def _legacy(root: Path) -> Path:
    home = root / "synthetic-agent"
    for name in ("state/home", "records", "e2e", "secrets"):
        (home / name).mkdir(parents=True)
        (home / name / "payload").write_bytes(b"synthetic-" + name.encode())
    (home / "records/payload").chmod(0o600)
    (home / "records/link").symlink_to("payload")
    return home


def _cli(script: str, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, check=True,
    )
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_real_cli_inventory_migrate_destroy_requires_yes(tmp_path: Path):
    home = _legacy(tmp_path)
    before = _snapshot(home)
    inventory = _cli("agent-home-inventory.py", str(home))
    assert inventory["legacy"]["records"] is True
    assert _snapshot(home) == before

    dry = _cli("agent-home-migrate.py", str(home))
    assert dry["dry_run"] is True
    assert _snapshot(home) == before
    assert _cli("agent-home-migrate.py", str(home), "--yes")["dry_run"] is False
    assert (home / "workspace/notes/payload").read_bytes() == b"synthetic-records"

    secret = b"SYNTHETIC-SECRET-DO-NOT-RETAIN-9c73"
    (home / "secrets/token").write_bytes(secret)
    before = _snapshot(home)
    args = (str(home), "--who", "user:synthetic-operator", "--why", "closeout-test")
    dry = _cli("agent-home-destroy.py", *args)
    assert dry["dry_run"] is True
    assert _snapshot(home) == before
    assert not (tmp_path / "destroyed").exists()

    applied = _cli("agent-home-destroy.py", *args, "--yes")
    target = Path(applied["target"])
    assert not home.exists()
    assert not (target / "secrets").exists()
    for _, payload in _snapshot(target).values():
        if isinstance(payload, bytes):
            assert secret not in payload
            assert b"synthetic-secrets" not in payload
    receipt = json.loads((target / "DESTROYED.json").read_text())
    assert set(receipt) == {"who", "when", "why", "what", "external_refs"}
    assert receipt["who"] == "user:synthetic-operator"
    assert receipt["why"] == "closeout-test"
    assert receipt["what"]["secrets_deleted"] == ["secrets/payload", "secrets/token"]
    assert (target / "COMPLETE").read_text().strip() == receipt["when"]


@pytest.mark.parametrize("source", ["records", "workspace/records", "both"])
def test_cli_either_or_source_rule(tmp_path: Path, source: str):
    home = tmp_path / "synthetic-agent"
    sources = ("records", "workspace/records") if source == "both" else (source,)
    for relative in sources:
        directory = home / relative
        directory.mkdir(parents=True)
        (directory / "note").write_text(relative)
    before = _snapshot(tmp_path)
    if source == "both":
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "agent-home-migrate.py"), str(home), "--yes"],
            capture_output=True, text=True, check=False,
        )
        assert completed.returncode == 2
        assert completed.stdout == ""
        error = json.loads(completed.stderr)["error"]
        assert "ambiguous" in error
        assert str(home / "records") in error
        assert str(home / "workspace/records") in error
        assert _snapshot(tmp_path) == before
    else:
        assert _cli("agent-home-migrate.py", str(home))["dry_run"] is True
        assert _snapshot(tmp_path) == before
        _cli("agent-home-migrate.py", str(home), "--yes")
        assert not (home / source).exists()
        assert (home / "workspace/notes/note").read_text() == source


@pytest.mark.parametrize("failure", ["staging", "delivery", "persona"])
def test_failed_apply_restores_bytes_modes_and_links(tmp_path: Path, monkeypatch, failure):
    module = _load_migrate()
    home = _legacy(tmp_path)
    before = _snapshot(tmp_path)
    rename = module.os.rename
    mkdir = module._mkdir_recorded
    renames = 0

    def inject_rename(source, destination):
        nonlocal renames
        renames += 1
        # Two sources stage first; fail after one stage or one delivery.
        if renames == {"staging": 2, "delivery": 4}.get(failure):
            raise OSError("synthetic rename failure")
        return rename(source, destination)

    def inject_mkdir(path, created):
        if failure == "persona" and path == home / "persona/skills":
            raise OSError("synthetic persona failure")
        return mkdir(path, created)

    monkeypatch.setattr(module.os, "rename", inject_rename)
    monkeypatch.setattr(module, "_mkdir_recorded", inject_mkdir)
    with pytest.raises(module.MigrationError, match="rolled back") as caught:
        module.migrate(home, yes=True)
    assert "rollback errors" not in str(caught.value)
    assert _snapshot(tmp_path) == before
