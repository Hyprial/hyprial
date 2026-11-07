"""R1–R3 regressions: only synthetic data in the explicit pytest temp root."""
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
TOKEN = b"SYNTHETIC-R2-SECRET-b91e02"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"agent-home-{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(name, home, yes):
    args = [sys.executable, str(SCRIPTS / f"agent-home-{name}.py"), str(home)]
    if name == "destroy":
        args += ["--who", "synthetic-reviewer", "--why", "regression"]
    return subprocess.run(args + (["--yes"] if yes else []), capture_output=True, timeout=10)


def snapshot(root):
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        data = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                else path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
        result[path.relative_to(root).as_posix()] = (info.st_mode, info.st_ino, data)
    return result


@pytest.mark.parametrize("yes", [False, True])
@pytest.mark.parametrize("alias", ["workspace/token", "state/token", "secrets/alias", "../outside"])
def test_destroy_refuses_every_secret_hardlink_before_writes(tmp_path, yes, alias):
    home = tmp_path / "agent"
    (home / "secrets/nested").mkdir(parents=True)
    secret = home / "secrets/nested/token"
    secret.write_bytes(TOKEN)
    retained = home / alias
    retained.parent.mkdir(parents=True, exist_ok=True)
    os.link(secret, retained)
    before = snapshot(tmp_path)
    proc = cli("destroy", home, yes)
    assert proc.returncode == 2
    assert b"hardlink" in proc.stderr
    assert TOKEN not in proc.stdout + proc.stderr
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "destroyed").exists()


def test_destroy_refuses_hardlinked_secret_root_file(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(TOKEN)
    os.link(outside, home / "secrets")
    before = snapshot(tmp_path)
    assert cli("destroy", home, True).returncode == 2
    assert snapshot(tmp_path) == before


def test_destroy_rechecks_hardlinks_before_receipt(tmp_path):
    module = load("destroy")
    home = tmp_path / "agent"
    (home / "secrets").mkdir(parents=True)
    (home / "secrets/token").write_bytes(TOKEN)
    plan = module._plan(home, who="synthetic", why="test", now=None)
    os.link(home / "secrets/token", tmp_path / "outside")
    before = snapshot(tmp_path)
    with pytest.raises(module.DestroyError, match="hardlink"):
        module._apply(plan)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("yes", [False, True])
@pytest.mark.parametrize("component", ["records", "e2e",
                                      "workspace", "workspace/records", "workspace/notes",
                                      "workspace/e2e"])
@pytest.mark.parametrize("target", ["outside", "inside", "missing"])
def test_migrate_refuses_all_symlink_components(tmp_path, yes, component, target):
    home = tmp_path / "agent"
    home.mkdir()
    outside = tmp_path / "outside"
    (outside / "home").mkdir(parents=True)
    (outside / "home/keep").write_bytes(TOKEN)
    inside = home / "inside"
    inside.mkdir()
    link = home / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to({"outside": outside, "inside": inside,
                     "missing": tmp_path / "missing"}[target], target_is_directory=True)
    before = snapshot(tmp_path)
    proc = cli("migrate", home, yes)
    assert proc.returncode == 2
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("content", [TOKEN, b"gitdir: /repo/worktrees/" + TOKEN,
                                     b"\xff\xfe" + TOKEN, b"x" * 100000 + TOKEN])
@pytest.mark.parametrize("yes", [False, True])
def test_migrate_git_pointer_never_echoes_bytes(tmp_path, content, yes):
    home = tmp_path / "agent"
    (home / "records").mkdir(parents=True)
    (home / "records/.git").write_bytes(content)
    before = snapshot(tmp_path)
    proc = cli("migrate", home, yes)
    assert proc.returncode == 2
    assert TOKEN not in proc.stdout + proc.stderr
    assert b"Traceback" not in proc.stderr
    assert json.loads(proc.stderr)["ok"] is False
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("name", ["migrate", "destroy", "inventory"])
def test_git_reads_are_bounded_and_binary(tmp_path, monkeypatch, name):
    module = load(name)
    home = tmp_path / "agent"
    (home / "records").mkdir(parents=True)
    (home / "records/.git").write_bytes(b"gitdir: /repo/worktrees/synthetic\n" * 10000)
    original_open = Path.open
    sizes = []

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size=-1):
            sizes.append(size)
            assert 0 < size <= 4096
            return b"gitdir: /repo/worktrees/synthetic\n"

    def guarded_open(path, mode="r", *args, **kwargs):
        if path.name == ".git":
            assert mode == "rb"
            return Reader()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    if name == "migrate":
        with pytest.raises(module.MigrationError):
            module.migrate(home)
    elif name == "destroy":
        module.destroy(home, who="synthetic", why="test")
    else:
        module.inventory(home)
    assert sizes


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
)
def test_destroy_unreadable_secret_subtree_refuses_before_writes(tmp_path):
    # Real EACCES needs a normal user; the in-process variant below covers root runners.
    home = tmp_path / "agent"
    nested = home / "secrets/nested"
    nested.mkdir(parents=True)
    (nested / "token").write_bytes(TOKEN)
    before = snapshot(tmp_path)
    nested.chmod(0)
    try:
        proc = cli("destroy", home, True)
    finally:
        nested.chmod(0o755)
    assert proc.returncode == 2
    assert snapshot(tmp_path) == before



