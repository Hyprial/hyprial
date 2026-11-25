"""WP2 acceptance checks for the per-agent append-only secret store."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from hyprial.identity import (
    AgentSecretStore,
    FingerprintUnavailable,
    secret_fingerprint,
)


def _store(tmp_path: Path) -> tuple[Path, AgentSecretStore]:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    return agent_home, AgentSecretStore(agent_home)


def _database_rows(path: Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def _files_containing(root: Path, value: str) -> list[Path]:
    needle = value.encode()
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and needle in path.read_bytes()
    ]


def test_acceptance_1_set_stores_value_but_never_puts_it_in_events(
    tmp_path: Path,
) -> None:
    agent_home, store = _store(tmp_path)
    value = "forgejo-token-value-000001"

    stored = store.set(
        service="forgejo",
        field="FORGEJO_TOKEN",
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=101,
    )

    assert store.path == agent_home / "secrets.sqlite"
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stored.fingerprint == hashlib.sha256(value.encode()).hexdigest()[:16]
    assert _database_rows(store.path, "secrets") == [
        ("forgejo", "FORGEJO_TOKEN", value, stored.fingerprint, 101)
    ]
    event_rows = _database_rows(store.path, "secret_events")
    key = (agent_home / ".secret-hmac-key").read_bytes()
    assert event_rows == [
        (
            1,
            101,
            "user:owner",
            "cli",
            "hmac_key_created",
            "agent-secret-key",
            None,
            hashlib.sha256(key).hexdigest()[:16],
            "ok",
        ),
        (
            2,
            101,
            "user:owner",
            "cli",
            "set",
            "forgejo",
            "FORGEJO_TOKEN",
            stored.fingerprint,
            "ok",
        ),
    ]
    assert value not in repr(event_rows)

    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        secret_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(secrets)")
        ]
        event_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(secret_events)")
        ]
    assert tables == {"secrets", "secret_events", "sqlite_sequence"}
    assert secret_columns == [
        "service",
        "field",
        "value",
        "fingerprint",
        "updated_at_ms",
    ]
    assert event_columns == [
        "seq",
        "at_ms",
        "actor",
        "via",
        "action",
        "service",
        "field",
        "fingerprint",
        "result",
    ]
    assert triggers == {"secret_events_no_update", "secret_events_no_delete"}


def test_a3_low_entropy_fingerprint_uses_private_hmac_key(tmp_path: Path) -> None:
    agent_home, store = _store(tmp_path)
    value = "9182"

    stored = store.set(
        service="door",
        field="DOOR_PIN",
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=102,
    )

    key_path = agent_home / ".secret-hmac-key"
    key = key_path.read_bytes()
    assert len(key) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stored.fingerprint == "hmac16:" + hmac.new(
        key, value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
    assert store.history(service="door")[0].fingerprint == stored.fingerprint
    assert value not in stored.fingerprint
    assert store.fingerprint("DOOR_PIN", value + "\n") == stored.fingerprint

    token = "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"
    assert secret_fingerprint("OPAQUE_CREDENTIAL", token + "\n") == (
        secret_fingerprint("OPAQUE_CREDENTIAL", token)
    )


@pytest.mark.parametrize("value", ("!", "!@#$", "!@#$%^", "!@#$%^&*"))
def test_a3_low_entropy_lengths_never_copy_value_fragments(
    tmp_path: Path, value: str
) -> None:
    _agent_home, store = _store(tmp_path)

    stored = store.set(
        service="short-secret",
        field="SHORT_SECRET",
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=len(value),
    )

    row_fingerprint = _database_rows(store.path, "secrets")[0][3]
    event_row = _database_rows(store.path, "secret_events")[0]
    rendered = repr((stored.fingerprint, row_fingerprint, event_row))
    fragments = {
        value[start:end]
        for start in range(len(value))
        for end in range(start + 1, len(value) + 1)
    }
    assert all(fragment not in rendered for fragment in fragments)


def test_a3_missing_or_invalid_key_refuses_value_write_without_a_row(
    tmp_path: Path,
) -> None:
    agent_home, store = _store(tmp_path)
    store.set(
        service="door",
        field="DOOR_PIN",
        value="9182",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    key_path = agent_home / ".secret-hmac-key"
    key_path.unlink()

    with pytest.raises(FingerprintUnavailable, match="fingerprint_unavailable"):
        store.set(
            service="door",
            field="BACKUP_PIN",
            value="27",
            actor="user:owner",
            via="cli",
            at_ms=2,
        )

    assert [(row.service, row.field) for row in store.list()] == [("door", "DOOR_PIN")]
    assert [event.action for event in store.history()] == [
        "hmac_key_created",
        "set",
    ]

    key_path.write_bytes(b"x" * 32)
    key_path.chmod(0o644)
    with pytest.raises(FingerprintUnavailable, match="fingerprint_unavailable"):
        store.rotate(
            service="door",
            field="DOOR_PIN",
            value="7319",
            actor="user:owner",
            via="cli",
            at_ms=3,
        )
    assert store.get("door", "DOOR_PIN").value == "9182"


def test_a4_legacy_store_bootstraps_one_key_event_then_requires_key(
    tmp_path: Path,
) -> None:
    agent_home, store = _store(tmp_path)
    legacy_token = "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"
    legacy_sha = hashlib.sha256(legacy_token.encode()).hexdigest()[:16]
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.executemany(
            """
            INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("api", "API_TOKEN", legacy_token, legacy_sha, 1),
                ("door", "DOOR_PIN", "9182", "len=4,tail=9182", 2),
            ],
        )
        connection.executemany(
            """
            INSERT INTO secret_events(
              at_ms, actor, via, action, service, field, fingerprint, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "user:owner", "cli", "set", "api", "API_TOKEN", legacy_sha, "ok"),
                (
                    2,
                    "user:owner",
                    "cli",
                    "set",
                    "door",
                    "DOOR_PIN",
                    "len=4,tail=9182",
                    "ok",
                ),
            ],
        )

    assert not store.key_path.exists()
    rotated = store.rotate(
        service="door",
        field="DOOR_PIN",
        value="7319",
        actor="user:owner",
        via="cli",
        at_ms=3,
    )
    assert rotated.fingerprint.startswith("hmac16:")
    key = store.key_path.read_bytes()
    assert len(key) == 32
    key_events = [
        event for event in store.history() if event.action == "hmac_key_created"
    ]
    assert len(key_events) == 1
    assert key_events[0].fingerprint == hashlib.sha256(key).hexdigest()[:16]
    assert key_events[0].field is None

    store.key_path.unlink()
    with pytest.raises(FingerprintUnavailable, match="fingerprint_unavailable"):
        store.set(
            service="api",
            field="SECOND_TOKEN",
            value="Z9y8X7w6V5u4T3s2R1q0P9n8M7k6J5h4",
            actor="user:owner",
            via="cli",
            at_ms=4,
        )
    assert not store.key_path.exists()
    assert store.get("api", "SECOND_TOKEN") is None

    replacement = b"x" * 32
    store.key_path.write_bytes(replacement)
    store.key_path.chmod(0o600)
    with pytest.raises(
        FingerprintUnavailable, match=r"fingerprint_unavailable\{key_mismatch\}"
    ):
        store.rotate(
            service="door",
            field="DOOR_PIN",
            value="2468",
            actor="user:owner",
            via="cli",
            at_ms=5,
        )
    assert store.key_path.read_bytes() == replacement
    assert store.get("door", "DOOR_PIN").value == "7319"
    assert len(
        [event for event in store.history() if event.action == "hmac_key_created"]
    ) == 1


def test_a4_hmac_row_without_lifecycle_event_never_regenerates_key(
    tmp_path: Path,
) -> None:
    _agent_home, store = _store(tmp_path)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("door", "DOOR_PIN", "9182", "hmac16:0123456789abcdef", 1),
        )
        connection.execute(
            """
            INSERT INTO secret_events(
              at_ms, actor, via, action, service, field, fingerprint, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "user:owner",
                "cli",
                "set",
                "door",
                "DOOR_PIN",
                "hmac16:0123456789abcdef",
                "ok",
            ),
        )

    with pytest.raises(FingerprintUnavailable, match="fingerprint_unavailable"):
        store.set(
            service="door",
            field="BACKUP_PIN",
            value="7319",
            actor="user:owner",
            via="cli",
            at_ms=2,
        )
    assert not store.key_path.exists()
    assert not any(
        event.action == "hmac_key_created" for event in store.history()
    )
    assert store.get("door", "BACKUP_PIN") is None


