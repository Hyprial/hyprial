"""Detached tmux sessions for interactive TUI workers.

The foreground interactive launcher owns the TUI's terminal, which means the
daemon can observe the session registration but never the TUI process itself:
when the launching terminal closes, the worker is gone.  Running the TUI
inside a detached tmux session inverts that ownership -- the tmux server keeps
the pane alive across launcher exit and terminal close, the daemon records the
session name on the interactive registration (``tmuxSession``), and the user
attaches on demand:

* macOS / iTerm2 control mode: ``tmux -CC attach -t <name>``
* WSL / Linux / plain terminal:  ``tmux attach -t <name>``

Attach and detach never signal the pane process, so the TUI (and its channel
carrier) survives both.  Cross-machine attach stays out of scope here; that is
the transfer track (#104).

Session names derive from the canonical actor URI so `hyprial ps` output and
`tmux ls` line up one-to-one.  tmux target syntax treats ``:`` and ``.`` as
separators, so every disallowed character maps deterministically to ``-``;
the raw actor stays the daemon identity key, exactly like the pi session-id
boundary translation (hyprial.harnesses.pi_session).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_SESSION_NAME_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
# tmux itself imposes no tight session-name limit, but `tmux ls` output and
# iTerm2 -CC window titles stay readable under a bounded prefix; the digest
# suffix keeps truncated names unique per actor.
_MAX_SESSION_NAME = 64
_ENV_HANDOFF_TIMEOUT_SECONDS = 5.0


class _EnvironmentHandoff:
    """One secret-safe environment transfer to a tmux pane process."""

    def __init__(self, environment: Mapping[str, str]) -> None:
        self._directory = Path(tempfile.mkdtemp(prefix="hyprial-tmux-env-", dir="/tmp"))
        os.chmod(self._directory, 0o700)
        self.path = self._directory / "environment.sock"
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._server.listen(1)
        self._server.settimeout(_ENV_HANDOFF_TIMEOUT_SECONDS)
        self._payload = json.dumps(
            dict(environment), separators=(",", ":")
        ).encode() + b"\n"
        self._delivered = threading.Event()
        self._release = threading.Event()
        self._aborted = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _address = self._server.accept()
            with connection:
                if not self._release.wait(_ENV_HANDOFF_TIMEOUT_SECONDS):
                    raise TimeoutError("tmux launch owner was not established in time")
                if self._aborted:
                    return
                connection.sendall(self._payload)
        except BaseException as error:  # noqa: BLE001 - transferred to caller
            self._error = error
        finally:
            self._delivered.set()
            self._cleanup()

    def release(self) -> None:
        """Let the pane exec only after every launch resource has an owner."""

        self._release.set()

    def wait(self) -> None:
        if not self._delivered.wait(_ENV_HANDOFF_TIMEOUT_SECONDS + 0.5):
            self.close()
            raise TimeoutError("tmux pane did not receive its launch environment")
        self._thread.join(timeout=1.0)
        if self._error is not None:
            raise RuntimeError("tmux environment handoff failed") from self._error

    def close(self) -> None:
        self._aborted = True
        self._release.set()
        try:
            self._server.close()
        finally:
            self._thread.join(timeout=1.0)
            self._cleanup()

    def _cleanup(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass
        self.path.unlink(missing_ok=True)
        try:
            self._directory.rmdir()
        except OSError:
            pass


def find_tmux() -> str | None:
    """Return the tmux binary on PATH, or None when tmux is unavailable."""

    return shutil.which("tmux")


def session_name_for_actor(actor: str) -> str:
    """Derive the deterministic tmux session name for one canonical actor."""

    base = _SESSION_NAME_DISALLOWED.sub("-", actor).strip("-") or "tui"
    name = f"hyprial-{base}"
    if len(name) > _MAX_SESSION_NAME:
        digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: _MAX_SESSION_NAME - 9]}-{digest}"
    return name


def _run(
    tmux_bin: str,
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tmux_bin, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def has_session(tmux_bin: str, name: str) -> bool:
    """Return True while a tmux session with this exact name exists."""

    return _run(tmux_bin, ["has-session", "-t", name], check=False).returncode == 0


@dataclass(frozen=True, slots=True)
class LaunchCleanupResources:
    config_path: Path
    recovery_path: Path | None = None


def new_detached_session(
    tmux_bin: str,
    name: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    cleanup: LaunchCleanupResources | None = None,
) -> None:
    """Spawn ``argv`` inside a new detached tmux session.

    A long-lived tmux server cannot be trusted to carry the launcher's current
    environment.  Values are therefore transferred over a one-shot 0600 Unix
    socket to a tiny exec wrapper. Only the socket path enters pane argv;
    credentials never do. Raises when tmux rejects the spawn or the child does
    not take custody of the environment.
    """

    handoff = _EnvironmentHandoff(env)
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "hyprial.harnesses._exec_env_socket",
            str(handoff.path),
            *(
                ["--cleanup-config", str(cleanup.config_path)]
                if cleanup is not None
                else []
            ),
            *(
                ["--cleanup-recovery", str(cleanup.recovery_path)]
                if cleanup is not None and cleanup.recovery_path is not None
                else []
            ),
            "--",
            *argv,
        ]
    )
    try:
        _run(
            tmux_bin,
            ["new-session", "-d", "-s", name, "-c", str(cwd), "--", command],
        )
        # Every created resource must have a release owner, including the
        # success path. The pane is still blocked on its private environment
        # socket here, so cleanup ownership exists before the TUI can exec or
        # register a daemon session.
        handoff.release()
        handoff.wait()
    except BaseException:
        handoff.close()
        _run(tmux_bin, ["kill-session", "-t", name], check=False)
        if cleanup is not None:
            from hyprial.harnesses._launch_cleanup import cleanup_launch_resources

            cleanup_launch_resources(cleanup.config_path, cleanup.recovery_path)
        raise


def kill_session(tmux_bin: str, name: str) -> None:
    """Best-effort session teardown; an already-dead session is not an error."""

    _run(tmux_bin, ["kill-session", "-t", name], check=False)


def pane_pid(tmux_bin: str, name: str) -> int | None:
    """Return the pane's top-process PID, or None when it cannot be read.

    The launcher exits right after spawn, so the channel carrier cannot fence
    on the launcher PID; the pane's top process lives exactly as long as the
    tmux session and is the stable owner instead.
    """

    result = _run(
        tmux_bin,
        ["display-message", "-p", "-t", name, "#{pane_pid}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def attach_hints(name: str) -> dict[str, str]:
    """The two supported attach commands for one detached session name."""

    return {
        # iTerm2 control mode: tmux windows become native iTerm2 windows and
        # detaching from the menu/window close leaves the session running.
        "macOS-iTerm2": f"tmux -CC attach -t {name}",
        "wsl-linux-terminal": f"tmux attach -t {name}",
    }
