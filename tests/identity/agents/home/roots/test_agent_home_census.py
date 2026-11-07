"""Read-only agent-home census and legacy-path observability."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from hyprial.identity import AgentToolProfile, resolve_agent_runtime_context
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

_RETIRED_PROFILE = AgentToolProfile(
    profile_id="agent-home-p2-allen-v1",
    git_author_name="Allen Woods",
    git_author_email="allenwoods@users.noreply.code.hyprial.com",
)


class _NoHarnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _NoPresence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


@pytest.fixture
def app(tmp_path: Path) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    instance = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="owner",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    instance._inbox = object()  # type: ignore[assignment]
    instance._harnesses = _NoHarnesses()  # type: ignore[assignment]
    instance._presence = _NoPresence()  # type: ignore[assignment]
    try:
        yield instance
    finally:
        instance.agents.close()


def _tree_snapshot(*roots: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for root in roots:
        for path in (root, *sorted(root.rglob("*"))):
            relative = f"{root.name}/{path.relative_to(root)}"
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if path.is_symlink():
                snapshot[relative] = ("symlink", mode, path.readlink().as_posix())
            elif path.is_file():
                # SQLite can update WAL read marks in the shared-memory page
                # during a SELECT. Its presence/mode stay guarded; durable DB,
                # WAL, receipt, plan, and all other bytes remain exact.
                snapshot[relative] = (
                    ("sqlite-shm", mode)
                    if path.name == "agents.sqlite3-shm"
                    else ("file", mode, path.read_bytes())
                )
            else:
                snapshot[relative] = ("directory", mode)
    return snapshot


def test_home_census_reports_every_agent_without_mutating_state(
    app: DaemonApplication, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    p2_home = home / "agents" / "p2"
    p2_config = p2_home / "config"
    p2_cwd = p2_home / "workspace"
    app.agents.create(
        "p2",
        cwd=str(p2_cwd),
        config={
            "sources": [{"path": str(p2_config), "required": True}],
            "discovery": "explicit-only",
        },
        preferred_harness="claude",
    )
    app.agents.create("legacy", cwd=str(tmp_path / "external"), preferred_harness="pi")
    resident_config = home / "agents" / "resident" / "config"
    app.agents.create(
        "resident",
        config={
            "sources": [{"path": str(resident_config), "required": True}],
            "discovery": "explicit-only",
        },
        preferred_harness="dsh",
    )
    app.agents.create_transfer_hosted(
        "received", pinned_owner="remote-owner", preferred_harness="codex"
    )

    before = _tree_snapshot(home, tmp_path / "state")
    result = app.handle("agent.home-census", {})
    after = _tree_snapshot(home, tmp_path / "state")

    assert after == before
    assert result["ok"] is True
    assert result["registry"] == {
        "path": str(tmp_path / "state" / "agents.sqlite3"),
        "version": app._agent_session_domains.agent.version,
    }
    assert isinstance(result["readAtMs"], int)
    rows = {row["actor"]: row for row in result["agents"]}
    assert set(rows) == {"legacy", "p2", "received", "resident"}
    assert rows["p2"] == {
        "actor": "p2",
        "uri": "agent:owner:node-1:p2",
        "preferredHarness": "claude",
        "lastHarness": None,
        "harness": "claude",
        "homeProvisioned": True,
        "explicitConfig": True,
        "configInsideHome": True,
        "cwdInsideHome": True,
        "residentOrDsh": False,
        "transferReceived": False,
        "mode": "p2",
        "legacyReason": None,
    }
    assert rows["legacy"]["mode"] == "legacy"
    assert rows["legacy"]["legacyReason"] == "no_config"
    assert rows["legacy"]["cwdInsideHome"] is False
    assert rows["resident"]["residentOrDsh"] is True
    assert rows["resident"]["legacyReason"] == "harness_not_p2"
    assert rows["received"]["transferReceived"] is True
    assert rows["received"]["harness"] == "codex"
    assert result["totals"] == {
        "agents": 4,
        "byModeHarness": [
            {"mode": "legacy", "harness": "codex", "count": 1},
            {"mode": "legacy", "harness": "dsh", "count": 1},
            {"mode": "legacy", "harness": "pi", "count": 1},
            {"mode": "p2", "harness": "claude", "count": 1},
        ],
        "homeUnprovisioned": 0,
    }


def test_home_census_reports_unprovisioned_agents_instead_of_failing(
    app: DaemonApplication, tmp_path: Path
) -> None:
    """A legacy agent with no home row is a finding, not a census failure.

    Production carries agents imported from legacy records without a home
    row (200 on the HQ host at the time of writing): the pre-fix census died
    on the first of them with ``not-provisioned`` instead of measuring them.
    """

    app.agents.create("legacy-import", cwd=str(tmp_path / "external"), preferred_harness="pi")
    registry_db = app._agent_registry._db  # noqa: SLF001
    with registry_db:
        registry_db.execute(
            "DELETE FROM lifecycle_resources WHERE resource_key = ?",
            ("agent-home:legacy-import",),
        )

    result = app.handle("agent.home-census", {})

    assert result["ok"] is True
    rows = {row["actor"]: row for row in result["agents"]}
    assert rows["legacy-import"]["homeProvisioned"] is False
    assert rows["legacy-import"]["mode"] == "legacy"
    assert result["totals"]["homeUnprovisioned"] == 1


def test_ensure_home_provisions_the_official_p2_home_for_a_legacy_agent(
    app: DaemonApplication, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    app.agents.create("migrate-me", cwd=str(tmp_path / "external"), preferred_harness="pi")
    registry_db = app._agent_registry._db  # noqa: SLF001
    with registry_db:
        registry_db.execute(
            "DELETE FROM lifecycle_resources WHERE resource_key = ?",
            ("agent-home:migrate-me",),
        )
    # A true legacy agent has neither the row nor the on-disk tree.
    shutil.rmtree(home / "agents" / "migrate-me")

    result = app.handle("agent.ensure-home", {"name": "migrate-me"})

    assert result["ok"] is True
    assert result["actor"] == "migrate-me"
    assert result["agent"] == "agent:owner:node-1:migrate-me"
    receipt = result["home"]
    assert receipt["actor"] == "migrate-me"
    assert receipt["status"] == "ready"
    assert receipt["path"] == str(home / "agents" / "migrate-me")

    # The durable row is active and matches the on-disk mirror.
    row = registry_db.execute(
        "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
        ("agent-home:migrate-me",),
    ).fetchone()
    assert bool(row["active"])
    assert json.loads(row["payload"]) == receipt
    agent_root = home / "agents" / "migrate-me"
    assert not (agent_root / "secrets").exists()
    for child in (agent_root, *(agent_root / part for part in ("config", "state"))):
        assert stat.S_IMODE(child.lstat().st_mode) == 0o700
    mirror = json.loads(
        (agent_root / "state" / "home-receipt.json").read_text(encoding="utf-8")
    )
    assert mirror == receipt
    assert stat.S_IMODE((agent_root / "state" / "home-receipt.json").lstat().st_mode) == 0o600

    # Idempotent: a second call validates and returns the same receipt.
    again = app.handle("agent.ensure-home", {"name": "migrate-me"})
    assert again["home"] == receipt
    census = app.handle("agent.home-census", {})
    rows = {row["actor"]: row for row in census["agents"]}
    assert rows["migrate-me"]["homeProvisioned"] is True


def test_ensure_home_unknown_agent_fails_with_agent_not_found(
    app: DaemonApplication,
) -> None:
    with pytest.raises(Exception) as raised:
        app.handle("agent.ensure-home", {"name": "never-registered"})
    assert "never-registered" in str(raised.value)


def test_home_census_cli_is_in_agent_help_and_preserves_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "ok": True,
        "readAtMs": 1,
        "registry": {"path": "/isolated/agents.sqlite3", "version": 2},
        "agents": [],
        "totals": {"agents": 0, "byModeHarness": []},
    }
    calls: list[tuple[str, object]] = []

    def request(method: str, params: object = None) -> object:
        calls.append((method, params))
        return payload

    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()

    helped = runner.invoke(_cli_root_app, ["agent", "home", "--help"])
    result = runner.invoke(_cli_root_app, ["agent", "home", "census", "--json"])

    assert helped.exit_code == 0
    assert "census" in helped.output
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
    assert calls == [("agent.home-census", None)]


def test_legacy_path_log_is_once_per_actor_per_daemon(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.agents.create("legacy", preferred_harness="claude")
    app.agents.create("other", preferred_harness="claude")
    configured_source = Path(app.hyprial_home) / "agents" / "configured" / "config"
    app.agents.create(
        "configured",
        config={
            "sources": [{"path": str(configured_source), "required": True}],
            "discovery": "explicit-only",
        },
        preferred_harness="claude",
    )
    events: list[tuple[str, str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda level, component, event, **fields: events.append(
            (level, component, event, fields)
        ),
    )

    for _ in range(2):
        result = app.handle(
            "agent.runtime-context",
            {"name": "legacy", "harness": "claude", "cwd": None},
        )
        assert result["mode"] == "legacy"
    app.handle(
        "agent.runtime-context",
        {"name": "other", "harness": "unsupported", "cwd": None},
    )
    app.handle(
        "agent.runtime-context",
        {"name": "configured", "harness": "unsupported", "cwd": None},
    )

    assert events == [
        (
            "warn",
            "daemon",
            "agent.home.legacy_path",
            {"actor": "agent:owner:node-1:legacy", "reason": "no_config"},
        ),
        (
            "warn",
            "daemon",
            "agent.home.legacy_path",
            {"actor": "agent:owner:node-1:other", "reason": "no_config"},
        ),
        (
            "warn",
            "daemon",
            "agent.home.legacy_path",
            {
                "actor": "agent:owner:node-1:configured",
                "reason": "harness_not_p2",
            },
        ),
    ]


def test_migration_notice_is_logged_once_per_actor_on_reresolution(
    app: DaemonApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headless launch path logs AGENT_GIT_PROFILE_MIGRATED exactly once.

    Red-first for review feedback on f98c63e248: before the wiring existed,
    the notice reached only the returned context JSON and the interactive
    CLI warn, never the daemon's structured log on headless launch.
    """

    configured_source = tmp_path / "personality"
    (configured_source / "native" / "pi").mkdir(parents=True)
    (configured_source / "native" / "pi" / "settings.json").write_text("{}\n")
    skill = configured_source / "shared" / "skills" / "p22" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# P22\n", encoding="utf-8")
    app.agents.create(
        "p2worker",
        config={
            "sources": [{"path": str(configured_source), "required": True}],
            "discovery": "explicit-only",
        },
    )
    cwd = str(tmp_path / "p2worker" / "work")

    legacy = resolve_agent_runtime_context(
        registry=app._agent_session_domains._registry,
        agent_name="p2worker",
        harness="pi",
        cwd=cwd,
        tool_profile=_RETIRED_PROFILE,
    )
    assert legacy is not None
    assert legacy.tool_profile_id == "agent-home-p2-allen-v1"

    migrated = resolve_agent_runtime_context(
        registry=app._agent_session_domains._registry,
        agent_name="p2worker",
        harness="pi",
        cwd=cwd,
        tool_profile=None,
    )
    assert migrated is not None
    assert migrated.tool_profile_id.startswith("agent-git-v1-")
    assert len(migrated.notices) == 1
    notice = migrated.notices[0]

    events: list[tuple[str, str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda level, component, event, **fields: events.append(
            (level, component, event, fields)
        ),
    )
    app._report_agent_runtime_notices(migrated)
    app._report_agent_runtime_notices(migrated)

    assert events == [
        (
            "warn",
            "daemon",
            "AGENT_GIT_PROFILE_MIGRATED",
            {
                "actor": notice.actor,
                "path": notice.path,
                "message": notice.message,
            },
        )
    ]


def test_public_p2_defaults_and_census_share_classifier_without_writes(
    app: DaemonApplication, tmp_path: Path,
) -> None:
    app.handle("agent.create", {"name": "public-p2", "preferredHarness": "pi"})
    app.agents.create("legacy-direct", preferred_harness="pi")
    before = _tree_snapshot(tmp_path / "home", tmp_path / "state")
    census = app.handle("agent.home-census", {})
    assert _tree_snapshot(tmp_path / "home", tmp_path / "state") == before
    rows = {row["actor"]: row for row in census["agents"]}
    assert rows["public-p2"]["mode"] == "p2"
    assert rows["public-p2"]["configInsideHome"] is True
    assert rows["public-p2"]["cwdInsideHome"] is True
    assert rows["legacy-direct"]["mode"] == "legacy"
    assert rows["legacy-direct"]["legacyReason"] == "no_config"


def test_legacy_report_uses_owner_decision_after_projection_changes(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:

    legacy = app.agents.create("legacy-decision", preferred_harness="claude")
    actor = app._agent_session_domains.agent
    original = actor._call_home_access
    reported = []

    def settle_then_lose_projection(command):
        result = original(command)
        # An observer can lose/replace the projection after the owner chose
        # legacy. Logging must still describe that settled choice.
        monkeypatch.setattr(actor, "read_agent", lambda _name: None)
        return result

    monkeypatch.setattr(actor, "_call_home_access", settle_then_lose_projection)
    result = actor.prepare_runtime_context(
        legacy.actor, "claude", None, TEST_TOOL_PROFILE,
        legacy_reporter=lambda uri, reason: reported.append((uri, reason)),
    )
    assert result is None
    assert reported == [(legacy.uri, "no_config")]


def _ensure_home_cli_environment(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, object],
) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []

    def request(method: str, params: object = None) -> object:
        calls.append((method, params))
        reply = responses.get(method)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(params)
        return reply if reply is not None else {"ok": True}

    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    return calls


def test_ensure_home_cli_provisions_named_agents_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.kernel import DaemonRequestError

    def ensure_reply(params: object) -> object:
        if params["name"] == "broken":
            raise DaemonRequestError("AGENT_ERROR", "boom")
        return {"ok": True, "agent": f"agent:owner:node-1:{params['name']}", "home": {"actor": params["name"], "status": "ready"}}

    calls = _ensure_home_cli_environment(
        monkeypatch,
        {
            "agent.ensure-home": ensure_reply,
        },
    )
    runner = CliRunner()
    result = runner.invoke(_cli_root_app, ["agent", "ensure-home", "fine", "broken", "--json"])

    # A result carrying ok:false exits 1 (the CLI emit contract); the JSON
    # payload is still complete on stdout.
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["provisioned"] == 1
    assert payload["failed"] == 1
    assert [item["actor"] for item in payload["results"]] == ["fine", "broken"]
    assert payload["results"][1]["ok"] is False
    assert [call[0] for call in calls] == ["agent.ensure-home", "agent.ensure-home"]


def test_ensure_home_cli_all_legacy_defers_running_and_excludes_wf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    census = {
        "ok": True,
        "agents": [
            {"actor": "offline-legacy", "homeProvisioned": False},
            {"actor": "running-legacy", "homeProvisioned": False},
            {"actor": "wf-1234", "homeProvisioned": False},
            {"actor": "already-p2", "homeProvisioned": True},
        ],
        "totals": {"agents": 4, "homeUnprovisioned": 3},
    }
    listing = {
        "ok": True,
        "agents": [
            {"actor": "offline-legacy", "status": "offline"},
            {"actor": "running-legacy", "status": "running"},
            {"actor": "wf-1234", "status": "offline"},
            {"actor": "already-p2", "status": "offline"},
        ],
    }
    calls = _ensure_home_cli_environment(
        monkeypatch,
        {
            "agent.home-census": census,
            "agent.list": listing,
            "agent.ensure-home": {"ok": True, "home": {"status": "ready"}},
        },
    )
    runner = CliRunner()
    result = runner.invoke(_cli_root_app, ["agent", "ensure-home", "--all-legacy", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provisioned"] == 1
    assert payload["deferred"] == ["running-legacy"]
    ensure_calls = [params["name"] for method, params in calls if method == "agent.ensure-home"]
    assert ensure_calls == ["offline-legacy"]


def test_ensure_home_cli_dry_run_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _ensure_home_cli_environment(
        monkeypatch,
        {
            "agent.home-census": {"ok": True, "agents": [{"actor": "offline-legacy", "homeProvisioned": False}]},
            "agent.list": {"ok": True, "agents": [{"actor": "offline-legacy", "status": "offline"}]},
        },
    )
    runner = CliRunner()
    result = runner.invoke(
        _cli_root_app, ["agent", "ensure-home", "--all-legacy", "--dry-run", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dryRun"] is True
    assert payload["selected"] == ["offline-legacy"]
    assert payload["deferred"] == []
    assert [call[0] for call in calls] == ["agent.home-census", "agent.list"]


def test_ensure_home_survives_a_daemon_rebuild(
    tmp_path: Path,
) -> None:
    """The provisioned home and its row survive daemon down + up.

    The legacy-host restart was the original failure trigger: after
    ``hyprial agent stop`` the P2 read path answered ``not-provisioned`` with no
    official rebuild.  Two sequential application incarnations on the same
    state dir stand in for down + up: the second must validate the first
    one's receipt, not mint a second home incarnation.
    """

    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    def build() -> DaemonApplication:
        return DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            node_id="node-1",
            owner="owner",
            hyprial_home=home,
            **_deep_module_daemon_dependencies(),
        )

    first = build()
    first._inbox = object()  # type: ignore[assignment]
    first._harnesses = _NoHarnesses()  # type: ignore[assignment]
    first._presence = _NoPresence()  # type: ignore[assignment]
    try:
        first.agents.create("survivor", preferred_harness="pi")
        registry_db = first._agent_registry._db  # noqa: SLF001
        with registry_db:
            registry_db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                ("agent-home:survivor",),
            )
        shutil.rmtree(home / "agents" / "survivor")
        provisioned = first.handle("agent.ensure-home", {"name": "survivor"})
    finally:
        first.agents.close()

    second = build()
    second._inbox = object()  # type: ignore[assignment]
    second._harnesses = _NoHarnesses()  # type: ignore[assignment]
    second._presence = _NoPresence()  # type: ignore[assignment]
    try:
        again = second.handle("agent.ensure-home", {"name": "survivor"})
        assert again["home"] == provisioned["home"]
        census = second.handle("agent.home-census", {})
        rows = {row["actor"]: row for row in census["agents"]}
        assert rows["survivor"]["homeProvisioned"] is True
    finally:
        second.agents.close()