def test_a4_concurrent_first_writes_create_one_key_and_one_event(
    tmp_path: Path,
) -> None:
    agent_home, store = _store(tmp_path)
    barrier = threading.Barrier(2)

    def write(index: int) -> None:
        barrier.wait()
        AgentSecretStore(agent_home).set(
            service="parallel",
            field=f"TOKEN_{index}",
            value=f"A{index}b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6",
            actor="user:owner",
            via="cli",
            at_ms=index,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, index) for index in (1, 2)]
        for future in futures:
            future.result()

    key = store.key_path.read_bytes()
    key_events = [
        event for event in store.history() if event.action == "hmac_key_created"
    ]
    assert len(key) == 32
    assert len(key_events) == 1
    assert key_events[0].fingerprint == hashlib.sha256(key).hexdigest()[:16]
    assert len(store.list(service="parallel")) == 2


def test_acceptance_2_update_event_is_aborted_by_append_only_trigger(
    tmp_path: Path,
) -> None:
    _agent_home, store = _store(tmp_path)
    store.set(
        service="forgejo",
        field="FORGEJO_TOKEN",
        value="forgejo-token-value-000002",
        actor="user:owner",
        via="cli",
        at_ms=102,
    )

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="secret_events is append-only"):
            connection.execute("UPDATE secret_events SET result = 'forged'")