def test_destroy_secret_walk_error_refuses_before_writes(tmp_path, monkeypatch):
    # Same refusal as the real-EACCES case, injected so it also runs as root (CI).
    module = load("destroy")
    home = tmp_path / "agent"
    nested = home / "secrets/nested"
    nested.mkdir(parents=True)
    (nested / "token").write_bytes(TOKEN)
    before = snapshot(tmp_path)
    real_walk = module.os.walk

    def denied_walk(top, *args, onerror=None, **kwargs):
        for entry in real_walk(top, *args, onerror=onerror, **kwargs):
            if Path(entry[0]) == nested:
                onerror(PermissionError(13, "Permission denied", str(nested)))
                return
            yield entry

    monkeypatch.setattr(module.os, "walk", denied_walk)
    with pytest.raises(module.DestroyError, match="cannot inspect secret tree"):
        module.destroy(home, who="synthetic", why="test", yes=True)
    monkeypatch.setattr(module.os, "walk", real_walk)
    assert snapshot(tmp_path) == before

def test_migrate_refuses_late_unsafe_path_before_any_rename(tmp_path, monkeypatch):
    module = load("migrate")
    home = tmp_path / "agent"
    (home / "state/home").mkdir(parents=True)
    (home / "records").mkdir()
    (home / "workspace").symlink_to(tmp_path / "missing")
    before = snapshot(tmp_path)

    def forbidden(*args):
        pytest.fail("rename must not run before all paths pass preflight")

    monkeypatch.setattr(module.os, "rename", forbidden)
    with pytest.raises(module.MigrationError):
        module.migrate(home, yes=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_migrate_does_not_read_special_git_pointer(tmp_path, kind):
    home = tmp_path / "agent"
    (home / "records").mkdir(parents=True)
    pointer = home / "records/.git"
    if kind == "symlink":
        (tmp_path / "outside").write_bytes(TOKEN)
        pointer.symlink_to(tmp_path / "outside")
    else:
        os.mkfifo(pointer)
    before = snapshot(tmp_path)
    proc = cli("migrate", home, True)
    assert proc.returncode == 2
    assert TOKEN not in proc.stdout + proc.stderr
    assert snapshot(tmp_path) == before


def test_migrate_exception_payload_is_not_public(tmp_path, monkeypatch, capsys):
    module = load("migrate")
    home = tmp_path / "agent"
    (home / "records").mkdir(parents=True)
    before = snapshot(tmp_path)

    def fail(*args):
        raise OSError(TOKEN.decode())

    monkeypatch.setattr(module.os, "rename", fail)
    assert module.main([str(home), "--yes"]) == 2
    captured = capsys.readouterr()
    assert TOKEN.decode() not in captured.out + captured.err
    assert json.loads(captured.err)["ok"] is False
    assert snapshot(tmp_path) == before
