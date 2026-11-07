"Login U5 — the account switch, the removed manual owner flags, D7 pinned.\n\nSpec: ``hq/notes/spec-hyprial-login-u5-switch-2026-09-04.md`` §1.1-1.4, tests\nT1-T6.  The fake OIDC, zero-write snapshots and home fixtures mirror\n``tests/shell/login/test_login.py`` (login U2); the migration assertions reuse the\nseeding shapes of ``tests/identity/ownership/test_owner_migration.py`` (#352) — the migration\nmechanism itself is *not* under test here and is not modified by U5.\n\nThe autouse home fixture sets ``HYPRIAL_OWNER``; every test here clears or\nmatches it explicitly (the spec's fixture reminder).\n"

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests/fixtures'))

from hyprial.daemon.impl.configuration.identity import (
    resolve_node_owner,
    write_settings_owner,
)
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.daemon.impl.owner_migration import migrate_owner_if_needed
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from hyprial.shell.impl.login.flow import LoginError, run_login
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402

OLD = "old-owner"
NEW = "alicesmith"  # what the fake OIDC answers by default


# -- helpers (mirrors of tests/test_login.py; the switch adds state) ----------


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _profile_for(server) -> NetworkProfile:
    return NetworkProfile(
        org="acme",
        issuer=server.issuer,
        client_id="fake-client-id",
    )


def _snapshot(home: Path) -> dict[str, bytes | None]:
    """Every file except the heartbeat scratch files — those are the daemon's,
    not login's, and T2 writes one deliberately."""

    snap: dict[str, bytes | None] = {}
    for path in sorted(home.rglob("*")):
        if path.name.startswith(".active_daemon"):
            continue
        snap[str(path.relative_to(home))] = (
            path.read_bytes() if path.is_file() else None
        )
    return snap


def _run(server, home, *, env=None, **kwargs):
    environ = {"HYPRIAL_HOME": str(home)}
    if env is not None:
        environ.update(env)
    environ.setdefault("HYPRIAL_OWNER", "")
    return run_login(
        _profile_for(server),
        profile_source="profile.json",
        environ=environ,
        hyprial_home=home,
        open_browser=False,
        **kwargs,
    )


def _seed_state(home: Path, owner: str) -> Path:
    """A minimal migrated-or-not state dir: one agents.sqlite3 whose rows name
    ``owner`` — the read :func:`detect_previous_owner` does (same shape as
    ``test_owner_migration._seed_agents``)."""

    state = home / "state"
    state.mkdir(exist_ok=True)
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute("CREATE TABLE agents (actor TEXT, owner TEXT, uri TEXT, cwd TEXT)")
        db.execute(
            "INSERT INTO agents VALUES (?, ?, ?, ?)",
            ("squire", owner, f"agent:{owner}:node-hq-01:squire", "/Users/x/w"),
        )
    db.close()
    return state


def _live_heartbeat(home: Path) -> None:
    """Write a heartbeat a live daemon could have written: this process's own
    pid and birth-marker, a fresh monotonic timestamp (the exact record
    ActiveDaemonHeartbeat maintains)."""

    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    identity = _read_process_identity(os.getpid())
    assert identity, "test cannot establish its own process identity"
    (home / ".active_daemon").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pid": os.getpid(),
                "processIdentity": identity,
                "generation": "u5-test-generation",
                "heartbeatAt": "2026-09-04T00:00:00Z",
                "heartbeatMonotonic": time.monotonic(),
                "keepaliveDurationSeconds": 5.0,
            }
        ),
        encoding="utf-8",
    )


# -- T1: same owner ⇒ zero migration (spec §1.1) ------------------------------


