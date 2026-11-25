"""Fake runner, fake GUI child and builders shared by the GUI-check tests.

Everything is in-process: no systemctl, systemd-run, hyprial or daemon is ever
executed.  ``GuiRunner`` is the shared ``fakes.FakeHost`` (the hyprial service
seam) plus ``spawn``, the seam the GUI check starts ``hyprial gui`` through.
``spawn`` returns a ``FakeChild`` that behaves as the startup-record contract
(``contract/cli/gui-startup-record.v1.json``) says: line 1 on stdout is the
record or a failure, a stop requested on stdin before the record exits 0 with no
line, a closed stdin after the record exits 0.  The child lives on the fake
clock: its line becomes readable, and its exit happens, once enough fake time
has passed.  A child whose stderr log is ``gui-probe-*`` is the pre-stop
runtime probe: it behaves with the defaults plus ``probe_overrides``, while the
runner-level settings describe the post-upgrade GUI child only.  The runner
models the final state a test asserts on (service active, installed wheel, GUI
child alive).  A ``daemon status`` read only times out while a daemon answers,
and a daemon "moved" by the GUI start is a new daemon after a stop.
"""

from __future__ import annotations

import json
import signal
import socket
from pathlib import Path
from typing import Any, Sequence

from .fakes import (
    SERVICE,
    STARTED_PID,
    WALL,
    FakeClock,
    FakeHost,
    build_apply,
    check,
    result,
    smoke,
    summary_of,
)

GUI_PID = 4242

__all__ = [
    "GUI_PID",
    "SERVICE",
    "WALL",
    "FakeChild",
    "FakeClock",
    "GuiRunner",
    "Listener",
    "assert_final",
    "build",
    "check",
    "smoke",
    "summary_of",
]


class Listener:
    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]

    def close(self) -> None:
        self.sock.close()


CHILD_KEYS = (
    "record_after",
    "exit_after_record",
    "exit_after_record_code",
    "stdin_stop_seconds",
    "stdin_stop_code",
    "exits_on_sigterm",
    "exits_on_sigkill",
    "group_survives_root",
    "group_exits_on_sigterm",
    "group_exits_on_sigkill",
    "move_on_gui",
    "spawn_error",
)
CHILD_DEFAULTS: dict[str, Any] = {
    "record_after": 1.0,
    "exit_after_record": None,
    "exit_after_record_code": 1,
    "stdin_stop_seconds": 0.5,
    "stdin_stop_code": 0,
    "exits_on_sigterm": True,
    "exits_on_sigkill": True,
    "group_survives_root": False,
    "group_exits_on_sigterm": True,
    "group_exits_on_sigkill": True,
    "move_on_gui": False,
    "spawn_error": None,
}


class FakeChild:
    """One ``hyprial gui --stdin-stop`` child on the fake clock."""

    def __init__(
        self, runner: "GuiRunner", argv: list[str], cfg: dict[str, Any], line: str | None
    ) -> None:
        self.runner = runner
        self.argv = argv
        self.cfg = cfg
        self.pid = GUI_PID
        self.started = runner.clock.monotonic()
        self.record_at = self.started + cfg["record_after"]
        self.returncode: int | None = None
        self.exit_at: float | None = None
        self.exit_code = 0
        self.stdin_closed_at: float | None = None
        self.signals: list[int] = []
        self.lines: list[str] = [line + "\n"] if line is not None else []
        self.closed = False
        self._residual_alive = bool(cfg["group_survives_root"])
        if cfg["exit_after_record"] is not None:
            self.exit_at = self.record_at + cfg["exit_after_record"]
            self.exit_code = cfg["exit_after_record_code"]

    def now(self) -> float:
        return self.runner.clock.monotonic()

    def settle(self) -> None:
        if self.returncode is None and self.exit_at is not None and self.now() >= self.exit_at:
            self.returncode = self.exit_code

    def read_line(self) -> str | None:
        if self.closed:
            raise AssertionError("read_line after close")
        if self.lines and self.now() >= self.record_at:
            return self.lines.pop(0)
        return None

    def poll(self) -> int | None:
        self.settle()
        return self.returncode

    def close_stdin(self) -> None:
        if self.stdin_closed_at is not None:
            return
        self.stdin_closed_at = self.now()
        self.stdin_closed_after_calls = len(self.runner.calls)
        self.settle()
        if self.cfg["stdin_stop_seconds"] is None or self.returncode is not None:
            return
        at = self.stdin_closed_at + self.cfg["stdin_stop_seconds"]
        if self.exit_at is None or at < self.exit_at:
            self.exit_at = at
            self.exit_code = self.cfg["stdin_stop_code"]
        if self.now() < self.record_at:
            self.lines.clear()

    def signal_group(self, number: int) -> bool:
        self.settle()
        if self.returncode is not None:
            raise AssertionError("signal sent to a child that already exited")
        self.signals.append(number)
        exits = {
            signal.SIGTERM: self.cfg["exits_on_sigterm"],
            signal.SIGKILL: self.cfg["exits_on_sigkill"],
        }.get(number, False)
        if exits:
            self.returncode = 128 + int(number)
        return True

    def group_alive(self) -> bool:
        if self.poll() is None:
            return True
        return self._residual_alive

    def kill_residual_group(self, number: int) -> bool:
        if not self.group_alive():
            return False
        self.signals.append(number)
        exits = {
            signal.SIGTERM: self.cfg["group_exits_on_sigterm"],
            signal.SIGKILL: self.cfg["group_exits_on_sigkill"],
        }.get(number, False)
        if exits:
            self._residual_alive = False
        return True

    def close(self) -> None:
        self.closed = True


