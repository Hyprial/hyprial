"""``hyprial agent secret`` commands.

The legacy credential/grant commands still cross daemon IPC.  The per-agent
commands below operate on the identity-owned ``secrets.sqlite`` through its
public facade.  Plaintext enters only through stdin and never enters a result,
warning, metric, or event.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import _local_operator_identity
from hyprial.shell.impl.cli.output import report_error, warn

from hyprial.shell.impl.cli.commands.agent.admin import secret_app


@dataclass(frozen=True, slots=True)
class _ServiceValidator:
    """Declarative field-set and field-shape rules; never a capability probe."""

    required: tuple[str, ...]
    shapes: tuple[tuple[str, str], ...]


_SERVICE_VALIDATORS = {
    "cloudflare-r2": _ServiceValidator(
        required=("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
        shapes=(
            ("CLOUDFLARE_ACCOUNT_ID", r"[0-9a-fA-F]{32}"),
            ("CLOUDFLARE_API_TOKEN", r"\S{16,}"),
        ),
    ),
    "forgejo": _ServiceValidator(
        required=("FORGEJO_BASE_URL", "FORGEJO_TOKEN"),
        shapes=(
            ("FORGEJO_BASE_URL", r"https?://[^\s/]+(?::[0-9]+)?(?:/.*)?"),
            ("FORGEJO_TOKEN", r"\S{16,}"),
        ),
    ),
}
_METRICS_FILE = "secret-metrics.jsonl"
_UNVALIDATED_METRIC = "credential_unvalidated_total"


def _now_ms() -> int:
    return get_services().time.time_ns() // 1_000_000


def _secret_store(agent: str) -> Any:
    from hyprial.identity import AgentRegistry, AgentSecretStore

    services = get_services()
    try:
        name = AgentRegistry.normalize_actor(agent)
        return AgentSecretStore(services._hyprial_home() / "agents" / name)
    except (OSError, TypeError, ValueError) as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"cannot open private secret store for agent {agent!r}: {error}",
        ) from None


def _secret_store_exists(agent: str) -> bool:
    from hyprial.identity import agent_secret_store_exists

    services = get_services()
    try:
        return agent_secret_store_exists(services._hyprial_home(), agent)
    except (OSError, TypeError, ValueError) as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"cannot inspect private secret store for agent {agent!r}: {error}",
        ) from None


def _read_stdin_values(field_names: list[str]) -> dict[str, str]:
    services = get_services()
    if services._stdin_isatty():
        raise services.CliError(
            "SECRET_REQUIRED",
            "secret write refuses a TTY; pipe exactly one value per --field on stdin",
        )
    if not field_names or len(field_names) != len(set(field_names)):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "secret write requires one or more unique --field options",
        )
    raw = sys.stdin.read()
    if raw.endswith("\n"):
        raw = raw[:-1]
    values = raw.split("\n")
    if (
        len(values) != len(field_names)
        or any(not value or "\r" in value for value in values)
    ):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "secret write requires one non-empty line per --field on stdin",
        )
    return dict(zip(field_names, values, strict=True))


def _validate_service_shape(
    store: Any,
    *,
    service: str,
    incoming: dict[str, str],
    no_validate: bool,
    actor: str,
    at_ms: int,
) -> tuple[bool, str | None]:
    services = get_services()
    validator = _SERVICE_VALIDATORS.get(service)
    if validator is None:
        if no_validate:
            return False, (
                f"warning: no validator for service {service!r}; "
                "fields written unvalidated"
            )
        store.record_event(
            action="rejected",
            service=service,
            field=",".join(incoming),
            actor=actor,
            via="cli",
            at_ms=at_ms,
            result="rejected:no-validator",
        )
        raise services.CliError(
            "SECRET_VALIDATOR_REQUIRED",
            f"no validator for service {service!r}; pass --no-validate to write it",
        )
    if no_validate:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"service {service!r} has a validator; remove --no-validate",
        )

    final = {secret.field: secret.value for secret in store.list(service=service)}
    final.update(incoming)
    missing = [field for field in validator.required if field not in final]
    invalid = [
        field
        for field, pattern in validator.shapes
        if field in final and re.fullmatch(pattern, final[field]) is None
    ]
    if missing or invalid:
        store.record_event(
            action="rejected",
            service=service,
            field=",".join(incoming),
            actor=actor,
            via="cli",
            at_ms=at_ms,
            result="rejected:validator-shape",
        )
        details: list[str] = []
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        if invalid:
            details.append("invalid field shapes: " + ", ".join(invalid))
        raise services.CliError(
            "SECRET_VALIDATION_FAILED",
            f"service {service!r} failed declarative validation ({'; '.join(details)})",
        )
    return True, None


def _append_unvalidated_metric(agent_home: Path) -> int:
    """Append one durable +1 row; days with no increments write no rows."""

    from hyprial.kernel import (
        PrivateAccessUnsupported,
        harden_private_path,
        private_metadata_violation,
    )

    services = get_services()
    path = agent_home / _METRICS_FILE
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        try:
            violation = private_metadata_violation(path, metadata, mode=0o600)
        except OSError as error:
            raise services.CliError(
                "SECRET_METRIC_UNAVAILABLE", f"cannot inspect secret metric: {error}"
            ) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or violation is not None
        ):
            raise services.CliError(
                "SECRET_METRIC_INVALID",
                "secret metric log must be a private regular file",
            )
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            harden_private_path(path, mode=0o600)
            row = json.dumps(
                {"metric": _UNVALIDATED_METRIC, "delta": 1},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            written = 0
            while written < len(row):
                written += os.write(descriptor, row[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        rows = path.read_text(encoding="utf-8").splitlines()
    except PrivateAccessUnsupported:
        raise services.CliError(
            "SECRET_METRIC_UNAVAILABLE",
            "private access is unsupported for the secret metric",
        ) from None
    except OSError as error:
        raise services.CliError(
            "SECRET_METRIC_UNAVAILABLE", f"cannot update secret metric: {error}"
        ) from None
    total = 0
    for row in rows:
        try:
            parsed = json.loads(row)
        except json.JSONDecodeError:
            parsed = None
        if parsed != {"metric": _UNVALIDATED_METRIC, "delta": 1}:
            raise services.CliError(
                "SECRET_METRIC_INVALID", "secret metric log contains an invalid row"
            )
        total += 1
    return total


def _visible_fingerprint(
    fingerprint: str | None, kind: str | None, *, available: bool
) -> str | None:
    if fingerprint is None:
        return None
    if not available or kind == "legacy_plaintext_tail":
        return "unavailable"
    return fingerprint


def _secret_metadata(
    secret: Any, *, fingerprint_available: bool = True
) -> dict[str, object]:
    return {
        "service": secret.service,
        "field": secret.field,
        "fingerprint": _visible_fingerprint(
            secret.fingerprint,
            secret.fingerprint_kind,
            available=fingerprint_available,
        ),
        "updatedAtMs": secret.updated_at_ms,
    }


def _event_metadata(
    event: Any, *, fingerprint_available: bool = True
) -> dict[str, object]:
    data = asdict(event)
    kind = event.fingerprint_kind
    result = {
        "seq": data["seq"],
        "atMs": data["at_ms"],
        "actor": data["actor"],
        "via": data["via"],
        "action": data["action"],
        "service": data["service"],
        "field": data["field"],
        "fingerprint": _visible_fingerprint(
            data["fingerprint"], kind, available=fingerprint_available
        ),
        "result": data["result"],
    }
    if kind is not None:
        result["fingerprintKind"] = kind
    return result


def _incomplete_remote_revocations(store: Any) -> set[tuple[str, str]]:
    """Derive interrupted revoke state only from the append-only journal."""

    incomplete: set[tuple[str, str]] = set()
    for event in store.history():
        if event.field is None:
            continue
        key = (event.service, event.field)
        if event.action == "remove":
            incomplete.discard(key)
        elif event.action == "revoke_remote":
            incomplete.add(key)
    return incomplete


def _has_remote_revoke_event(
    store: Any, service: str, field: str | None = None
) -> bool:
    return any(
        event.action == "revoke_remote"
        and (field is None or event.field == field)
        for event in store.history(service=service)
    )


def _remote_revoke_time(
    raw: str | None, *, at_ms: int, json_output: bool
) -> tuple[str, str]:
    """Normalize an operator-declared timestamp or exit in the usage domain."""

    services = get_services()

    def usage_error(reason: str) -> NoReturn:
        error = services.CliError(
            ipc_errors.INVALID_ARGUMENT, f"invalid --remote-revoked-at: {reason}"
        )
        report_error(error, json_output=json_output)
        raise typer.Exit(code=64)

    if raw is None:
        usage_error("missing_remote_revoked_at")
    if raw == "now":
        instant = datetime.fromtimestamp(at_ms / 1000, UTC)
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "call_time"
    if "T" not in raw:
        usage_error("bad_remote_revoked_at_format")
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        usage_error("bad_remote_revoked_at_format")
    if instant.tzinfo is None or instant.utcoffset() is None:
        usage_error("missing_timezone")
    normalized = instant.astimezone(UTC)
    if normalized.timestamp() * 1000 > at_ms + 60_000:
        usage_error("remote_revoked_at_in_future")
    return normalized.isoformat().replace("+00:00", "Z"), "operator_declared"


def _exit_fingerprint_unavailable(
    unavailable: Exception, *, json_output: bool
) -> NoReturn:
    services = get_services()
    reason = getattr(unavailable, "reason", None)
    if reason == "key_mismatch":
        message = (
            "fingerprint_unavailable{key_mismatch}: the per-agent HMAC key "
            "does not match its lifecycle event"
        )
    else:
        message = "fingerprint_unavailable: the per-agent HMAC key is absent or unreadable"
    error = services.CliError("FINGERPRINT_UNAVAILABLE", message)
    report_error(error, json_output=json_output)
    raise typer.Exit(code=10)


def _write_secret_values(
    *,
    command: str,
    agent: str,
    service: str,
    fields: list[str],
    no_validate: bool,
    json_output: bool,
) -> dict[str, object]:
    from hyprial.identity import (
        FingerprintUnavailable,
        secret_environment_name_is_allowed,
    )

    services = get_services()
    values = _read_stdin_values(fields)
    store = _secret_store(agent)
    actor = _local_operator_identity()
    at_ms = _now_ms()
    invalid_fields = [
        field for field in fields if not secret_environment_name_is_allowed(field)
    ]
    if invalid_fields:
        invalid = invalid_fields[0]
        event_count = len(store.history())
        try:
            store.set(
                service=service,
                field=invalid,
                value=values[invalid],
                actor=actor,
                via="cli",
                at_ms=at_ms,
            )
        except ValueError as error:
            if len(store.history()) == event_count:
                store.record_event(
                    action="rejected",
                    service=service,
                    field=invalid,
                    actor=actor,
                    via="cli",
                    at_ms=at_ms,
                    result="rejected:invalid-field",
                )
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT, f"secret write rejected: {error}"
            ) from None
        raise AssertionError("environment validator accepted an invalid field")
    validated, warning = _validate_service_shape(
        store,
        service=service,
        incoming=values,
        no_validate=no_validate,
        actor=actor,
        at_ms=at_ms,
    )
    written = []
    try:
        for field, value in values.items():
            method = store.set if command == "set" else store.rotate
            written.append(
                method(
                    service=service,
                    field=field,
                    value=value,
                    actor=actor,
                    via="cli",
                    at_ms=at_ms,
                )
            )
    except FingerprintUnavailable as error:
        _exit_fingerprint_unavailable(error, json_output=json_output)
    except KeyError as error:
        raise services.CliError(
            "SECRET_NOT_FOUND",
            f"cannot rotate absent secret {service!r}/{error.args[0][1]!r}",
        ) from None
    except ValueError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, f"secret write rejected: {error}"
        ) from None

    result: dict[str, object] = {
        "agent": agent,
        "service": service,
        "fields": [secret.field for secret in written],
        "secrets": [_secret_metadata(secret) for secret in written],
        "validated": validated,
    }
    if warning is not None:
        total = _append_unvalidated_metric(store.agent_home)
        warn(warning, json_output=json_output)
        result["credentialUnvalidatedTotal"] = total
        result["warnings"] = [warning]
    return result


@secret_app.command("set")
def agent_secret_set(
    agent: str = typer.Option(..., "--agent", help="Registered agent name."),
    service: str = typer.Option(..., "--service", help="Credential service name."),
    fields: list[str] = typer.Option(
        ..., "--field", help="Environment field; repeat once per stdin line."
    ),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="Explicitly allow a service with no declarative validator.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set one or more fields from non-TTY stdin without exposing values."""

    get_services()._execute(
        lambda: _write_secret_values(
            command="set",
            agent=agent,
            service=service,
            fields=fields,
            no_validate=no_validate,
            json_output=json_output,
        ),
        json_output=json_output,
    )


