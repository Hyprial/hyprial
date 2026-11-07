"""Acceptance coverage for per-agent sqlite launch materialization."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import AgentSecretStore, secret_fingerprint
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)


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
def sqlite_secret_app(tmp_path: Path) -> tuple[DaemonApplication, AgentSecretStore]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
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
    app.handle(
        "agent.create",
        {
            "name": "alpha",
            "cwd": str(tmp_path / "alpha"),
            "config": {
                "sources": [{"path": str(config), "required": True}],
                "discovery": "explicit-only",
            },
        },
    )
    agent_home = Path(app._agent_registry.home_receipt("alpha").path)
    return app, AgentSecretStore(agent_home)


def _acquire(app: DaemonApplication, tmp_path: Path, operation: str) -> dict[str, object]:
    context = app.handle(
        "agent.runtime-context",
        {"name": "alpha", "harness": "codex", "cwd": str(tmp_path / "alpha")},
    )
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


def test_acceptance_4_remove_stops_injection_and_keeps_events(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    value = "forgejo-token-4aC9xP7mQ2"
    store.set(
        service="forgejo",
        field="FORGEJO_TOKEN",
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    store.set(
        service="smtp",
        field="SMTP_USER",
        value="mailer@example.test",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )

    before = _acquire(app, tmp_path, "before-remove")
    try:
        assert before["secretEnvironment"] == {
            "FORGEJO_TOKEN": value,
            "SMTP_USER": "mailer@example.test",
        }
    finally:
        _release(app, before)

    removed = store.remove(
        service="forgejo",
        field="FORGEJO_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=2,
    )
    after = _acquire(app, tmp_path, "after-remove")
    try:
        assert removed and store.get("forgejo", "FORGEJO_TOKEN") is None
        assert after["secretEnvironment"] == {"SMTP_USER": "mailer@example.test"}
    finally:
        _release(app, after)

    events = store.history()
    assert events[0].action == "hmac_key_created"
    value_events = events[1:]
    assert [event.action for event in value_events] == [
        "set",
        "set",
        "materialize",
        "remove",
        "materialize",
    ]
    assert value_events[2].field == "FORGEJO_TOKEN,SMTP_USER"
    assert value_events[4].field == "SMTP_USER"
    assert value not in repr(events)


def test_a4_matching_legacy_schemes_inject_and_tampered_tail_is_rejected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    sha_value = "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"
    legacy_sha = secret_fingerprint("LEGACY_TOKEN", sha_value)
    rows = [
        ("legacy", "LEGACY_TOKEN", sha_value, legacy_sha, 1),
        ("legacy", "LEGACY_PIN", "1234", "len=4,tail=1234", 2),
        ("legacy", "TAMPERED_PIN", "9999", "len=4,tail=5678", 3),
    ]
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.executemany(
            """
            INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.executemany(
            """
            INSERT INTO secret_events(
              at_ms, actor, via, action, service, field, fingerprint, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (at_ms, "user:owner", "cli", "set", service, field, fingerprint, "ok")
                for service, field, _value, fingerprint, at_ms in rows
            ],
        )

    assert not store.key_path.exists()
    acquired = _acquire(app, tmp_path, "legacy-schemes")
    try:
        assert acquired["secretEnvironment"] == {
            "LEGACY_PIN": "1234",
            "LEGACY_TOKEN": sha_value,
        }
    finally:
        _release(app, acquired)
    rejected = [
        event for event in store.history(service="legacy") if event.action == "rejected"
    ]
    assert [(event.field, event.result) for event in rejected] == [
        ("TAMPERED_PIN", "rejected:no-write-event")
    ]
    database_text = store.path.read_bytes().decode("utf-8", errors="ignore")
    assert "len=4,tail=9999" not in database_text


def test_a4_hmac_and_legacy_receipts_are_not_cross_compared(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    stored = store.set(
        service="mixed",
        field="MIXED_PIN",
        value="1234",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    assert stored.fingerprint.startswith("hmac16:")
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            UPDATE secrets SET fingerprint = ?
            WHERE service = ? AND field = ?
            """,
            ("len=4,tail=1234", "mixed", "MIXED_PIN"),
        )

    acquired = _acquire(app, tmp_path, "mixed-schemes")
    try:
        assert "MIXED_PIN" not in acquired["secretEnvironment"]
    finally:
        _release(app, acquired)
    assert any(
        event.action == "rejected" and event.result == "rejected:no-write-event"
        for event in store.history(service="mixed")
    )


