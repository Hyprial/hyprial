"""P1a T01/T02/T13: registry-owned agent homes and incarnation fences."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import (
    AgentRegistry,
    SecretResolver,
    SecretSource,
)
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.kernel.impl.contracts.ports import PortCommandRejected
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.composition import (
    DomainCommandError,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


_OPEN_REGISTRIES: list[AgentRegistry] = []
_OPEN_APPLICATIONS: list[DaemonApplication] = []


def _close_application(application: DaemonApplication) -> None:
    application._close()
    application._close_home_lease()
    _OPEN_APPLICATIONS.remove(application)


@pytest.fixture(autouse=True)
def _isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "process-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    yield
    while _OPEN_APPLICATIONS:
        _close_application(_OPEN_APPLICATIONS[-1])
    while _OPEN_REGISTRIES:
        _OPEN_REGISTRIES.pop().close()


def _track(registry: AgentRegistry) -> AgentRegistry:
    _OPEN_REGISTRIES.append(registry)
    return registry


def _registry(tmp_path: Path, *, owner: str = "owner") -> AgentRegistry:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    return _track(
        AgentRegistry(
            tmp_path / "state" / "agents.sqlite3",
            owner=owner,
            machine="node",
            hyprial_home=home,
        )
    )


def _application(tmp_path: Path) -> DaemonApplication:
    class EmptyInbox:
        @staticmethod
        def pending_messages(_recipient: str) -> tuple[()]:
            return ()

    class EmptyHarnesses:
        @staticmethod
        def status() -> tuple[()]:
            return ()

    class OfflinePresence:
        @staticmethod
        def actor_online(_actor: str) -> bool:
            return False

    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="owner",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    application._inbox = EmptyInbox()  # type: ignore[assignment]
    application._harnesses = EmptyHarnesses()  # type: ignore[assignment]
    application._presence = OfflinePresence()  # type: ignore[assignment]
    _OPEN_APPLICATIONS.append(application)
    return application


class _AgentEvents:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[object] = []

    def __call__(self, event: object) -> None:
        with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    def wait(self, correlation_id: str, expected: type[object]) -> object:
        deadline = time.monotonic() + 3.0
        with self._condition:
            while True:
                match = next(
                    (
                        event
                        for event in self._events
                        if isinstance(event, expected)
                        and getattr(event, "correlation_id", None) == correlation_id
                    ),
                    None,
                )
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                assert remaining > 0
                self._condition.wait(remaining)

    def wait_domain(self, correlation_id: str, expected: type[object]) -> object:
        deadline = time.monotonic() + 3.0
        with self._condition:
            while True:
                events = [
                    event
                    for event in self._events
                    if getattr(event, "correlation_id", None) == correlation_id
                ]
                rejection = next(
                    (
                        event
                        for event in events
                        if isinstance(event, PortCommandRejected)
                    ),
                    None,
                )
                if rejection is not None:
                    raise DomainCommandError(rejection.code, rejection.detail)
                match = next(
                    (event for event in events if isinstance(event, expected)),
                    None,
                )
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                assert remaining > 0
                self._condition.wait(remaining)


def _configured_launch_actor(
    tmp_path: Path,
    *,
    model_provider: str | None = None,
    seeded_codex_auth: bytes | None = None,
):
    source = tmp_path / "personality"
    config_file = source / "native" / "codex" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('cli_auth_credentials_store = "file"\n')
    registry = _registry(tmp_path)
    registry.create(
        "worker",
        cwd=str(tmp_path / "work"),
        config={
            "sources": [{"path": str(source), "required": True}],
            "discovery": "explicit-only",
        },
        provider=model_provider,
    )
    if seeded_codex_auth is not None:
        native = (
            Path(registry.home_receipt("worker").path)
            / "secrets"
            / "native"
            / "codex"
        )
        native.mkdir(parents=True, exist_ok=True)
        for private in (native.parents[1], native.parent, native):
            private.chmod(0o700)
        auth = native / "auth.json"
        auth.write_bytes(seeded_codex_auth)
        auth.chmod(0o600)
    events = _AgentEvents()
    from hyprial.daemon.impl.harnesses.codex.native_env import prepare_codex_runtime_context

    actor = AgentActor(
        registry,
        event_sink=events,
        home_runtime_preparers={"codex": prepare_codex_runtime_context},
    )
    context = actor.prepare_runtime_context(
        "worker", "codex", str(tmp_path / "work"), TEST_TOOL_PROFILE
    )
    assert context is not None and context.launch_token
    return registry, actor, events, context


# --------------------------------------------------------------------------- #
# R2: reading failure is not absence (读不到 ≠ 没有, #513 comment 12723)     #
# --------------------------------------------------------------------------- #


def _old_owner_state_with_live_grant(tmp_path: Path) -> Path:
    """A closed registry whose state holds one foreign owner + one grant."""

    registry = _registry(tmp_path, owner="old-owner")
    resolver = SecretResolver(tmp_path / "home", registry)
    registry.create("worker")
    resolver.write_user_provider("deepseek", {"apiKey": "sentinel"})
    registry.grant_secret(
        "worker",
        grant_id="deepseek",
        source=SecretSource.USER_PROVIDER,
        entry_id="deepseek",
        field_name="apiKey",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )
    registry.close()
    return tmp_path / "state"


# --------------------------------------------------------------------------- #
# A3: legacy/mixed-version databases and the entity_token contract           #
# --------------------------------------------------------------------------- #


def _agents_table_facts(database: Path) -> tuple[tuple[tuple[str, int, str], ...], set]:
    """(column, notnull, type) rows plus the (columns, unique) index set."""

    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    try:
        columns = tuple(
            (str(row["name"]), int(row["notnull"]), str(row["type"]))
            for row in db.execute("PRAGMA table_info(agents)")
        )
        indexes: set = set()
        for row in db.execute("PRAGMA index_list(agents)"):
            columns_of_index = tuple(
                str(item[2])
                for item in db.execute(f"PRAGMA index_info({row[1]})")
            )
            indexes.add((columns_of_index, bool(row[2])))
    finally:
        db.close()
    return columns, indexes


def _legacy_agents_database(state: Path) -> None:
    """A pre-feature 13-column agents database (no entity_token column)."""

    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute(
            "CREATE TABLE agents ("
            "actor TEXT PRIMARY KEY, owner TEXT NOT NULL, machine TEXT NOT NULL, "
            "uri TEXT NOT NULL UNIQUE, cwd TEXT, provider TEXT, model TEXT, "
            "capabilities TEXT NOT NULL DEFAULT '{}', "
            "harness_args TEXT NOT NULL DEFAULT '{}', preferred_harness TEXT, "
            "last_harness TEXT, last_session_id TEXT, "
            "created_at_ms INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO agents (actor, owner, machine, uri, capabilities, "
            "harness_args, created_at_ms) VALUES "
            "('squire', 'old-owner', 'node', 'agent:old-owner:node:squire', "
            "'{}', '{}', 1)"
        )
    db.close()


def _nullable_token_database(state: Path, *, tokens: dict[str, str | None]) -> None:
    """The intermediate shape: entity_token exists but is nullable.

    This is what a database migrated by an earlier build of this branch and
    then written by a mixed-version binary looks like.
    """

    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute(
            "CREATE TABLE agents ("
            "actor TEXT PRIMARY KEY, owner TEXT NOT NULL, machine TEXT NOT NULL, "
            "uri TEXT NOT NULL UNIQUE, entity_token TEXT, cwd TEXT, provider TEXT, "
            "model TEXT, capabilities TEXT NOT NULL DEFAULT '{}', "
            "harness_args TEXT NOT NULL DEFAULT '{}', preferred_harness TEXT, "
            "last_harness TEXT, last_session_id TEXT, "
            "created_at_ms INTEGER NOT NULL, "
            "hosted_by TEXT DEFAULT NULL)"
        )
        for name, token in tokens.items():
            db.execute(
                "INSERT INTO agents (actor, owner, machine, uri, entity_token, "
                "capabilities, harness_args, created_at_ms) VALUES "
                "(?, 'old-owner', 'node', ?, ?, '{}', '{}', 1)",
                (name, f"agent:old-owner:node:{name}", token),
            )
        db.execute(
            "CREATE TABLE agent_secret_grants ("
            "agent TEXT NOT NULL, entity_token TEXT, grant_id TEXT NOT NULL, "
            "source TEXT NOT NULL, entry_id TEXT NOT NULL, field_name TEXT, "
            "environment_names TEXT NOT NULL, revision INTEGER NOT NULL, "
            "PRIMARY KEY (agent, grant_id))"
        )
    db.close()
