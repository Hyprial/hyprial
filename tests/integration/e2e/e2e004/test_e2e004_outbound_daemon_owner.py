"""Card 88fa866a: E2E-004's second isolated daemon must carry its own owner.

Controlled #4971 was the first run to reach A16 (outbound-file-route) after
#399 fixed the DM lookup, and `hyprial init` for the outbound-daemon home refused
to start: "user identity is not set: HYPRIAL_OWNER is unset or empty".  The home
is built by the 004 runner itself, not by _daemon_runner, so it never got the
`HYPRIAL_OWNER=e2e` that every other isolated daemon gets.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "contract" / "e2e" / "run-feishu-roundtrip.py"


def load_runner():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "e2e004_outbound_owner_under_test", RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(RUNNER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(RUNNER.parent))
    return module


def test_outbound_daemon_home_gets_the_isolation_owner(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_OWNER", "developer-real-identity")
    monkeypatch.setenv("HYPRIAL_ZENOH_CONNECT", "tcp/should-be-scrubbed:7447")
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:prod:node:worker")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "production-session")
    runner = load_runner()
    root = tmp_path / "outbound-daemon"
    environment = runner.outbound_daemon_environment(root / "home", root / "state")
    assert environment["HYPRIAL_OWNER"] == "e2e"
    assert environment["HYPRIAL_HOME"] == str(root / "home")
    assert environment["HARNESS_STATE_DIR"] == str(root / "state")
    assert "HYPRIAL_ZENOH_CONNECT" not in environment
    assert "HYPRIAL_WORKER_ACTOR" not in environment
    assert "HYPRIAL_WORKER_SESSION_REF" not in environment


def test_outbound_cli_binds_a_session_with_attachment_cwd(monkeypatch, tmp_path):
    runner = load_runner()
    outbound = runner._transports_outbound
    executable = tmp_path / "hyprial"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    monkeypatch.setenv("HYPRIAL_BIN", str(executable))
    calls = []
    registrations = []

    def request(self, method, params):
        registrations.append((self.root, method, params))
        return {"actor": "agent:e2e:e2e004-outbound-file:" + runner.OUTBOUND_SENDER}

    monkeypatch.setattr(outbound.IsolatedDaemon, "request", request)
    monkeypatch.setattr(runner, "injected_app_credentials", lambda: ("app", "fixture"))
    def cli(*args, **kwargs):
        calls.append(args)
        if args[2:5] == ("agent", "cell", "show"):
            return {"cell": {"revision": 3, "members": [{"ref": "harness", "role": "harness"}], "params": {}}}
        return {"ok": True}

    monkeypatch.setattr(runner, "hyprial_cli", cli)
    _, environment, _ = runner.prepare_outbound_daemon(tmp_path, chat_id="fixture-chat")
    root, method, params = registrations[0]
    assert root == tmp_path / "outbound-daemon"
    assert method == "session.register"
    assert params["actor"] == runner.OUTBOUND_SENDER
    assert params["cwd"] == str((tmp_path / "outbound-workspace").resolve())
    assert not root.is_relative_to(Path(params["cwd"]))
    assert environment["HYPRIAL_WORKER_SESSION_REF"] == params["sessionRef"]
    assert environment["HYPRIAL_WORKER_ACTOR"].endswith(":" + runner.OUTBOUND_SENDER)
    assert any(call[2:4] == ("agent", "create") for call in calls)
    [applied] = [call for call in calls if call[2:5] == ("agent", "cell", "apply")]
    assert applied[applied.index("--expected-revision") + 1] == "3"
    assert environment["E2E_OUTBOUND_USER_TARGET"] == "user:e2e"
    config = json.loads((tmp_path / "outbound-daemon" / "home" / "channels.json").read_text())
    assert config["gateways"][0]["defaultRoute"] == "e2e-outbound-file"