def test_acceptance_5_sql_row_without_write_event_is_rejected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("smtp", "SMTP_PASSWORD", "sql-only-value", "len=14,tail=alue", 1),
        )

    acquired = _acquire(app, tmp_path, "no-write-event")
    try:
        assert "SMTP_PASSWORD" not in acquired["secretEnvironment"]
    finally:
        _release(app, acquired)

    rejected = [event for event in store.history(service="smtp") if event.action == "rejected"]
    assert [(event.field, event.result) for event in rejected] == [
        ("SMTP_PASSWORD", "rejected:no-write-event")
    ]


def test_acceptance_5_sql_update_without_new_write_event_is_rejected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    store.set(
        service="artifact",
        field="ARTIFACT_TOKEN",
        value="original_K4p8T2c7W9",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    replacement = "replacement_N6v3Z8w2L5"
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            UPDATE secrets
            SET value = ?, fingerprint = ?, updated_at_ms = ?
            WHERE service = ? AND field = ?
            """,
            (
                replacement,
                secret_fingerprint("ARTIFACT_TOKEN", replacement),
                2,
                "artifact",
                "ARTIFACT_TOKEN",
            ),
        )

    acquired = _acquire(app, tmp_path, "unrecorded-update")
    try:
        assert "ARTIFACT_TOKEN" not in acquired["secretEnvironment"]
    finally:
        _release(app, acquired)

    rejected = [
        event for event in store.history(service="artifact") if event.action == "rejected"
    ]
    assert [(event.field, event.result) for event in rejected] == [
        ("ARTIFACT_TOKEN", "rejected:no-write-event")
    ]


def test_acceptance_5_sql_value_update_without_event_is_rejected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    store.set(
        service="artifact-value",
        field="ARTIFACT_VALUE_TOKEN",
        value="original_R5j8P2c7W4",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            UPDATE secrets SET value = ?, updated_at_ms = ?
            WHERE service = ? AND field = ?
            """,
            ("replacement_M6v3Z8w2L5", 2, "artifact-value", "ARTIFACT_VALUE_TOKEN"),
        )

    acquired = _acquire(app, tmp_path, "unrecorded-value-update")
    try:
        assert "ARTIFACT_VALUE_TOKEN" not in acquired["secretEnvironment"]
    finally:
        _release(app, acquired)
    rejected = [
        event
        for event in store.history(service="artifact-value")
        if event.action == "rejected"
    ]
    assert [(event.field, event.result) for event in rejected] == [
        ("ARTIFACT_VALUE_TOKEN", "rejected:no-write-event")
    ]


def test_acceptance_5_sql_fingerprint_update_without_event_is_rejected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    store.set(
        service="artifact-fingerprint",
        field="ARTIFACT_FINGERPRINT_TOKEN",
        value="original_H7m4Q9x2K6",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            UPDATE secrets SET fingerprint = ?, updated_at_ms = ?
            WHERE service = ? AND field = ?
            """,
            ("0000000000000000", 2, "artifact-fingerprint", "ARTIFACT_FINGERPRINT_TOKEN"),
        )

    acquired = _acquire(app, tmp_path, "unrecorded-fingerprint-update")
    try:
        assert "ARTIFACT_FINGERPRINT_TOKEN" not in acquired["secretEnvironment"]
    finally:
        _release(app, acquired)
    rejected = [
        event
        for event in store.history(service="artifact-fingerprint")
        if event.action == "rejected"
    ]
    assert [(event.field, event.result) for event in rejected] == [
        ("ARTIFACT_FINGERPRINT_TOKEN", "rejected:no-write-event")
    ]


def test_acceptance_5_low_entropy_sql_update_without_event_is_rejected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    original = "alpha-prefix-1234"
    replacement = "omega-prefix-1234"
    stored = store.set(
        service="smtp",
        field="SMTP_PASSWORD",
        value=original,
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    assert stored.fingerprint != store.fingerprint("SMTP_PASSWORD", replacement)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            UPDATE secrets SET value = ?, updated_at_ms = ?
            WHERE service = ? AND field = ?
            """,
            (replacement, 2, "smtp", "SMTP_PASSWORD"),
        )

    acquired = _acquire(app, tmp_path, "unrecorded-low-entropy-update")
    try:
        assert "SMTP_PASSWORD" not in acquired["secretEnvironment"]
    finally:
        _release(app, acquired)
    assert any(
        event.action == "rejected" and event.result == "rejected:no-write-event"
        for event in store.history(service="smtp")
    )