def test_t1_same_owner_login_leaves_settings_bytes_and_migration_zero(
    tmp_path: Path,
) -> None:
    """The main path off manual owners (design §5.1): a node whose
    ``settings.owner`` already equals the login identity rewrites the same
    value and the first daemon start after that reports
    ``identity.owner_migration.applied rewrittenCells=0``."""

    home = _home(tmp_path)
    write_settings_owner(NEW, environ={"HYPRIAL_HOME": str(home)})  # + no other keys
    write_settings_owner(  # add an unrelated key the way a live home has them
        NEW, environ={"HYPRIAL_HOME": str(home)}
    )
    (home / "settings.json").write_text(
        json.dumps({"updateTrack": "dev", "owner": NEW}, indent=2) + "\n",
        encoding="utf-8",
    )
    state = _seed_state(home, NEW)  # state already under the login identity

    with fake_oidc() as server:
        result = _run(server, home)

    assert result.owner == NEW
    assert result.switched is False
    assert result.previous_owner is None
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings == {
        "updateTrack": "dev",
        "owner": NEW,
        "userId": server.config.sub,
        "username": NEW,
    }
    # The number the startup hook logs (application.py logs it
    # unconditionally; here it is the value that log would carry).
    assert migrate_owner_if_needed(state_dir=state, hyprial_home=home, owner=NEW) == 0


# -- T2: --switch-account requires the daemon stopped (spec §1.2, D-U5-1) -----


def test_t2_switch_refused_while_daemon_runs_zero_writes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    before = _snapshot(home)
    _live_heartbeat(home)

    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, switch_account=True, assume_yes=True)

    assert caught.value.code == "DAEMON_RUNNING"
    assert caught.value.data["pid"] == os.getpid()
    assert caught.value.data["currentOwner"] == OLD
    assert caught.value.data["loginOwner"] == NEW
    # the refusal names how to stop it (D-U5-1: login does not stop it itself)
    assert "hyprial daemon stop" in str(caught.value)
    assert _snapshot(home) == before


def test_t2_stale_heartbeat_reads_as_stopped(tmp_path: Path) -> None:
    """A heartbeat whose keepalive window has lapsed is not a live daemon —
    the same freshness rule the home guard itself applies."""

    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    (home / ".active_daemon").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pid": os.getpid(),
                "processIdentity": _read_process_identity(os.getpid()),
                "generation": "stale",
                "heartbeatMonotonic": time.monotonic() - 10_000.0,
                "keepaliveDurationSeconds": 5.0,
            }
        ),
        encoding="utf-8",
    )

    with fake_oidc() as server:
        result = _run(server, home, switch_account=True, assume_yes=True)

    assert result.switched is True
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == NEW


def test_t2_declined_confirmation_writes_nothing(tmp_path: Path) -> None:
    """TTY path: the operator is shown ``old -> new`` and must type yes."""

    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    before = _snapshot(home)
    asked: list[str] = []

    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            _run(
                server,
                home,
                switch_account=True,
                prompt=lambda message: asked.append(message) or "no",
            )

    assert caught.value.code == "SWITCH_DECLINED"
    assert OLD in asked[0] and NEW in asked[0]  # the old -> new line was shown
    assert _snapshot(home) == before


# -- T3: switch writes credential first, then owner; migration on start -------


def test_t3_switch_writes_credential_first_then_owner(tmp_path: Path) -> None:
    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    order: list[str] = []

    def observing_write_owner(owner, **_kwargs):
        credential = home / "secrets" / "login.json"
        order.append("credential" if credential.exists() else "MISSING")
        order.append(f"owner={owner}")
        return write_settings_owner(owner, **_kwargs)

    with fake_oidc() as server:
        result = _run(
            server,
            home,
            switch_account=True,
            assume_yes=True,
            write_owner=observing_write_owner,
        )

    assert result.switched is True
    assert result.previous_owner == OLD
    # credential first, owner second (design §2.2 step 8's one-way commit)
    assert order[0] == "credential"
    assert order[1] == f"owner={NEW}"
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == NEW