@secret_app.command("rotate")
def agent_secret_rotate(
    agent: str = typer.Option(..., "--agent", help="Registered agent name."),
    service: str = typer.Option(..., "--service", help="Credential service name."),
    field: str = typer.Option(..., "--field", help="Existing environment field."),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="Explicitly allow a service with no declarative validator.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Replace one existing field from non-TTY stdin."""

    get_services()._execute(
        lambda: _write_secret_values(
            command="rotate",
            agent=agent,
            service=service,
            fields=[field],
            no_validate=no_validate,
            json_output=json_output,
        ),
        json_output=json_output,
    )


@secret_app.command("remove")
def agent_secret_remove(
    agent: str = typer.Option(..., "--agent", help="Registered agent name."),
    service: str = typer.Option(..., "--service", help="Credential service name."),
    field: str | None = typer.Option(None, "--field", help="One field; omit for service."),
    remote_revoked: bool = typer.Option(
        False,
        "--remote-revoked",
        help="Assert that the operator already revoked the remote credential.",
    ),
    remote_revoked_at: str | None = typer.Option(
        None,
        "--remote-revoked-at",
        help="Operator-declared remote revocation time (not journaled).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Delete local values; optionally journal a prior operator assertion."""

    services = get_services()

    def operation() -> dict[str, object]:
        if not remote_revoked and remote_revoked_at is not None:
            error = services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--remote-revoked-at requires --remote-revoked",
            )
            report_error(error, json_output=json_output)
            raise typer.Exit(code=64)
        store = _secret_store(agent)
        actor = _local_operator_identity()
        at_ms = _now_ms()
        normalized_remote_at: str | None = None
        source: str | None = None
        if remote_revoked:
            normalized_remote_at, source = _remote_revoke_time(
                remote_revoked_at, at_ms=at_ms, json_output=json_output
            )
        targets = tuple(
            secret
            for secret in store.list(service=service)
            if field is None or secret.field == field
        )
        resumed = any(
            _has_remote_revoke_event(store, secret.service, secret.field)
            for secret in targets
        )
        if remote_revoked:
            for secret in targets:
                if _has_remote_revoke_event(store, secret.service, secret.field):
                    continue
                store.record_event(
                    action="revoke_remote",
                    service=service,
                    field=secret.field,
                    actor=actor,
                    via="cli",
                    at_ms=at_ms,
                )
        removed = store.remove(
            service=service,
            field=field,
            actor=actor,
            via="cli",
            at_ms=at_ms,
        )
        if resumed and removed:
            reason = "resumed_from_existing_revoke_remote"
        elif not removed:
            reason = (
                "already_removed"
                if remote_revoked
                and _has_remote_revoke_event(store, service, field)
                else "target_absent"
            )
        else:
            reason = "removed"
        result: dict[str, object] = {
            "agent": agent,
            "service": service,
            "removed": [
                _secret_metadata(
                    secret, fingerprint_available=store.fingerprint_available
                )
                for secret in removed
            ],
            "reason": reason,
        }
        if remote_revoked:
            result.update(
                {
                    "remote_revoked_at": normalized_remote_at,
                    "source": source,
                    "capability_probe": "not_performed",
                    "result_reason": reason,
                }
            )
        return result

    services._execute(operation, json_output=json_output)


@secret_app.command("history")
def agent_secret_history(
    agent: str = typer.Option(..., "--agent", help="Registered agent name."),
    service: str | None = typer.Option(None, "--service", help="Filter by service."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read append-only secret events; plaintext is never selected."""

    services = get_services()
    incomplete = False

    def operation() -> dict[str, object]:
        nonlocal incomplete
        if not _secret_store_exists(agent):
            return {"agent": agent, "events": [], "incomplete": False}
        store = _secret_store(agent)
        events = store.history(service=service)
        keys = _incomplete_remote_revocations(store)
        if service is not None:
            keys = {key for key in keys if key[0] == service}
        incomplete = bool(keys)
        result: dict[str, object] = {
            "agent": agent,
            "events": [
                _event_metadata(
                    event, fingerprint_available=store.fingerprint_available
                )
                for event in events
            ],
            "incomplete": incomplete,
        }
        if incomplete:
            result["state"] = "remote_revoked_value_present"
        return result

    services._execute(operation, json_output=json_output)
    if incomplete:
        raise typer.Exit(code=10)


@secret_app.command("provider-" + "write")
def agent_secret_entry_write(
    entry_id: str = typer.Argument(..., help="Secret entry id."),
    field_name: str = typer.Option(..., "--field", help="JSON field name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Write one model-vendor secret from a non-TTY stdin stream."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if sys.stdin.isatty():
            raise services.CliError(
                "SECRET_REQUIRED",
                "secret write refuses a TTY; pipe exactly one secret value on stdin",
            )
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        if not value or "\n" in value or "\r" in value:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "secret write requires one non-empty line on stdin",
            )
        return services._daemon_request(
            "agent.secret." + "provider-write",
            {"entryId": entry_id, "fieldName": field_name, "value": value},
        )

    services._execute(operation, json_output=json_output)


@secret_app.command("grant")
def agent_secret_grant(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Option(..., "--grant-id", help="Stable grant id."),
    source: str = typer.Option(..., "--source", help="user-" + "provider, agent-private or agent-shared"),
    owner_agent: str | None = typer.Option(None, "--owner-agent", help="Source owner for agent-shared."),
    entry_id: str = typer.Option(..., "--entry-id", help="Secret entry id."),
    field_name: str = typer.Option(..., "--field-name", help="Model-vendor field name."),
    environment_name: list[str] = typer.Option(
        ..., "--environment-name", help="Approved environment variable name; repeatable."
    ),
    revision: int = typer.Option(..., "--revision", help="Positive grant revision."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Grant one named secret entry to one current agent incarnation."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.secret.grant",
            {
                "actor": actor,
                "grantId": grant_id,
                "source": source,
                "ownerActor": owner_agent,
                "entryId": entry_id,
                "fieldName": field_name,
                "environmentNames": environment_name,
                "revision": revision,
            },
        ),
        json_output=json_output,
    )


@secret_app.command("ls")
def agent_secret_grants(
    actor: str | None = typer.Argument(None, help="Agent name; omit to list all grants."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List explicit grants and their owner references, never values."""
    services = get_services()
    services._execute(
        lambda: services._daemon_request("agent.secret.list", {"actor": actor}),
        json_output=json_output,
    )


@secret_app.command("list")
def agent_secret_list(
    agent: str = typer.Option(..., "--agent", help="Registered agent name."),
    service: str | None = typer.Option(None, "--service", help="Filter by service."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List current secret metadata without selecting plaintext for output."""

    services = get_services()
    incomplete = False

    def operation() -> dict[str, object]:
        nonlocal incomplete
        if not _secret_store_exists(agent):
            return {"agent": agent, "secrets": []}
        store = _secret_store(agent)
        rows = store.list(service=service)
        keys = _incomplete_remote_revocations(store)
        if service is not None:
            keys = {key for key in keys if key[0] == service}
        incomplete = bool(keys)
        secrets: list[dict[str, object]] = []
        for secret in rows:
            metadata = _secret_metadata(
                secret, fingerprint_available=store.fingerprint_available
            )
            metadata.update(
                {
                    "present": True,
                    "state": (
                        "remote_revoked_value_present"
                        if (secret.service, secret.field) in keys
                        else "present"
                    ),
                }
            )
            secrets.append(metadata)
        return {"agent": agent, "secrets": secrets}

    services._execute(operation, json_output=json_output)
    if incomplete:
        raise typer.Exit(code=10)


@secret_app.command("revoke")
def agent_secret_revoke(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Argument(..., help="Grant id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Revoke one legacy grant; the underlying credential entry remains intact."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.secret.revoke", {"actor": actor, "grantId": grant_id}
        ),
        json_output=json_output,
    )