def test_acceptance_2_delete_event_is_aborted_by_append_only_trigger(
    tmp_path: Path,
) -> None:
    _agent_home, store = _store(tmp_path)
    store.set(
        service="forgejo",
        field="FORGEJO_TOKEN",
        value="forgejo-token-value-000003",
        actor="user:owner",
        via="cli",
        at_ms=103,
    )

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="secret_events is append-only"):
            connection.execute("DELETE FROM secret_events")


def test_acceptance_3_rotate_changes_current_fingerprint_and_keeps_history(
    tmp_path: Path,
) -> None:
    agent_home, store = _store(tmp_path)
    old_value = "forgejo-token-value-old-0004"
    new_value = "forgejo-token-value-new-0004"
    old_fingerprint = secret_fingerprint("FORGEJO_TOKEN", old_value)
    new_fingerprint = secret_fingerprint("FORGEJO_TOKEN", new_value)

    store.set(
        service="forgejo",
        field="FORGEJO_TOKEN",
        value=old_value,
        actor="user:owner",
        via="cli",
        at_ms=104,
    )
    rotated = store.rotate(
        service="forgejo",
        field="FORGEJO_TOKEN",
        value=new_value,
        actor="user:owner",
        via="cli",
        at_ms=105,
    )

    assert old_fingerprint != new_fingerprint
    assert rotated.fingerprint == new_fingerprint
    assert store.get("forgejo", "FORGEJO_TOKEN") == rotated
    assert [event.action for event in store.history(service="forgejo")] == [
        "set",
        "rotate",
    ]
    assert [event.fingerprint for event in store.history(service="forgejo")] == [
        old_fingerprint,
        new_fingerprint,
    ]
    assert old_fingerprint not in {
        secret.fingerprint for secret in store.list(service="forgejo")
    }
    assert _files_containing(agent_home, old_value) == []

    password = "horse-battery-pin"
    bare_password_hash = hashlib.sha256(password.encode()).hexdigest()[:16]
    password_row = store.set(
        service="smtp",
        field="SMTP_PASSWORD",
        value=password,
        actor="user:owner",
        via="cli",
        at_ms=106,
    )
    password_fingerprint = store.fingerprint("SMTP_PASSWORD", password)
    assert password_fingerprint.startswith("hmac16:")
    assert password_row.fingerprint == password_fingerprint
    assert store.history(service="smtp")[0].fingerprint == password_fingerprint
    assert bare_password_hash not in store.path.read_bytes().decode(
        "utf-8", errors="ignore"
    )


@pytest.mark.parametrize(
    "field",
    (
        "DATABASE_PASSPHRASE",
        "DEVICE_PASSCODE",
        "MYSQL_ROOT_PWD",
        "DB_SECRET_PHRASE",
    ),
)
def test_password_like_field_aliases_are_never_bare_hashed(
    tmp_path: Path,
    field: str,
) -> None:
    _agent_home, store = _store(tmp_path)
    value = "correct horse battery staple"
    bare_hash = hashlib.sha256(value.encode()).hexdigest()[:16]

    stored = store.set(
        service="password-service",
        field=field,
        value=value,
        actor="user:owner",
        via="cli",
        at_ms=110,
    )

    assert stored.fingerprint.startswith("hmac16:")
    assert store.get("password-service", field).fingerprint == stored.fingerprint
    assert store.history(service="password-service")[0].fingerprint == stored.fingerprint
    assert value[-4:] not in stored.fingerprint
    assert bare_hash not in store.path.read_bytes().decode("utf-8", errors="ignore")