def test_t3_first_start_migrates_second_start_is_zero(tmp_path: Path) -> None:
    """After a switch, the next daemon start rewrites (rewrittenCells>0,
    agents.owner all new) and the one after that reports 0 — the #352 hook,
    untouched by U5, doing the migration login deliberately does not."""

    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    state = _seed_state(home, OLD)

    with fake_oidc() as server:
        _run(server, home, switch_account=True, assume_yes=True)
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == NEW

    first = migrate_owner_if_needed(state_dir=state, hyprial_home=home, owner=NEW)
    assert first > 0
    db = sqlite3.connect(state / "agents.sqlite3")
    owners = [row[0] for row in db.execute("SELECT owner FROM agents")]
    uris = [row[0] for row in db.execute("SELECT uri FROM agents")]
    db.close()
    assert owners == [NEW]
    assert uris == [f"agent:{NEW}:node-hq-01:squire"]

    second = migrate_owner_if_needed(state_dir=state, hyprial_home=home, owner=NEW)
    assert second == 0


def test_t3_switch_without_flag_still_refuses_zero_writes(tmp_path: Path) -> None:
    """U2 behavior unchanged by U5: the flag is the only opt-in."""

    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    before = _snapshot(home)

    with fake_oidc() as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, switch_account=False, assume_yes=True)

    assert caught.value.code == "OWNER_MISMATCH"
    assert caught.value.data["flag"] == "--switch-account"
    assert _snapshot(home) == before


def test_t3_cli_json_switch_result_shape(tmp_path: Path, monkeypatch) -> None:
    """``--json`` output for a switch: identity.status=switched,
    previousOwner, owner, nextStep="hyprial init" (spec §1.2 step 5)."""

    home = _home(tmp_path)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    # Pin the "no sidecar" premise at the seam instead of relying on the host.
    # `locate_tailcat_sidecar` searches HYPRIAL_TAILCAT_BINARY > wheel > home/bin,
    # so a shared working tree that happens to carry an untracked
    # src/hyprial/_bin/hyprial-tailcat makes the device stage succeed and this
    # test red on that machine only.  An explicit missing HYPRIAL_TAILCAT_BINARY
    # would work today but self-invalidates if that precedence ever changes; the
    # stub keeps testing D12 itself ("login still succeeds without a sidecar").
    from hyprial.daemon.impl.network.tailcat.binary import TailcatSidecarError

    def _no_sidecar(*_args, **_kwargs):
        raise TailcatSidecarError(
            "SIDECAR_MISSING", "pinned: no sidecar in this test"
        )

    # The login device stage reaches the sidecar through tailcat.keys, which
    # imports the name into its own module: the stub must be bound where the
    # name is USED, not where it is defined.  (Patching only tailcat.binary
    # leaves the already-bound reference in keys untouched, and the test stays
    # red on a tree that carries an untracked sidecar.)
    import hyprial.daemon.impl.network.tailcat.keys as tailcat_keys

    monkeypatch.setattr(tailcat_keys, "locate_tailcat_sidecar", _no_sidecar)

    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        # The CLI resolves the profile from the home; the drill/U2 seam
        # applies: point the home's profile at the fake through the CLI's
        # imported name so `hyprial login` runs against the loopback issuer.
        monkeypatch.setattr(
            _cli_get_services(),
            "resolve_profile",
            lambda: (_profile_for(server), "profile.json"),
        )
        runner = CliRunner()
        outcome = runner.invoke(
            app,
            [
                "login",
                "--json",
                "--switch-account",
                "--no-daemon",
                "--no-open",
            ],
            input="",
        )

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert payload["identity"]["status"] == "switched"
    assert payload["identity"]["previousOwner"] == OLD
    assert payload["identity"]["owner"] == NEW
    assert payload["identity"]["nextStep"] == (
        "start the daemon to apply and verify migration"
    )
    # No sidecar in the test home: the device stage reports not-ready and
    # the login still succeeds (D12).
    assert payload["device"]["ready"] is False
    assert payload["device"]["error"]["code"] == "SIDECAR_MISSING"
    assert "network" not in payload


# -- T4: the manual owner flags are gone (spec §1.3) --------------------------


def test_t4_init_owner_is_gone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(_home(tmp_path)))
    monkeypatch.setenv("HYPRIAL_OWNER", "someone")
    result = CliRunner().invoke(app, ["init", "--owner", "x", "--json"])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output or "--owner" not in result.output


