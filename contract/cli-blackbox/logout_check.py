"""Black-box logout contract: one daemon ``auth.logout``, scoped to the home.

Decision: L1 (owner 2026-11-21).  Split out of ``run.py`` (1000-line budget)
when logout became one daemon request.  The check now needs a daemon, so it
writes a node-id sentinel, runs logout without ``HYPRIAL_OWNER`` (as a
logged-in home runs; KP-120 records the env-owner case), then stops the
isolated daemon and removes the sentinel so later checks still see a
non-running daemon.  Before the repeated logout it waits for the daemon that
logged out to answer again (it returns to bootstrap in the same process), so
the check exercises idempotency rather than the restart window.  Older sealed
references that do not advertise logout are skipped, as before.
Dependency-free like ``run.py``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

OWNER_ENV = ("HYPRIAL_OWNER",)
RETURN_SECONDS = 60.0


def _wait_for_daemon(runner: Any, require: Callable[[bool, str], None]) -> None:
    deadline = time.monotonic() + RETURN_SECONDS
    while time.monotonic() < deadline:
        status = runner.invoke("daemon", "status", "--json", isolated=True, exits=(0, 1), drop=OWNER_ENV)
        daemon = status.get("daemon")
        if isinstance(daemon, dict) and daemon.get("running") is True:
            return
        time.sleep(0.5)
    require(False, "daemon must return within 60s after logout")


def logout_contract(runner: Any, require: Callable[[bool, str], None]) -> None:
    usage = runner.invoke("help", "--json", isolated=True).get("usage", "")
    if "hyprial logout" not in usage:
        return

    home = runner.isolated_home
    issuer = "https://issuer.invalid"
    node_id = home / "node-id"
    if not node_id.exists():
        node_id.write_text("d." + "c0" * 16 + "\n", encoding="utf-8")
    login = home / "secrets" / "login.json"
    device = home / "secrets" / "tailcat" / "device-key.json"
    agent = home / "agents" / "contract-agent" / "auth.json"
    for path in (login, device, agent):
        path.parent.mkdir(parents=True, exist_ok=True)
    login.write_text(
        json.dumps(
            {
                "version": 1,
                "issuer": issuer,
                "refreshToken": "contract-refresh-token",
                "obtainedAt": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    device.write_text('{"private":"preserve"}', encoding="utf-8")
    agent.write_text('{"credential":"preserve"}', encoding="utf-8")
    (home / "settings.json").write_text(
        json.dumps(
            {
                "owner": "e2e",
                "identityMode": "casdoor",
                "identityIssuer": issuer,
                "unrelated": "preserve",
            }
        ),
        encoding="utf-8",
    )

    try:
        _check_logout(runner, require, home, login, device, agent)
    finally:
        runner.invoke("daemon", "stop", "--json", isolated=True, exits=(0, 1), drop=OWNER_ENV)
        node_id.unlink(missing_ok=True)

def _check_logout(
    runner: Any,
    require: Callable[[bool, str], None],
    home: Path,
    login: Path,
    device: Path,
    agent: Path,
) -> None:
    value = runner.invoke("logout", "--yes", "--json", isolated=True, drop=OWNER_ENV)
    require(value.get("ok") is True, "logout.ok must be true")
    require(value.get("status") == "logged-out", "logout must report logged-out")
    reported_home = value.get("home")
    require(
        isinstance(reported_home, str)
        and Path(reported_home).resolve() == home.resolve(),
        "logout must report its scoped home",
    )
    daemon = value.get("daemon")
    require(
        isinstance(daemon, dict) and isinstance(daemon.get("stopRequested"), bool),
        "logout must report daemon.stopRequested",
    )
    require(not login.exists(), "logout must remove the user login credential")
    require(device.exists(), "logout must preserve the Tailcat device key")
    require(agent.exists(), "logout must preserve agent credentials")
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    require(
        settings == {"unrelated": "preserve"},
        "logout must remove only Casdoor login fields from settings",
    )

    _wait_for_daemon(runner, require)
    repeated = runner.invoke("logout", "--yes", "--json", isolated=True, drop=OWNER_ENV)
    require(
        repeated.get("ok") is True
        and repeated.get("status") == "not-logged-in"
        and repeated.get("removed") == [],
        "repeated logout must be an idempotent success",
    )
