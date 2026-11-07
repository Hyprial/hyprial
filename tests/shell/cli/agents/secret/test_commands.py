"""WP5 acceptance checks for ``hyprial agent secret``."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.identity import AgentSecretStore
from hyprial.shell.impl.cli.commands.agent import secret as secret_commands
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def secret_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "agents").mkdir(mode=0o700)
    for actor in ("worker", "human-warning", "validated"):
        (home / "agents" / actor).mkdir(mode=0o700)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_get_services(), "_hyprial_home", lambda: home)
    monkeypatch.setattr(
        secret_commands,
        "_local_operator_identity",
        lambda: "user:owner",
        raising=False,
    )
    return home


def _invoke(*arguments: str, input: str | None = None):
    return CliRunner().invoke(
        _cli_root_app,
        ["agent", "secret", *arguments],
        input=input,
    )


def _store(home: Path, actor: str = "worker") -> AgentSecretStore:
    return AgentSecretStore(home / "agents" / actor)


def test_list_and_history_do_not_initialize_a_pristine_store(
    secret_home: Path,
) -> None:
    agent_home = secret_home / "agents" / "worker"
    before = tuple(agent_home.rglob("*"))

    listed = _invoke("list", "--agent", "worker", "--json")
    history = _invoke("history", "--agent", "worker", "--json")

    assert listed.exit_code == history.exit_code == 0
    assert json.loads(listed.stdout)["secrets"] == []
    assert json.loads(history.stdout)["events"] == []
    assert tuple(agent_home.rglob("*")) == before

    written = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        "--json",
        input="custom-token-A1b2C3d4E5f6\n",
    )
    assert written.exit_code == 0
    assert (agent_home / "secrets.sqlite").is_file()


def test_a3_low_entropy_cli_output_never_contains_value_fragments(
    secret_home: Path,
) -> None:
    values = ("!", "!@#$", "!@#$%^", "!@#$%^&*")
    for index, value in enumerate(values, start=1):
        written = _invoke(
            "set",
            "--agent",
            "worker",
            "--service",
            f"short-{index}",
            "--field",
            f"SHORT_SECRET_{index}",
            "--no-validate",
            "--json",
            input=value + "\n",
        )
        assert written.exit_code == 0, written.output

    listed = _invoke("list", "--agent", "worker", "--json")
    history = _invoke("history", "--agent", "worker", "--json")
    assert listed.exit_code == history.exit_code == 0
    rendered = listed.stdout + history.stdout
    fragments = {
        value[start:end]
        for value in values
        for start in range(len(value))
        for end in range(start + 1, len(value) + 1)
    }
    assert all(fragment not in rendered for fragment in fragments)


def test_a3_missing_key_refuses_write_and_redacts_read_fingerprints(
    secret_home: Path,
) -> None:
    written = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "door",
        "--field",
        "DOOR_PIN",
        "--no-validate",
        "--json",
        input="9182\n",
    )
    assert written.exit_code == 0
    store = _store(secret_home)
    (store.agent_home / ".secret-hmac-key").unlink()

    refused = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "door",
        "--field",
        "BACKUP_PIN",
        "--no-validate",
        "--json",
        input="7319\n",
    )
    assert refused.exit_code == 10
    assert json.loads(refused.stdout)["code"] == "FINGERPRINT_UNAVAILABLE"
    assert [(row.service, row.field) for row in store.list()] == [("door", "DOOR_PIN")]
    assert [event.action for event in store.history()] == [
        "hmac_key_created",
        "set",
    ]

    listed = _invoke("list", "--agent", "worker", "--json")
    history = _invoke("history", "--agent", "worker", "--json")
    assert json.loads(listed.stdout)["secrets"][0]["fingerprint"] == "unavailable"
    assert all(
        event["fingerprint"] == "unavailable"
        for event in json.loads(history.stdout)["events"]
    )
    assert "9182" not in listed.stdout + history.stdout

    store.key_path.write_bytes(b"x" * 32)
    store.key_path.chmod(0o600)
    mismatch = _invoke(
        "rotate",
        "--agent",
        "worker",
        "--service",
        "door",
        "--field",
        "DOOR_PIN",
        "--no-validate",
        "--json",
        input="2468\n",
    )
    assert mismatch.exit_code == 10
    mismatch_error = json.loads(mismatch.stdout)
    assert mismatch_error["code"] == "FINGERPRINT_UNAVAILABLE"
    assert "key_mismatch" in mismatch.stdout
    assert store.get("door", "DOOR_PIN").value == "9182"


def test_a3_history_flags_and_redacts_legacy_plaintext_tail(
    secret_home: Path,
) -> None:
    store = _store(secret_home)
    store.set(
        service="api",
        field="API_TOKEN",
        value="A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO secret_events(
              at_ms, actor, via, action, service, field, fingerprint, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (2, "user:owner", "cli", "set", "door", "DOOR_PIN", "len=4,tail=9182", "ok"),
        )

    history = _invoke("history", "--agent", "worker", "--json")
    assert history.exit_code == 0
    events = json.loads(history.stdout)["events"]
    set_events = [event for event in events if event["action"] == "set"]
    assert set_events[0]["fingerprintKind"] == "legacy_sha"
    assert set_events[1]["fingerprint"] == "unavailable"
    assert set_events[1]["fingerprintKind"] == "legacy_plaintext_tail"
    assert "9182" not in history.stdout


def test_acceptance_7_unknown_service_refuses_without_override_and_counts_override(
    secret_home: Path,
) -> None:
    value = "custom-token-A1b2C3d4E5f6"

    refused = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--json",
        input=value + "\n",
    )

    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["code"] == "SECRET_VALIDATOR_REQUIRED"
    assert value not in refused.stdout
    assert value not in refused.stderr
    store = _store(secret_home)
    metrics_path = store.agent_home / "secret-metrics.jsonl"
    assert store.list() == ()
    assert not metrics_path.exists()
    assert [(event.action, event.result) for event in store.history()] == [
        ("rejected", "rejected:no-validator")
    ]

    accepted = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        "--json",
        input=value + "\n",
    )

    assert accepted.exit_code == 0
    payload = json.loads(accepted.stdout)
    warning = "warning: no validator for service 'custom-api'; fields written unvalidated"
    assert payload["credentialUnvalidatedTotal"] == 1
    assert payload["warnings"] == [warning]
    assert stat.S_IMODE(metrics_path.stat().st_mode) == 0o600
    assert metrics_path.read_text(encoding="utf-8").splitlines() == [
        '{"metric":"credential_unvalidated_total","delta":1}'
    ]
    assert accepted.stderr == ""
    assert value not in accepted.stdout
    assert [(event.action, event.result) for event in store.history()] == [
        ("rejected", "rejected:no-validator"),
        ("hmac_key_created", "ok"),
        ("set", "ok"),
    ]

    human = _invoke(
        "set",
        "--agent",
        "human-warning",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        input="other-custom-token-Z9y8X7w6\n",
    )
    assert human.exit_code == 0
    assert human.stderr == warning + "\n"


def test_acceptance_7_validator_is_declarative_shape_only(secret_home: Path) -> None:
    missing = _invoke(
        "set",
        "--agent",
        "validated",
        "--service",
        "cloudflare-r2",
        "--field",
        "CLOUDFLARE_API_TOKEN",
        "--json",
        input="cloudflare-token-A1b2C3d4E5f6\n",
    )
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["code"] == "SECRET_VALIDATION_FAILED"
    assert _store(secret_home, "validated").list() == ()

    accepted = _invoke(
        "set",
        "--agent",
        "validated",
        "--service",
        "cloudflare-r2",
        "--field",
        "CLOUDFLARE_ACCOUNT_ID",
        "--field",
        "CLOUDFLARE_API_TOKEN",
        "--json",
        input="0123456789abcdef0123456789abcdef\ncloudflare-token-A1b2C3d4E5f6\n",
    )
    assert accepted.exit_code == 0
    payload = json.loads(accepted.stdout)
    assert payload["validated"] is True
    assert payload["fields"] == [
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
    ]
    rendered = accepted.stdout.lower()
    assert "usable" not in rendered
    assert "capable" not in rendered

    rotated = _invoke(
        "rotate",
        "--agent",
        "validated",
        "--service",
        "cloudflare-r2",
        "--field",
        "CLOUDFLARE_API_TOKEN",
        "--json",
        input="cloudflare-token-Z9y8X7w6V5u4\n",
    )
    assert rotated.exit_code == 0
    assert json.loads(rotated.stdout)["validated"] is True

    forgejo_bad = _invoke(
        "set",
        "--agent",
        "validated",
        "--service",
        "forgejo",
        "--field",
        "FORGEJO_BASE_URL",
        "--field",
        "FORGEJO_TOKEN",
        "--json",
        input="not-a-url\nforgejo-token-A1b2C3d4E5f6\n",
    )
    assert forgejo_bad.exit_code == 1
    assert json.loads(forgejo_bad.stdout)["code"] == "SECRET_VALIDATION_FAILED"

    forgejo_ok = _invoke(
        "set",
        "--agent",
        "validated",
        "--service",
        "forgejo",
        "--field",
        "FORGEJO_BASE_URL",
        "--field",
        "FORGEJO_TOKEN",
        "--json",
        input="https://git.internal:443\nforgejo-token-A1b2C3d4E5f6\n",
    )
    assert forgejo_ok.exit_code == 0
    assert json.loads(forgejo_ok.stdout)["validated"] is True


def test_values_are_stdin_only_tty_is_refused_and_reads_do_not_reveal_values(
    secret_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "secret-never-in-cli-output-A1b2C3d4"
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: True)
    tty = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        "--json",
        input=value + "\n",
    )
    assert tty.exit_code == 1
    assert json.loads(tty.stdout)["code"] == "SECRET_REQUIRED"
    assert not (secret_home / "agents" / "worker" / "secrets.sqlite").exists()

    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: False)
    reserved = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--field",
        "HOME",
        "--no-validate",
        "--json",
        input=value + "\n/tmp/forbidden\n",
    )
    assert reserved.exit_code == 1
    store = _store(secret_home)
    assert store.list() == ()
    assert [(event.action, event.result) for event in store.history()] == [
        ("rejected", "rejected:reserved-name")
    ]

    written = _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        "--json",
        input=value + "\n",
    )
    assert written.exit_code == 0

    listed = _invoke("list", "--agent", "worker", "--json")
    history = _invoke("history", "--agent", "worker", "--json")
    assert listed.exit_code == history.exit_code == 0
    assert value not in written.stdout + listed.stdout + history.stdout
    assert "value" not in json.loads(listed.stdout)["secrets"][0]
    assert "value" not in json.loads(history.stdout)["events"][0]


def test_rotate_uses_stdin_and_writes_rotate_event(secret_home: Path) -> None:
    old_value = "rotate-old-token-A1b2C3d4E5f6"
    new_value = "rotate-new-token-Z9y8X7w6V5u4"
    assert _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        input=old_value + "\n",
    ).exit_code == 0

    rotated = _invoke(
        "rotate",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        "--json",
        input=new_value + "\n",
    )

    assert rotated.exit_code == 0
    assert old_value not in rotated.stdout
    assert new_value not in rotated.stdout
    store = _store(secret_home)
    assert store.get("custom-api", "CUSTOM_TOKEN").value == new_value
    assert [event.action for event in store.history()] == [
        "hmac_key_created",
        "set",
        "rotate",
    ]
    assert json.loads(rotated.stdout)["credentialUnvalidatedTotal"] == 2


def test_revoke_flow_records_assertion_and_idempotently_resumes(
    secret_home: Path,
) -> None:
    value = "revoked-custom-token-A1b2C3d4E5f6"
    assert _invoke(
        "set",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--no-validate",
        input=value + "\n",
    ).exit_code == 0

    removed = _invoke(
        "remove",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--remote-revoked",
        "--remote-revoked-at",
        "2026-10-22T12:00:00Z",
        "--json",
    )

    assert removed.exit_code == 0
    assert value not in removed.stdout
    payload = json.loads(removed.stdout)
    assert payload["remote_revoked_at"] == "2026-10-22T12:00:00+00:00".replace(
        "+00:00", "Z"
    )
    assert payload["source"] == "operator_declared"
    assert payload["capability_probe"] == "not_performed"
    store = _store(secret_home)
    assert store.get("custom-api", "CUSTOM_TOKEN") is None
    assert [event.action for event in store.history()] == [
        "hmac_key_created",
        "set",
        "revoke_remote",
        "remove",
    ]
    assert value.encode() not in store.path.read_bytes()

    # A retry resumes from the existing assertion without appending another
    # revoke_remote row.
    store.set(
        service="custom-api",
        field="CUSTOM_TOKEN",
        value="replacement-after-interruption-A1b2C3d4",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    resumed = _invoke(
        "remove",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--remote-revoked",
        "--remote-revoked-at",
        "2026-10-22T12:00:00Z",
        "--json",
    )
    assert resumed.exit_code == 0
    assert json.loads(resumed.stdout)["result_reason"] == (
        "resumed_from_existing_revoke_remote"
    )
    assert [event.action for event in store.history()].count("revoke_remote") == 1

    already_removed = _invoke(
        "remove",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--field",
        "CUSTOM_TOKEN",
        "--remote-revoked",
        "--remote-revoked-at",
        "now",
        "--json",
    )
    assert already_removed.exit_code == 0
    already_payload = json.loads(already_removed.stdout)
    assert already_payload["removed"] == []
    assert already_payload["reason"] == "already_removed"
    assert already_payload["source"] == "call_time"
    assert [event.action for event in store.history()].count("revoke_remote") == 1


def test_remote_revoke_timestamp_is_strict_usage_input(secret_home: Path) -> None:
    for timestamp, reason in (
        ("2026-10-22T12:00:00", "missing_timezone"),
        ("not-a-time", "bad_remote_revoked_at_format"),
        ("2999-01-01T00:00:00Z", "remote_revoked_at_in_future"),
    ):
        result = _invoke(
            "remove",
            "--agent",
            "worker",
            "--service",
            "custom-api",
            "--field",
            "CUSTOM_TOKEN",
            "--remote-revoked",
            "--remote-revoked-at",
            timestamp,
            "--json",
        )
        assert result.exit_code == 64
        assert reason in result.stdout

    unpaired = _invoke(
        "remove",
        "--agent",
        "worker",
        "--service",
        "custom-api",
        "--remote-revoked-at",
        "now",
        "--json",
    )
    assert unpaired.exit_code == 64
    assert "requires --remote-revoked" in unpaired.stdout


def test_list_and_history_expose_incomplete_remote_revoke(
    secret_home: Path,
) -> None:
    store = _store(secret_home)
    store.set(
        service="custom-api",
        field="CUSTOM_TOKEN",
        value="interrupted-remote-revoke-value-A1b2C3",
        actor="user:owner",
        via="cli",
        at_ms=1,
    )
    store.record_event(
        action="revoke_remote",
        service="custom-api",
        field="CUSTOM_TOKEN",
        actor="user:owner",
        via="cli",
        at_ms=2,
    )

    listed = _invoke("list", "--agent", "worker", "--json")
    assert listed.exit_code == 10, listed.output
    listed_payload = json.loads(listed.stdout)
    assert len(listed_payload["secrets"]) == 1
    metadata = listed_payload["secrets"][0]
    assert metadata["service"] == "custom-api"
    assert metadata["field"] == "CUSTOM_TOKEN"
    assert metadata["fingerprint"] == store.list()[0].fingerprint
    assert metadata["present"] is True
    assert metadata["state"] == "remote_revoked_value_present"

    history = _invoke("history", "--agent", "worker", "--json")
    assert history.exit_code == 10, history.output
    history_payload = json.loads(history.stdout)
    assert history_payload["incomplete"] is True
    assert history_payload["state"] == "remote_revoked_value_present"