def test_t4_squire_setup_owner_is_gone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(_home(tmp_path)))
    monkeypatch.setenv("HYPRIAL_OWNER", "someone")
    result = CliRunner().invoke(app, ["squire", "setup", "--owner", "x", "--json"])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output or "--owner" not in result.output


def test_t4_squire_setup_owner_comes_from_the_resolver(
    tmp_path: Path, monkeypatch
) -> None:
    """``--owner-key`` and friends survive (host-side lookup keys, design
    §3.3); the owner itself is resolved from the home's identity."""

    import hyprial.daemon as squire_module

    home = _home(tmp_path)
    write_settings_owner(NEW, environ={"HYPRIAL_HOME": str(home)})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", NEW)

    captured: dict[str, object] = {}

    class _StubSetup:
        def __init__(self, *, hyprial_home, state_dir, management_port):  # noqa: ANN001
            captured["constructed"] = True

        def run(self, identity, **_kwargs):  # noqa: ANN001
            captured["identity"] = identity
            return {"ok": True, "steps": []}

    monkeypatch.setattr(squire_module, "SquireSetup", _StubSetup)
    result = CliRunner().invoke(
        app,
        [
            "squire",
            "setup",
            "--owner-key",
            "owner-key-1",
            "--login-name",
            "login-1",
            "--machine",
            "machine-1",
            "--machine-key",
            "machine-key-1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["constructed"] is True
    assert captured["identity"].owner == NEW  # from settings.json, no flag
    assert captured["identity"].owner_key == "owner-key-1"  # lookup keys stay


def test_t4_missing_owner_error_points_at_login_not_init_owner(
    tmp_path: Path, monkeypatch
) -> None:
    home = _home(tmp_path)  # no settings.json, no owner anywhere
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "squire",
            "setup",
            "--owner-key",
            "k",
            "--login-name",
            "l",
            "--machine",
            "m",
            "--machine-key",
            "mk",
            "--json",
        ],
    )
    assert result.exit_code != 0
    assert "hyprial login" in result.output
    assert "init --owner" not in result.output


def test_t4_resolver_guidance_names_real_commands_only() -> None:
    '``tests/shell/cli/identity/test_identity_guidance.py`` reads the docstring and the failure\n    message; this pins the U5 wording directly: login yes, the removed flag\n    no.'

    with pytest.raises(ValueError) as caught:
        resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=Path("/nonexistent-u5"))
    message = str(caught.value)
    assert "hyprial login" in message
    assert "init --owner" not in message
    assert "HYPRIAL_OWNER" in message
    docstring = resolve_node_owner.__doc__ or ""
    assert "init --owner" not in docstring
    assert "squire setup --owner" not in docstring


# -- T5: D7 pinned (spec §1.4) -------------------------------------------------


def test_t5_d7_docstring_pins_the_override_semantics() -> None:
    """D7's definition sentence, anchored so it cannot silently rot: the
    override is for tests/managed deployments and a differing login errors
    before writing.  The env>settings order itself is pinned by
    ``test_actor_identity.test_owner_source_order_env_then_settings_then_loud_failure``
    (referenced, not rewritten); the login-side conflict is U2's T7."""

    docstring = " ".join((resolve_node_owner.__doc__ or "").split())
    assert "override for tests and managed deployments only" in docstring
    assert "before writing anything" in docstring


# -- T6: no host-login fallback, anywhere in src (spec §1.3/§1.4) --------------


def test_t6_no_getpass_getuser_call_in_src() -> None:
    """The grep criterion as an AST walk: CALLS only — the historical
    explanation in ``owner_migration``'s docstring mentions
    ``getpass.getuser()`` as text and is deliberately legal, and
    ``cli.py``'s ``getpass.getpass`` is password input, not identity.  The
    identity-module import guard lives in
    ``test_actor_identity.test_owner_never_falls_back_to_the_host_login``."""

    root = Path(__file__).resolve().parents[3] / 'src'
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "getuser"
                and isinstance(function.value, ast.Name)
                and function.value.id == "getpass"
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"getpass.getuser( call(s) reintroduced: {offenders}"
