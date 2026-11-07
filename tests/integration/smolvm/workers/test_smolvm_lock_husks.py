"""Lock-only runtime husks must not accumulate, and live generations must survive.

A successful `close()` removes the engine and `owner.json` but keeps the private
runtime directory and its `runtime.lock`; nothing ever collected them (37 such
directories were measured under `/private/tmp/hv501` on devusr-dev). The reaper
is deliberately conservative: it only touches this uid's lock-only directories,
only when their lock is free, and never the directory it is about to create.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses import smolvm_runtime as runtime_module
from hyprial.daemon.impl.harnesses.smolvm_runtime import SmolvmWorkerRuntime, reap_lock_husks
from tests.integration.smolvm.workers.test_smolvm_worker_runtime import channel, materials  # noqa: F401  (fixtures)


class _Stop(Exception):
    """Sentinel: proves the reaper ran before anything else in start()."""


def husk(
    parent: Path,
    name: str = "deadbeef",
    *,
    age_seconds: float = 3600.0,
    extra: tuple[str, ...] = (),
    mode: int = 0o700,
) -> Path:
    parent.mkdir(mode=0o700, exist_ok=True)
    path = parent / name
    path.mkdir(mode=0o700)
    path.chmod(mode)
    lock = path / "runtime.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    for item in extra:
        (path / item).write_text("leftover")
    stamp = time.time() - age_seconds
    os.utime(lock, (stamp, stamp))
    os.utime(path, (stamp, stamp))
    return path


def test_reaps_a_lock_only_husk(tmp_path):
    parent = tmp_path / "hv"
    dead = husk(parent)

    assert reap_lock_husks(parent, now=time.time()) == [dead]
    assert not dead.exists()
    assert parent.exists()


def test_keeps_a_husk_a_live_generation_still_owns(tmp_path):
    parent = tmp_path / "hv"
    dead = husk(parent)
    fd = os.open(dead / "runtime.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert reap_lock_husks(parent, now=time.time()) == []
        assert dead.exists()
    finally:
        os.close(fd)


def test_keeps_a_husk_younger_than_the_minimum_age(tmp_path):
    parent = tmp_path / "hv"
    fresh = husk(parent, age_seconds=1.0)
    now = time.time()

    assert reap_lock_husks(parent, now=now) == []
    assert fresh.exists()
    assert reap_lock_husks(parent, now=now, minimum_age_seconds=0.5) == [fresh]
    assert not fresh.exists()


@pytest.mark.parametrize("leftover", ["owner.json", "engine", "notes.txt"])
def test_keeps_any_directory_that_is_more_than_the_lock(tmp_path, leftover):
    parent = tmp_path / "hv"
    kept = husk(parent, extra=(leftover,))

    assert reap_lock_husks(parent, now=time.time()) == []
    assert kept.exists()


def test_keeps_a_husk_with_unexpected_mode_or_link(tmp_path):
    parent = tmp_path / "hv"
    world = husk(parent, name="world", mode=0o755)
    linked = husk(parent, name="linked")
    (linked / "runtime.lock").unlink()
    (linked / "runtime.lock").symlink_to(tmp_path / "target")
    (tmp_path / "target").write_text("not a lock")

    assert reap_lock_husks(parent, now=time.time()) == []
    assert world.exists() and linked.exists()


def test_never_reaps_the_runtime_that_is_starting(tmp_path):
    parent = tmp_path / "hv"
    mine = husk(parent)

    assert reap_lock_husks(parent, keep=mine, now=time.time()) == []
    assert mine.exists()


def test_reaps_an_empty_leftover_from_an_interrupted_reap(tmp_path):
    parent = tmp_path / "hv"
    parent.mkdir(mode=0o700)
    empty = parent / "empty"
    empty.mkdir(mode=0o700)
    stamp = time.time() - 3600
    os.utime(empty, (stamp, stamp))

    assert reap_lock_husks(parent, now=time.time()) == [empty]
    assert not empty.exists()


def test_a_missing_parent_is_not_an_error(tmp_path):
    assert reap_lock_husks(tmp_path / "absent", now=time.time()) == []


def test_start_reaps_husks_before_creating_its_own(
    materials,  # noqa: F811
    channel,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    calls = {}

    def spy(parent, **kwargs):
        calls["parent"] = parent
        calls["keep"] = kwargs.get("keep")
        raise _Stop

    monkeypatch.setattr(runtime_module, "mount_roots", lambda channel, cwd: ())
    monkeypatch.setattr(runtime_module, "reap_lock_husks", spy)
    runtime = SmolvmWorkerRuntime(materials, channel, "epoch", None)
    monkeypatch.setattr(runtime, "output", tmp_path / "hv" / "ours")

    with pytest.raises(_Stop):
        runtime.start(("codex",), {})

    assert calls == {"parent": runtime.output.parent, "keep": runtime.output}
    assert runtime.output.parent.is_dir()
