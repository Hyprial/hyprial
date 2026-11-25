"""Start the daemon once before a business command's single request (Z-4).

Decision: daemon-first-z4-cli-ensure-20261117.
- Nothing is ever retried.  Before the one request of a command, a
  connect-only probe of the home's socket (no frame is sent) tells whether
  something listens.  Only a failed connect (no socket file, connection
  refused) starts the daemon; a daemon that accepts the connection and then
  answers ``DAEMON_UNAVAILABLE`` is reported as before and never restarted.
  The connect itself is the kernel's ``daemon_socket_absent`` (a platform seam),
  imported by ``support.py`` under its own reviewed import-ratchet entry and
  handed to ``ensure_listening``.
- The scope is opt-in per ``execute``: ``_execute`` opens it, and every
  lifecycle, upgrade, profile, login, meta and offline-capable command
  (adapter add/remove, agent tiers, org import)
  passes ``ensure_daemon=False``.  Outside a scope nothing here acts.
- ``ping`` (the readiness probe) and ``shutdown`` never start a daemon; the
  launch itself runs in a busy scope, so its own requests cannot recurse.
- The decision is made before any output: the opt-out
  ``HYPRIAL_DAEMON_AUTOSTART=0``, ``ensure_daemon=False`` and an agent
  session (complete or partial worker binding) leave stdout and stderr
  exactly as they were without this change.
- The launch is implicit and never installs an OS service unit
  (``autoinstall=False``); only ``init``, ``daemon start`` and
  ``daemon service install`` register one.
- ``ACCOUNT_REQUIRED`` is answered by the bootstrap daemon itself (OD-Z5);
  the CLI translates nothing and only attaches ``hyprial login`` as
  ``data.nextSteps``.  The shell spells the code once, here: importing it
  from ``hyprial.daemon`` would be a new CLI backend import, which the import
  ratchet refuses.  A test compares this spelling with the daemon's constant.
- The scope machinery is here and the launch logic is in ``launch.py``.
  ``_execute`` sets and resets the two context variables itself (C-level
  calls) instead of using a context manager: a Python helper that runs after
  the install swap would be traced in the post-install upgrade tail and grow
  its committed surface.  ``support.py`` imports the package at module level
  because that fence forbids unprimed lazy imports in the modules it traces;
  the package modules therefore appear in the primed list.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"

OFF = "off"
ON = "on"
BUSY = "busy"

SCOPE: ContextVar[str] = ContextVar("hyprial_cli_ensure_scope", default=OFF)
JSON_OUTPUT: ContextVar[bool] = ContextVar("hyprial_cli_ensure_json_output", default=False)


def current_scope() -> str:
    return SCOPE.get()


def json_output_requested() -> bool:
    return JSON_OUTPUT.get()


def enter_scope(value: str) -> Token[str]:
    return SCOPE.set(value)


def leave_scope(token: Token[str]) -> None:
    SCOPE.reset(token)
