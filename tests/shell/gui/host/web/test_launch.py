"""Launch-file lifecycle (ports of the handoff-plugin timer cases, fake clock)."""

from __future__ import annotations

import os
from pathlib import Path

from hyprial.shell.impl.gui.host.session import SessionStore
from hyprial.shell.impl.gui.host.web.launch import PUBLICATION_POLL_SECONDS, LaunchFiles
from tests.shell.gui.host.support import FakeClock, host_config, run_gateway

POLL_MS = int(PUBLICATION_POLL_SECONDS * 1000)


def launch_files(tmp_path: Path, **overrides):
    clock = FakeClock()
    config = host_config(launch_ttl_ms=1_000, **overrides)
    store = SessionStore(config, clock=clock)
    return LaunchFiles(config, store, clock), clock


def handoff(tmp_path: Path) -> Path:
    path = tmp_path / "launch.url"
    path.write_text("http://127.0.0.1:4173/#grant=synthetic\n")
    path.chmod(0o600)
    return path


def publish(tmp_path: Path) -> Path:
    path = tmp_path / "launch.ready"
    path.write_text("published\n")
    path.chmod(0o600)
    return path


def test_the_private_handoff_is_removed_at_launch_expiry(tmp_path: Path) -> None:
    path = handoff(tmp_path)
    files, clock = launch_files(tmp_path, handoff_path=str(path))
    files.start()
    assert path.exists()
    clock.advance(999)
    assert path.exists()
    clock.advance(2)
    assert not path.exists()
    files.close()


def test_expiry_is_armed_from_a_handoff_created_after_the_former_startup_deadline(tmp_path: Path) -> None:
    path, ready = tmp_path / "launch.url", tmp_path / "launch.ready"
    files, clock = launch_files(tmp_path, handoff_path=str(path), publication_path=str(ready))
    files.start()
    clock.advance(1_500)
    assert not path.exists()
    publish(tmp_path)
    handoff(tmp_path)
    clock.advance(POLL_MS)
    assert path.exists()
    clock.advance(999)
    assert path.exists()
    clock.advance(2)
    assert not path.exists() and not ready.exists()
    files.close()


def test_a_complete_handoff_survives_publication_delayed_beyond_the_launch_ttl(tmp_path: Path) -> None:
    path, ready = handoff(tmp_path), tmp_path / "launch.ready"
    files, clock = launch_files(tmp_path, handoff_path=str(path), publication_path=str(ready))
    files.start()
    clock.advance(5_000)
    assert path.exists()
    publish(tmp_path)
    clock.advance(POLL_MS)
    assert path.exists()
    clock.advance(999)
    assert path.exists()
    clock.advance(2)
    assert not path.exists() and not ready.exists()
    files.close()


def test_a_browser_launch_publication_marker_is_removed_at_expiry_without_a_handoff(tmp_path: Path) -> None:
    ready = tmp_path / "launch.ready"
    files, clock = launch_files(tmp_path, publication_path=str(ready))
    files.start()
    publish(tmp_path)
    clock.advance(POLL_MS)
    assert ready.exists()
    clock.advance(1_001)
    assert not ready.exists()
    files.close()


def test_consuming_the_grant_removes_both_files_and_stops_polling(tmp_path: Path) -> None:
    path, ready = handoff(tmp_path), tmp_path / "launch.ready"
    files, clock = launch_files(tmp_path, handoff_path=str(path), publication_path=str(ready))
    files.start()
    publish(tmp_path)
    files.grant_consumed()
    assert not path.exists() and not ready.exists()
    publish(tmp_path)
    clock.advance(5_000)
    assert ready.exists()
    files.close()


def test_close_removes_the_files_and_cancels_every_timer(tmp_path: Path) -> None:
    path = handoff(tmp_path)
    files, clock = launch_files(tmp_path, handoff_path=str(path))
    files.start()
    files.close()
    assert not path.exists()
    path = handoff(tmp_path)
    clock.advance(5_000)
    assert path.exists()


def test_only_regular_files_are_unlinked_never_a_symlink_or_its_target(tmp_path: Path) -> None:
    target = tmp_path / "precious"
    target.write_text("keep")
    link = tmp_path / "launch.url"
    os.symlink(target, link)
    directory = tmp_path / "launch.ready"
    directory.mkdir()
    files, clock = launch_files(tmp_path, handoff_path=str(link), publication_path=str(directory))
    files.start()
    files.grant_consumed()
    files.close()
    assert link.is_symlink() and target.read_text() == "keep" and directory.is_dir()


def test_a_missing_launch_file_is_not_an_error(tmp_path: Path) -> None:
    files, clock = launch_files(tmp_path, handoff_path=str(tmp_path / "launch.url"))
    files.start()
    clock.advance(2_000)
    files.grant_consumed()
    files.close()


def test_the_app_lifespan_arms_the_files_and_the_grant_consumption_removes_them(tmp_path: Path) -> None:
    path, ready = handoff(tmp_path), publish(tmp_path)
    config = host_config(handoff_path=str(path), publication_path=str(ready))
    clock = FakeClock()

    async def scenario(gateway) -> None:
        assert path.exists() and ready.exists()
        opened = await gateway.open()
        assert opened.reply.status == 200
        assert not path.exists() and not ready.exists()

    run_gateway(scenario, config=config, clock=clock)


def test_shutting_the_app_down_removes_unconsumed_launch_files(tmp_path: Path) -> None:
    path, ready = handoff(tmp_path), publish(tmp_path)
    config = host_config(handoff_path=str(path), publication_path=str(ready))

    async def scenario(gateway) -> None:
        assert path.exists()

    run_gateway(scenario, config=config, clock=FakeClock())
    assert not path.exists() and not ready.exists()
