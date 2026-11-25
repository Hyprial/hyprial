"""The pre-stop GUI runtime probe of ``apply --gui``.

Owner decision 2026-11-21 (the product has no read-only query): before
``service.stop``, with ``--gui``, the installed CLI's
``hyprial gui --no-open --json --port 0 --stdin-stop`` is started against the
target home and stopped at once.  Everything runs through the fake runner, the
fake child (``probe_overrides`` describe the probe child) and the fake clock.

Decisions pinned here:
- ``GUI_RUNTIME_BUSY`` from the probe refuses before the stop: exit 2,
  ``ABORTED``, no stop, no install, no rollback; the message names the home and
  the runtime and says to stop that GUI or drop ``--gui``;
- a valid record means the runtime is free: ``gui.runtime-free`` PASS, the probe
  is stopped before the service stop and the upgrade proceeds to the later GUI
  check;
- a probe that cannot be stopped, or a probe that changed the target daemon,
  refuses before the stop;
- any other probe outcome answers nothing: ``gui.runtime-free`` NOT_RUN with the
  reason, no child left, the upgrade proceeds;
- without ``--gui`` there is no probe; the post-upgrade fallback still rolls
  back when the runtime becomes busy after the probe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ..gui_support import GUI_PID, WALL, Listener, build, check, smoke, summary_of

BUSY_ERROR = "another foreground GUI already owns this runtime"


def busy_record(runtime: str) -> str:
    return json.dumps(
        {"ok": False, "code": "GUI_RUNTIME_BUSY", "error": BUSY_ERROR,
         "data": {"runtime": runtime}}
    )


def refused_before_stop(instance: Any, runner: Any, rc: int) -> None:
    assert (rc, instance.result) == (smoke.EXIT_USAGE, "ABORTED")
    assert runner.service_stops() == []
    assert runner.installs == []
    assert runner.gui_spawns() == []
    assert instance.rollback == {}
    assert runner.installed_version == "0.1.0"
    assert runner.active is True


def test_a_busy_runtime_refuses_before_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runtime = str(instance.state_dir / "gui" / "daemon")
    runner.probe_overrides = {
        "raw_record": busy_record(runtime),
        "exit_after_record": 0.1,
        "exit_after_record_code": 1,
    }

    rc = instance.finish(instance.run_apply())

    refused_before_stop(instance, runner, rc)
    free = check(instance, "gui.runtime-free")
    assert free.status == smoke.FAIL
    assert "GUI_RUNTIME_BUSY" in free.detail
    assert runtime in free.detail
    assert str(instance.home) in free.detail
    assert "--gui" in free.detail
    [probe] = runner.probes()
    assert probe.signals == []
    assert runner.gui_active is False
    assert summary_of(instance)["result"] == "ABORTED"


def test_a_free_runtime_lets_the_upgrade_proceed_to_the_gui_check(
    tmp_path: Path, listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_GUI_LAUNCH_TTL_MS", "86400000")
    instance, runner = build(tmp_path, listener.port, ["--gui", "--gui-hold-minutes", "5"])

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert runner.installs == ["0.2.0"]
    assert check(instance, "gui.runtime-free").status == smoke.PASS
    assert check(instance, "gui.probe-stop").status == smoke.PASS
    assert check(instance, "gui.probe-daemon-identity").status == smoke.PASS
    assert check(instance, "gui.record").status == smoke.PASS
    [probe] = runner.probes()
    assert probe.returncode == 0 and probe.signals == []
    first_stop = next(
        i for i, c in enumerate(runner.calls) if c[1:4] == ["daemon", "stop", "--json"]
    )
    assert probe.stdin_closed_after_calls <= first_stop
    [spawn] = [item for item in runner.spawns if item["probe"]]
    assert spawn["version"] == "0.1.0"
    assert spawn["argv"][:2] == [str(instance.live_cli), "gui"]
    assert spawn["argv"][spawn["argv"].index("--port") + 1] == "0"
    for flag in ("--stdin-stop", "--no-open", "--json"):
        assert flag in spawn["argv"]
    assert "HYPRIAL_GUI_LAUNCH_TTL_MS" not in spawn["env"]
    assert spawn["stderr"].name == f"gui-probe-{WALL}.err.log"
    assert len(runner.gui_spawns()) == 1


def test_a_probe_that_cannot_be_stopped_refuses_before_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.probe_overrides = {
        "stdin_stop_seconds": None,
        "exits_on_sigterm": False,
        "exits_on_sigkill": False,
    }

    rc = instance.finish(instance.run_apply())

    refused_before_stop(instance, runner, rc)
    stop = check(instance, "gui.probe-stop")
    assert stop.status == smoke.FAIL
    assert f"kill -KILL -- -{GUI_PID}" in stop.detail
    assert runner.gui_active is True
    assert summary_of(instance)["baseline"]["guiProbe"]["pid"] == GUI_PID


def test_a_probe_that_changes_the_target_daemon_refuses_before_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.probe_overrides = {"move_on_gui": True}

    rc = instance.run_apply()

    refused_before_stop(instance, runner, rc)
    assert check(instance, "gui.probe-daemon-identity").status == smoke.FAIL
    assert runner.gui_active is False


NO_ANSWER = {
    "old-cli-json-usage-error": {
        "raw_record": json.dumps(
            {"ok": False, "code": "INVALID_ARGUMENT", "error": "No such option: --stdin-stop"}
        ),
        "exit_after_record": 0.0,
        "exit_after_record_code": 2,
    },
    "old-cli-plain-usage-error": {
        "write_record": False,
        "exit_after_record": 0.0,
        "exit_after_record_code": 2,
    },
    "other-error-code": {
        "raw_record": json.dumps({"ok": False, "code": "GUI_DAEMON_NOT_READY", "error": "x"}),
        "exit_after_record": 0.1,
        "exit_after_record_code": 1,
    },
    "invalid-record": {"record_overrides": {"backend_owned": True}},
    "not-json": {"raw_record": "Usage: hyprial gui [OPTIONS]"},
    "no-record-in-time": {"write_record": False},
    "cannot-spawn": {"spawn_error": FileNotFoundError(2, "No such file or directory")},
}


@pytest.mark.parametrize("name", sorted(NO_ANSWER))
def test_a_probe_without_an_answer_does_not_block_the_upgrade(
    tmp_path: Path, listener: Listener, name: str
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.probe_overrides = NO_ANSWER[name]

    rc = instance.run_apply()

    assert (rc, instance.result) == (smoke.EXIT_OK, "PASS")
    free = check(instance, "gui.runtime-free")
    assert free.status == smoke.NOT_RUN
    assert len(free.detail) > 20
    assert runner.installs == ["0.2.0"]
    assert check(instance, "gui.record").status == smoke.PASS
    assert all(child.poll() is not None for child in runner.probes())
    assert runner.gui_active is False


def test_without_gui_there_is_no_probe(tmp_path: Path, listener: Listener) -> None:
    instance, runner = build(tmp_path, listener.port)

    assert instance.run_apply() == smoke.EXIT_OK

    assert runner.spawns == []
    assert all(not item.name.startswith("gui.") for item in instance.checks)


def test_a_runtime_busy_only_after_the_probe_still_rolls_back(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    runner.raw_record = busy_record(str(instance.state_dir / "gui" / "daemon"))
    runner.exit_after_record = 0.1
    runner.exit_after_record_code = 1

    rc = instance.run_apply()

    assert check(instance, "gui.runtime-free").status == smoke.PASS
    assert check(instance, "gui.start").status == smoke.FAIL
    assert (rc, instance.result) == (smoke.EXIT_FAILED, "ROLLED_BACK")
    assert runner.installs == ["0.2.0", "0.1.0"]


def test_a_stale_probe_log_refuses_before_the_stop(
    tmp_path: Path, listener: Listener
) -> None:
    instance, runner = build(tmp_path, listener.port, ["--gui"])
    stale = instance.artifacts_dir / f"gui-probe-{WALL}.err.log"
    stale.write_text("earlier run\n")

    rc = instance.run_apply()

    refused_before_stop(instance, runner, rc)
    assert runner.spawns == []
    assert "already exists" in check(instance, "gui.runtime-free").detail
    assert stale.read_text() == "earlier run\n"
