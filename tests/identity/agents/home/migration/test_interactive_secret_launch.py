"""Interactive launch secrets cross only the one acquire response."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import AgentSecretStore
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)


CANARY = "HYPRIAL-CANARY-9d76c82d-f204-4be4-a7d8-f606a23e0edc"


class _Harnesses:
    @staticmethod
    def status() -> tuple[object, ...]:
        return ()


class _Presence:
    @staticmethod
    def actor_online(_actor: str) -> bool:
        return False

    @staticmethod
    def online_actors() -> tuple[str, ...]:
        return ()

    @staticmethod
    def online_mailboxes() -> tuple[str, ...]:
        return ()


@pytest.fixture
def secret_app(tmp_path: Path) -> tuple[DaemonApplication, Path]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state = tmp_path / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="owner",
        hyprial_home=home,
        **_deep_module_daemon_dependencies(),
    )
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]

    config = tmp_path / "personality"
    native = config / "native" / "codex" / "config.toml"
    native.parent.mkdir(parents=True)
    native.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
    for actor in ("granted", "empty"):
        app.handle(
            "agent.create",
            {
                "name": actor,
                "cwd": str(tmp_path / actor),
                "config": {
                    "sources": [{"path": str(config), "required": True}],
                    "discovery": "explicit-only",
                },
            },
        )

    app.handle(
        "agent.secret.provider-write",
        {"entryId": "deepseek", "fieldName": "apiKey", "value": CANARY},
    )
    source = home / "secrets" / "providers" / "deepseek.json"
    app.handle(
        "agent.secret.grant",
        {
            "actor": "granted",
            "grantId": "deepseek",
            "source": "user-provider",
            "entryId": "deepseek",
            "fieldName": "apiKey",
            "environmentNames": ["DEEPSEEK_API_KEY"],
            "revision": 1,
        },
    )
    AgentSecretStore(Path(app._agent_registry.home_receipt("granted").path)).set(
        service="deepseek",
        field="DEEPSEEK_API_KEY",
        value=CANARY,
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    return app, source


def _context(app: DaemonApplication, actor: str, cwd: Path) -> dict[str, object]:
    result = app.handle(
        "agent.runtime-context",
        {"name": actor, "harness": "codex", "cwd": str(cwd)},
    )
    assert result["mode"] == "agent-home-p2"
    return result


def _acquire(
    app: DaemonApplication, context: dict[str, object], operation: str
) -> dict[str, object]:
    return app.handle(
        "agent.runtime-launch.acquire",
        {"launchToken": context["launchToken"], "operationId": operation},
    )


def _release(app: DaemonApplication, acquired: dict[str, object]) -> None:
    app.handle(
        "agent.runtime-launch.release",
        {
            "leaseToken": acquired["leaseToken"],
            "operationId": acquired["operationId"],
        },
    )


def test_t1_acquire_returns_complete_granted_environment_and_empty_map(
    secret_app: tuple[DaemonApplication, Path], tmp_path: Path
) -> None:
    app, _source = secret_app
    assert CANARY not in os.environ.values()
    granted = _acquire(app, _context(app, "granted", tmp_path / "granted"), "granted")
    assert CANARY not in os.environ.values()
    empty = _acquire(app, _context(app, "empty", tmp_path / "empty"), "empty")
    assert CANARY not in os.environ.values()
    try:
        assert granted["secretEnvironment"] == {"DEEPSEEK_API_KEY": CANARY}
        assert granted["secretGrants"] == [["deepseek", 1]]
        assert empty["secretEnvironment"] == {}
        assert empty["secretGrants"] == []
    finally:
        _release(app, granted)
        _release(app, empty)
    assert CANARY not in os.environ.values()


def test_t1_removed_sqlite_value_is_absent_from_an_existing_launch_context(
    secret_app: tuple[DaemonApplication, Path], tmp_path: Path
) -> None:
    app, _source = secret_app
    store = AgentSecretStore(
        Path(app._agent_registry.home_receipt("granted").path)
    )
    stale = _context(app, "granted", tmp_path / "granted")
    store.remove(
        service="deepseek",
        field="DEEPSEEK_API_KEY",
        actor="user:owner",
        via="cli",
        at_ms=2,
    )

    removed = _acquire(app, stale, "removed")
    assert removed["secretEnvironment"] == {}
    _release(app, removed)

    store.set(
        service="deepseek",
        field="DEEPSEEK_API_KEY",
        value=CANARY,
        actor="user:owner",
        via="cli",
        at_ms=3,
    )
    fresh = _acquire(app, _context(app, "granted", tmp_path / "granted"), "fresh")
    assert fresh["secretEnvironment"] == {"DEEPSEEK_API_KEY": CANARY}
    _release(app, fresh)


def test_t2_t3_t4_secret_is_absent_from_projection_custody_state_and_logs(
    secret_app: tuple[DaemonApplication, Path], tmp_path: Path
) -> None:
    app, source = secret_app
    assert source == tmp_path / "home" / "secrets" / "providers" / "deepseek.json"
    assert CANARY in source.read_text(encoding="utf-8")
    context = _context(app, "granted", tmp_path / "granted")
    assert CANARY not in json.dumps(context, sort_keys=True)
    acquired = _acquire(app, context, "scan")
    try:
        custody = app.handle("agent.runtime-launch.custody", {})
        assert CANARY not in json.dumps(custody, sort_keys=True)
    finally:
        _release(app, acquired)

    writer = app._logger.writer_status()
    assert writer is not None
    assert app._logger.wait_for_writes(
        target=writer.accepted,
        rejected_at_capture=writer.rejected,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "state" / "logs" / "daemon.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    deliveries = [
        record
        for record in records
        if record.get("event") == "agent.runtime_launch.secrets_delivered"
    ]
    assert len(deliveries) == 1
    assert deliveries[0]["actor"] == "agent:owner:node:granted"
    assert deliveries[0]["names"] == ["DEEPSEEK_API_KEY"]
    assert deliveries[0]["grantCount"] == 1
    assert CANARY not in json.dumps(deliveries, sort_keys=True)

    sqlite_store = Path(
        app._agent_registry.home_receipt("granted").path
    ) / "secrets.sqlite"
    contaminated: list[Path] = []
    for path in tmp_path.rglob("*"):
        if not path.is_file() or path in {source, sqlite_store}:
            continue
        try:
            if CANARY.encode() in path.read_bytes():
                contaminated.append(path)
        except OSError:
            continue
    assert contaminated == []


def test_r2_cross_agent_acquire_is_empty_and_leaves_no_secret_trace(
    secret_app: tuple[DaemonApplication, Path], tmp_path: Path
) -> None:
    app, source = secret_app
    acquired = _acquire(app, _context(app, "empty", tmp_path / "empty"), "agent-b")
    try:
        assert acquired["actor"] == "agent:owner:node:empty"
        assert acquired["secretEnvironment"] == {}
        assert acquired["secretGrants"] == []
    finally:
        _release(app, acquired)

    sqlite_store = Path(
        app._agent_registry.home_receipt("granted").path
    ) / "secrets.sqlite"
    contaminated = []
    for path in tmp_path.rglob("*"):
        if not path.is_file() or path in {source, sqlite_store}:
            continue
        try:
            if CANARY.encode() in path.read_bytes():
                contaminated.append(path)
        except OSError:
            continue
    assert contaminated == []


def test_t1_any_resolution_failure_releases_lease_with_a_coded_refusal(
    secret_app: tuple[DaemonApplication, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Independent review on #1170: only SecretResolutionError released the
    # lease, so a sqlite error leaked it (no expiry, blocks destroy, uses one
    # of the 64 launch slots) and surfaced without an error code.
    import sqlite3

    from hyprial.daemon.impl.application.actors.agents_admin import (
        provider_credential,
    )

    app, _source = secret_app
    context = _context(app, "granted", tmp_path / "granted")

    class UnavailableStore:
        def __init__(self, _home: Path) -> None:
            pass

        def history(self) -> tuple[object, ...]:
            raise sqlite3.OperationalError(f"database is locked near {CANARY}")

    monkeypatch.setattr(provider_credential, "AgentSecretStore", UnavailableStore)
    with pytest.raises(DaemonRequestError) as refused:
        _acquire(app, context, "store-failure")
    assert refused.value.code == "AGENT_SECRET_RESOLUTION_FAILED"
    assert "OperationalError" in str(refused.value)
    assert CANARY not in str(refused.value)
    assert app.handle("agent.runtime-launch.custody", {})["custody"] == []

    monkeypatch.undo()
    fresh = _acquire(app, _context(app, "granted", tmp_path / "granted"), "fresh")
    assert fresh["secretEnvironment"] == {"DEEPSEEK_API_KEY": CANARY}
    _release(app, fresh)