def test_token_likeness_is_value_based_and_fail_safe(tmp_path: Path) -> None:
    _agent_home, store = _store(tmp_path)
    random_token = "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"
    low_diversity = "a" * 20

    token_row = store.set(
        service="api",
        field="OPAQUE_CREDENTIAL",
        value=random_token,
        actor="user:owner",
        via="cli",
        at_ms=111,
    )
    low_diversity_row = store.set(
        service="api",
        field="API_KEY",
        value=low_diversity,
        actor="user:owner",
        via="cli",
        at_ms=112,
    )

    token_hash = hashlib.sha256(random_token.encode()).hexdigest()[:16]
    low_diversity_fingerprint = store.fingerprint("API_KEY", low_diversity)
    assert token_row.fingerprint == token_hash
    assert low_diversity_row.fingerprint == low_diversity_fingerprint
    assert low_diversity_fingerprint.startswith("hmac16:")
    assert [event.fingerprint for event in store.history(service="api")] == [
        token_hash,
        low_diversity_fingerprint,
    ]
    # A name hint may force the keyed form, never force a bare hash.
    assert store.fingerprint("DATABASE_PASSWORD", random_token).startswith("hmac16:")
    # Both forms remain deterministic for same-value/history comparison.
    assert secret_fingerprint("OPAQUE_CREDENTIAL", random_token) == token_hash
    assert store.fingerprint("API_KEY", low_diversity) == low_diversity_fingerprint


@pytest.mark.parametrize(
    ("field", "old_value", "new_value"),
    (
        (
            "OPAQUE_CREDENTIAL",
            "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6",
            "Z9y8X7w6V5u4T3s2R1q0P9n8M7k6J5h4",
        ),
        (
            "DB_SECRET_PHRASE",
            "correct horse battery staple",
            "different memorable phrase value",
        ),
    ),
)
def test_fingerprint_forms_detect_an_old_value_reappearing(
    tmp_path: Path,
    field: str,
    old_value: str,
    new_value: str,
) -> None:
    _agent_home, store = _store(tmp_path)
    first = store.set(
        service="comparison",
        field=field,
        value=old_value,
        actor="user:owner",
        via="cli",
        at_ms=113,
    )
    old_fingerprint = first.fingerprint
    new_fingerprint = store.fingerprint(field, new_value)
    assert old_fingerprint != new_fingerprint

    store.rotate(
        service="comparison",
        field=field,
        value=new_value,
        actor="user:owner",
        via="cli",
        at_ms=114,
    )
    current = store.rotate(
        service="comparison",
        field=field,
        value=old_value,
        actor="user:owner",
        via="cli",
        at_ms=115,
    )

    assert current.fingerprint == old_fingerprint
    assert [event.fingerprint for event in store.history(service="comparison")] == [
        old_fingerprint,
        new_fingerprint,
        old_fingerprint,
    ]


def test_public_existence_probe_is_non_creating_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    from hyprial import identity as identity_public

    hyprial_home = tmp_path / "hyprial"
    agent_home = hyprial_home / "agents" / "worker"
    agent_home.mkdir(parents=True, mode=0o700)
    before = tuple(agent_home.iterdir())
    probe = getattr(identity_public, "agent_secret_store_exists")

    assert probe(hyprial_home, "worker") is False
    assert tuple(agent_home.iterdir()) == before

    store = AgentSecretStore(agent_home)
    assert probe(hyprial_home, "worker") is True

    store.path.unlink()
    target = tmp_path / "not-the-secret-database"
    target.write_bytes(b"not sqlite")
    store.path.symlink_to(target)
    assert probe(hyprial_home, "worker") is False


def test_acceptance_9_rotation_and_completed_revoke_leave_no_value_copy(
    tmp_path: Path,
) -> None:
    agent_home, store = _store(tmp_path)
    rotated_away = "cloudflare-token-rotated-away-0005"
    revoked = "cloudflare-token-revoked-0000005"

    store.set(
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        value=rotated_away,
        actor="user:owner",
        via="cli",
        at_ms=106,
    )
    store.rotate(
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        value=revoked,
        actor="user:owner",
        via="cli",
        at_ms=107,
    )
    store.record_event(
        action="revoke_remote",
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=108,
    )

    # Remote invalidation is only an event. It must not mutate or hide the row.
    assert store.get("cloudflare-r2", "CLOUDFLARE_API_TOKEN").value == revoked

    removed = store.remove(
        service="cloudflare-r2",
        field="CLOUDFLARE_API_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=109,
    )

    assert [secret.value for secret in removed] == [revoked]
    assert store.get("cloudflare-r2", "CLOUDFLARE_API_TOKEN") is None
    assert [event.action for event in store.history(service="cloudflare-r2")] == [
        "set",
        "rotate",
        "revoke_remote",
        "remove",
    ]
    assert _files_containing(agent_home, rotated_away) == []
    assert _files_containing(agent_home, revoked) == []
    assert not any(
        path.name.endswith((".bak", ".retired", ".old"))
        for path in agent_home.rglob("*")
    )