def test_launch_environment_conflict_records_failed_materialize_attempt(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    for service, value in (
        ("first", "first-token-A7q2M9x4K8"),
        ("second", "second-token-B8r3N1w5L7"),
    ):
        store.set(
            service=service,
            field="SHARED_TOKEN",
            value=value,
            actor="user:owner",
            via="cli",
            at_ms=1,
        )

    with pytest.raises(DaemonRequestError) as refused:
        _acquire(app, tmp_path, "environment-conflict")
    assert refused.value.code == "AGENT_SECRET_RESOLUTION_FAILED"
    assert app.handle("agent.runtime-launch.custody", {})["custody"] == []

    events = store.history()
    assert [event.action for event in events] == [
        "hmac_key_created",
        "set",
        "set",
        "rejected",
        "materialize",
    ]
    assert events[-2].field == "SHARED_TOKEN"
    assert events[-2].result == "rejected:environment-conflict"
    assert events[-1].field is None
    assert events[-1].result == "rejected:environment-conflict"


def test_acceptance_6_reserved_sql_row_is_never_injected(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.executemany(
            """
            INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("runtime", "PATH", "/hostile/bin", "len=12,tail=/bin", 1),
                ("runtime", "HOME", "/hostile/home", "mismatched-fingerprint", 1),
            ],
        )
        connection.executemany(
            """
            INSERT INTO secret_events(
              at_ms, actor, via, action, service, field, fingerprint, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "user:owner",
                    "cli",
                    "set",
                    "runtime",
                    "PATH",
                    "len=12,tail=/bin",
                    "ok",
                ),
                (
                    1,
                    "user:owner",
                    "cli",
                    "set",
                    "runtime",
                    "HOME",
                    "mismatched-fingerprint",
                    "ok",
                ),
            ],
        )

    acquired = _acquire(app, tmp_path, "reserved")
    try:
        assert {"PATH", "HOME"}.isdisjoint(acquired["secretEnvironment"])
    finally:
        _release(app, acquired)

    rejected = [event for event in store.history(service="runtime") if event.action == "rejected"]
    assert [(event.field, event.result) for event in rejected] == [
        ("HOME", "rejected:reserved-name"),
        ("PATH", "rejected:reserved-name"),
    ]


def test_acceptance_8_remote_revoke_remains_materializable_until_remove(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    value = "cloudflare-token-W8q3Lm6Z"
    store.set(
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    store.record_event(
        action="revoke_remote",
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=2,
    )

    incomplete = _acquire(app, tmp_path, "remote-only")
    try:
        assert incomplete["secretEnvironment"]["CLOUDFLARE_API_TOKEN"] == value
    finally:
        _release(app, incomplete)

    store.remove(
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=3,
    )
    complete = _acquire(app, tmp_path, "remote-and-remove")
    try:
        assert "CLOUDFLARE_API_TOKEN" not in complete["secretEnvironment"]
    finally:
        _release(app, complete)
    assert store.get("cloudflare-r2", "CLOUDFLARE_API_TOKEN") is None
    assert {event.action for event in store.history(service="cloudflare-r2")} >= {
        "revoke_remote",
        "remove",
    }


def test_acceptance_8b_event_and_table_reads_agree_remote_only_is_present(
    sqlite_secret_app: tuple[DaemonApplication, AgentSecretStore], tmp_path: Path
) -> None:
    app, store = sqlite_secret_app
    value = "session-token-N7k2Vx9Qa"
    store.set(
        service="session",
        field="SESSION_TOKEN",
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    store.record_event(
        action="revoke_remote",
        service="session",
        field="SESSION_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=2,
    )
    assert store.get("session", "SESSION_TOKEN") is not None

    acquired = _acquire(app, tmp_path, "event-table-agree")
    try:
        assert acquired["secretEnvironment"] == {"SESSION_TOKEN": value}
    finally:
        _release(app, acquired)
    actions = [event.action for event in store.history()]
    assert actions == [
        "hmac_key_created",
        "set",
        "revoke_remote",
        "materialize",
    ]