class GuiRunner(FakeHost):
    def __init__(self, tmp_path: Path, home: Path, listener_port: int) -> None:
        super().__init__(tmp_path, home)
        self.listener_port = listener_port
        self.handoff = tmp_path / "handoff"
        self.handoff.write_text("secret\n")
        self.handoff.chmod(0o600)
        self.record_overrides: dict[str, Any] = {}
        self.drop_keys: list[str] = []
        self.raw_record: str | None = None
        self.write_record = True
        self.record_after = 1.0
        self.exit_after_record: float | None = None
        self.exit_after_record_code = 1
        self.stdin_stop_seconds: float | None = 0.5
        self.stdin_stop_code = 0
        self.exits_on_sigterm = True
        self.exits_on_sigkill = True
        self.group_survives_root = False
        self.group_exits_on_sigterm = True
        self.group_exits_on_sigkill = True
        self.spawn_error: OSError | None = None
        self.probe_overrides: dict[str, Any] = {}
        self.moved_by_gui = False
        self.move_on_gui = False
        self.children: list[FakeChild] = []
        self.spawns: list[dict[str, Any]] = []
        self.status_fails_after_ready = False
        self.status_fail_budget: int | None = None
        self.status_ok_at_new = 0
        self.fail_status_when_restored = False

    @property
    def gui_active(self) -> bool:
        return any(child.poll() is None or child.group_alive() for child in self.children)

    def record(self) -> dict[str, Any]:
        record = self.base_record()
        record.update(self.record_overrides)
        for key in self.drop_keys:
            record.pop(key, None)
        return record

    def base_record(self) -> dict[str, Any]:
        return {
            "ok": True,
            "record_version": 1,
            "mode": "normal",
            "url": f"http://127.0.0.1:{self.listener_port}/",
            "host": "127.0.0.1",
            "port": self.listener_port,
            "handoff_path": str(self.handoff),
            "backend_driver": "daemon",
            "frontend_pid": 999,
            "backend_pid": None,
            "assets_pid": None,
            "backend_endpoint": "",
            "backend_owned": False,
        }

    def child_settings(self, probe: bool) -> tuple[dict[str, Any], str | None]:
        if not probe:
            cfg = {key: getattr(self, key) for key in CHILD_KEYS}
            if not self.write_record:
                return cfg, None
            return cfg, self.raw_record if self.raw_record is not None else json.dumps(self.record())
        overrides = dict(self.probe_overrides)
        cfg = {**CHILD_DEFAULTS, **{k: v for k, v in overrides.items() if k in CHILD_KEYS}}
        if overrides.get("write_record") is False:
            return cfg, None
        if overrides.get("raw_record") is not None:
            return cfg, overrides["raw_record"]
        record = self.base_record()
        record.update(overrides.get("record_overrides", {}))
        return cfg, json.dumps(record)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        stderr_path: Path,
    ) -> FakeChild:
        command = [str(item) for item in argv]
        probe = stderr_path.name.startswith("gui-probe-")
        self.calls.append(command)
        self.envs.append(env)
        self.spawns.append(
            {"argv": command, "env": env, "stderr": stderr_path, "probe": probe,
             "version": self.installed_version, "at": len(self.calls) - 1}
        )
        cfg, line = self.child_settings(probe)
        if cfg["spawn_error"] is not None:
            raise cfg["spawn_error"]
        stderr_path.write_text("")
        child = FakeChild(self, command, cfg, line)
        child.probe = probe
        self.children.append(child)
        self.moved_by_gui = self.moved_by_gui or cfg["move_on_gui"]
        return child

    def probes(self) -> list[FakeChild]:
        return [child for child in self.children if child.probe]

    def gui_children(self) -> list[FakeChild]:
        return [child for child in self.children if not child.probe]

    def gui_spawns(self) -> list[dict[str, Any]]:
        return [item for item in self.spawns if not item["probe"]]

    def gui_calls(self) -> list[list[str]]:
        return [item["argv"] for item in self.gui_spawns()]

    def manager_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] in ("systemd-run", "systemctl")]

    def daemon_payload(self) -> dict[str, Any]:
        payload = super().daemon_payload()
        daemon = payload["daemon"]
        if self.moved_by_gui and daemon.get("running") is True:
            daemon["pid"] += 1000
            daemon["socket"] = str(self.home / "other.sock")
        return payload

    def stop_daemon(self) -> None:
        self.moved_by_gui = False
        super().stop_daemon()

    def status_fails(self) -> bool:
        if self.answering_pid() is None:
            return False
        if self.fail_status_when_restored and self.active and self.main_pid == STARTED_PID:
            return True
        if not self.status_fails_after_ready:
            return False
        if self.installed_version != "0.2.0":
            return False
        if self.status_ok_at_new < 1:
            self.status_ok_at_new += 1
            return False
        if self.status_fail_budget is None:
            return True
        if self.status_fail_budget > 0:
            self.status_fail_budget -= 1
            return True
        return False

    def hyprial(self, command: list[str]) -> Any:
        if command[1:4] == ["daemon", "status", "--json"] and self.status_fails():
            return result(command, 1, "", "timeout")
        return super().hyprial(command)


def build(
    tmp_path: Path,
    listener_port: int,
    extra: Sequence[str] = (),
) -> tuple[Any, GuiRunner]:
    if "--gui-port" not in extra:
        extra = [*extra, "--gui-port", str(listener_port)]
    return build_apply(tmp_path, GuiRunner, extra, host_args=(listener_port,))


def assert_final(
    instance: Any,
    runner: GuiRunner,
    rc: int,
    *,
    expected_rc: int,
    result: str,
    active: bool,
    version: str,
    gui_alive: bool = False,
) -> None:
    assert (rc, instance.result) == (expected_rc, result)
    assert runner.active is active
    assert runner.installed_version == version
    assert runner.gui_active is gui_alive
