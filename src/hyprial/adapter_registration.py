"""Register and de-register external-platform (Lark) adapters in local config.

This is the file-level half of the adapter lifecycle: it writes exactly the
files the daemon already reads at boot -- ``channels.json`` and
``secrets/lark-<name>.json`` -- and removal deletes exactly those files plus the
adapter's desired-state harness entry and pin. It never
contacts the daemon; the running daemon captures the gateway list as a boot
snapshot, so lifecycle visibility changes only take effect after a restart.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hyprial.persistent_config import (
    ChannelConfiguration,
    ChannelRouteConfig,
    LarkGatewayConfig,
    PersistentConfigError,
    atomic_json_write,
)

# Existing gateway names are plain lowercase tokens (squire, kanban, adjutant).
# This bound also blocks path traversal in the derived ``lark-<name>`` credential
# file and the ``:`` that would break adapter-URI resolution in the daemon.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class AdapterExistsError(PersistentConfigError):
    """Raised when a gateway with the requested name already exists."""


class AdapterNotFoundError(PersistentConfigError):
    """Raised when no gateway with the requested name is configured."""


class AdapterConfigConflictError(PersistentConfigError):
    """A config resource changed outside the serialized mutation lease."""

    code = "ADAPTER_CONFIG_CONFLICT"


@dataclass(frozen=True, slots=True)
class RouteInput:
    """One direct route: a friendly name mapped to a native chat/user id."""

    name: str
    native_id: str


_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCKS: dict[Path, threading.RLock] = {}


def _config_thread_lock(path: Path) -> threading.RLock:
    key = path.absolute()
    with _CONFIG_LOCKS_GUARD:
        return _CONFIG_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _adapter_config_mutation(hyprial_home: Path) -> Iterator[None]:
    """Serialize the channels/credential pair across threads and processes."""

    home = Path(hyprial_home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / ".adapter-config.lock"
    with _config_thread_lock(path):
        stream = path.open("a+b")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()


def _optional_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _restore_if_current(
    path: Path, *, expected_current: bytes | None, prior: bytes | None
) -> None:
    """CAS rollback: never overwrite a replacement written by another owner."""

    current = _optional_bytes(path)
    if current == prior:
        return
    if current != expected_current:
        raise AdapterConfigConflictError(
            f"adapter config resource changed during rollback: {path.name}"
        )
    if prior is None:
        path.unlink(missing_ok=True)
    else:
        _restore_bytes(path, prior)


def _unlink_if_current(path: Path, expected: bytes | None) -> None:
    if _optional_bytes(path) != expected:
        raise AdapterConfigConflictError(
            f"adapter credential changed during removal: {path.name}"
        )
    path.unlink(missing_ok=True)


def _raise_rollback_failure(
    label: str,
    operation_error: BaseException,
    rollback_errors: list[BaseException],
) -> None:
    grouped = BaseExceptionGroup(
        label, [operation_error, *rollback_errors]
    )
    if any(isinstance(error, AdapterConfigConflictError) for error in rollback_errors):
        raise AdapterConfigConflictError(
            "adapter config replacement was preserved; rollback was refused"
        ) from grouped
    raise grouped from operation_error


def _validate_name(name: str) -> str:
    if not _NAME_PATTERN.fullmatch(name):
        raise PersistentConfigError(
            "adapter name must match ^[a-z0-9][a-z0-9._-]*$ "
            "(lowercase letters, digits, dot, underscore, hyphen)"
        )
    return name


def _restore_bytes(path: Path, data: bytes) -> None:
    """Atomically restore the exact prior bytes of ``path`` at mode 0600."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".restore", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_existing(channels_path: Path) -> ChannelConfiguration:
    if not channels_path.exists():
        return ChannelConfiguration()
    try:
        raw = channels_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PersistentConfigError(
            f"cannot read existing channels config {channels_path}: {error}"
        ) from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PersistentConfigError(
            f"existing channels config is not valid JSON: {error}"
        ) from error
    # Fail closed: refuse to clobber a config we cannot parse/validate.
    return ChannelConfiguration.from_json(value)


def ensure_lark_gateway_addable(
    *, hyprial_home: Path, name: str, force: bool = False
) -> None:
    """Reject a doomed registration *before* the caller pays for a credential.

    :func:`add_lark_gateway` makes these same checks, but only once it already
    holds an app secret. App onboarding obtains that secret by asking a human to
    approve the creation of a real App on a real tenant -- an act that cannot be
    undone from here. Checking first means a name typo or a clash costs nothing,
    instead of stranding a freshly created App that nothing will ever reference.
    """

    _validate_name(name)
    existing = _load_existing(Path(hyprial_home) / "channels.json")
    if not force and any(item.name == name for item in existing.gateways):
        raise AdapterExistsError(
            f"adapter {name!r} already exists; pass --force to overwrite it"
        )


def add_lark_gateway(
    *,
    hyprial_home: Path,
    name: str,
    app_id: str,
    app_secret: str,
    routes: Sequence[RouteInput],
    default_route: str | None = None,
    force: bool = False,
    allow_no_routes: bool = False,
) -> dict[str, object]:
    """Register one gateway under the shared config-mutation lease."""

    with _adapter_config_mutation(Path(hyprial_home)):
        return _add_lark_gateway_unlocked(
            hyprial_home=hyprial_home,
            name=name,
            app_id=app_id,
            app_secret=app_secret,
            routes=routes,
            default_route=default_route,
            force=force,
            allow_no_routes=allow_no_routes,
        )


def _add_lark_gateway_unlocked(
    *,
    hyprial_home: Path,
    name: str,
    app_id: str,
    app_secret: str,
    routes: Sequence[RouteInput],
    default_route: str | None = None,
    force: bool = False,
    allow_no_routes: bool = False,
) -> dict[str, object]:
    """Register a Lark adapter, writing its credential and channel entry.

    Returns a JSON-serializable summary that never contains the secret value.
    Raises :class:`hyprial.persistent_config.PersistentConfigError` (or the
    :class:`AdapterExistsError` subclass) on any validation failure, before any
    file is written.

    ``allow_no_routes`` exists for App onboarding (``hyprial adapter onboard``): an
    App that was just created has never been messaged, so no ``open_id`` or
    ``chat_id`` exists yet to bind a named outbound route to. Such a gateway is
    still inbound-capable -- inbound routes by the event's own ``chat_id`` and
    replies go through the durable reply index -- so a route-less entry is a
    legitimate first state rather than a broken one. It stays opt-in so every
    existing caller keeps the "at least one route" guarantee.
    """

    _validate_name(name)
    if not app_id:
        raise PersistentConfigError("app id must be a non-empty string")
    if not app_secret:
        # Never echo the value; only report its absence.
        raise PersistentConfigError("app secret must be a non-empty string")
    if not routes and not allow_no_routes:
        raise PersistentConfigError("at least one route is required")

    route_configs = tuple(
        ChannelRouteConfig(name=route.name, type="direct", native_id=route.native_id)
        for route in routes
    )
    resolved_default = default_route
    if resolved_default is None and len(route_configs) == 1:
        resolved_default = route_configs[0].name

    credential_ref = f"lark-{name}"
    gateway = LarkGatewayConfig(
        name=name,
        app_id=app_id,
        credential_ref=credential_ref,
        routes=route_configs,
        default_route=resolved_default,
    )

    hyprial_home = Path(hyprial_home)
    channels_path = hyprial_home / "channels.json"
    existing = _load_existing(channels_path)

    existing_names = {item.name for item in existing.gateways}
    if name in existing_names and not force:
        raise AdapterExistsError(
            f"adapter {name!r} already exists; pass --force to overwrite it"
        )

    others = tuple(item for item in existing.gateways if item.name != name)
    candidate = ChannelConfiguration(gateways=(*others, gateway))
    # Full round-trip validation BEFORE touching the filesystem: this enforces
    # route/name/credential-ref rules exactly as the daemon's loader will.
    validated = ChannelConfiguration.from_json(candidate.to_json())

    secret_path = hyprial_home / "secrets" / f"{credential_ref}.json"
    secret_backup = _optional_bytes(secret_path)
    channels_backup = _optional_bytes(channels_path)
    secret_written = _json_bytes({"appSecret": app_secret})
    channels_written = _json_bytes(validated.to_json())

    # Write the secret first, then channels.json. The daemon's loader iterates
    # every gateway and fails if any secret is missing, so a gateway entry that
    # references an absent secret would break *all* adapters. If the channels
    # write then fails, restore the prior state so the operation is a no-op: a
    # brand-new secret is removed, and an overwritten one (--force) is put back
    # to its original bytes -- a failed --force must never clobber a working
    # credential.
    try:
        atomic_json_write(secret_path, {"appSecret": app_secret})
        if _optional_bytes(secret_path) != secret_written:
            raise AdapterConfigConflictError(
                f"adapter credential changed during registration: {secret_path.name}"
            )
        atomic_json_write(channels_path, validated.to_json())
        if _optional_bytes(channels_path) != channels_written:
            raise AdapterConfigConflictError(
                "adapter channels changed during registration"
            )
    except BaseException as operation_error:
        rollback_errors: list[BaseException] = []
        for path, expected, prior in (
            (channels_path, channels_written, channels_backup),
            (secret_path, secret_written, secret_backup),
        ):
            try:
                _restore_if_current(
                    path, expected_current=expected, prior=prior
                )
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            _raise_rollback_failure(
                "adapter registration and rollback failed",
                operation_error,
                rollback_errors,
            )
        raise

    written = next(item for item in validated.gateways if item.name == name)
    return {
        "ok": True,
        "adapter": {
            "provider": "lark",
            "name": written.name,
            "appId": written.app_id,
            "credentialRef": written.credential_ref,
            "routes": [route.to_json() for route in written.routes],
            **(
                {"defaultRoute": written.default_route}
                if written.default_route is not None
                else {}
            ),
        },
        "channelsPath": str(channels_path),
        "secretPath": str(secret_path),
        "overwritten": name in existing_names,
        "note": (
            "Run 'hyprial adapter reload' so a running daemon refreshes its "
            "snapshot of the gateway list and 'hyprial adapter start' sees "
            "this adapter without a daemon restart ('hyprial adapter add' "
            "already attempts that reload), then 'hyprial adapter pin'."
        )
        + (
            ""
            if written.routes
            else (
                " This gateway has no named outbound route yet. Once the App "
                "has been messaged and you know a chat or user id, bind one "
                f"with: hyprial adapter add {written.name} --app-id "
                f"{written.app_id} --route <name>=<native-id> --force "
                "(the app secret is read from --secret-file or stdin)."
            )
        ),
    }


def remove_lark_gateway(
    *,
    hyprial_home: Path,
    state_dir: Path,
    name: str,
    management: object | None = None,
) -> dict[str, object]:
    """Remove one gateway under live or fenced-offline authorities."""

    # Preserve the old fail-before-state behavior for malformed or absent
    # gateways.  The authoritative check is repeated under the config lease;
    # this read-only preflight merely avoids constructing offline actor/SQLite
    # authority for a request that cannot mutate anything.
    _validate_name(name)
    preflight = _load_existing(Path(hyprial_home) / "channels.json")
    if not any(item.name == name for item in preflight.gateways):
        configured = ", ".join(item.name for item in preflight.gateways) or "(none)"
        raise AdapterNotFoundError(
            f"adapter {name!r} is not configured; configured adapters: {configured}"
        )

    if management is None:
        from hyprial.daemon.identity import resolve_node_owner
        from hyprial.management import OfflineManagementLease

        machine = os.environ.get("HYPRIAL_NODE_ID", "").strip() or socket.gethostname()
        with OfflineManagementLease(
            state_dir,
            owner=resolve_node_owner(),
            machine=machine,
            hyprial_home=hyprial_home,
        ) as offline:
            return remove_lark_gateway(
                hyprial_home=hyprial_home,
                state_dir=state_dir,
                name=name,
                management=offline,
            )

    with _adapter_config_mutation(Path(hyprial_home)):
        return _remove_lark_gateway_unlocked(
            hyprial_home=hyprial_home,
            state_dir=state_dir,
            name=name,
            management=management,
        )


def _remove_lark_gateway_unlocked(
    *,
    hyprial_home: Path,
    state_dir: Path,
    name: str,
    management: object | None = None,
) -> dict[str, object]:
    """De-register a Lark adapter: channels entry, credential, desired state.

    This only rewrites files; the caller must confirm the adapter is stopped
    first (the CLI checks the daemon and stops it under ``--force``).

    Removal order is ``channels.json``, then the desired-state harness entry
    and any staged legacy pin, then the adapter's pin row in
    ``agents.sqlite3``, then the credential file -- the mirror image of
    :func:`add_lark_gateway`. The daemon's config loader fails *every* gateway
    when a referenced secret is missing, so the secret is deleted only after
    the gateway entry that referenced it is gone. The exact prior bytes of
    every touched file are snapshotted first and restored on any failure (the
    sqlite pin row is snapshotted as data and re-inserted), so a failed
    remove is a no-op.
    """

    from hyprial.management import RegistryManagementHandler

    if not isinstance(management, RegistryManagementHandler):
        raise TypeError("management must be RegistryManagementHandler")

    _validate_name(name)
    hyprial_home = Path(hyprial_home)
    state_dir = Path(state_dir)
    channels_path = hyprial_home / "channels.json"
    existing = _load_existing(channels_path)

    gateway = next(
        (item for item in existing.gateways if item.name == name), None
    )
    if gateway is None:
        configured = ", ".join(item.name for item in existing.gateways) or "(none)"
        raise AdapterNotFoundError(
            f"adapter {name!r} is not configured; configured adapters: {configured}"
        )

    # Only ever delete the credential file this command's own naming scheme
    # derives (``lark-<name>``, already traversal-safe via _validate_name). A
    # hand-edited credentialRef pointing anywhere else is left in place rather
    # than deleted on a guess.
    expected_ref = f"lark-{name}"
    delete_secret = gateway.credential_ref == expected_ref
    secret_path = hyprial_home / "secrets" / f"{gateway.credential_ref}.json"

    remaining = tuple(item for item in existing.gateways if item.name != name)
    # Full round-trip validation BEFORE touching the filesystem, exactly as in
    # add_lark_gateway.
    validated = ChannelConfiguration.from_json(
        ChannelConfiguration(gateways=remaining).to_json()
    )

    # File bytes and registry rows form one compensated operation. Registry
    # mutations execute only through the typed management authority; this
    # function owns channels/credential files and restores their exact bytes
    # before the management context rolls its snapshot back.
    tracked = [channels_path, secret_path]
    backups = {
        path: _optional_bytes(path) for path in tracked
    }
    channels_written = _json_bytes(validated.to_json())
    registry_snapshot = None
    with management.adapter_removal(name) as transaction:
        registry_snapshot = transaction.snapshot
        try:
            atomic_json_write(channels_path, validated.to_json())
            if _optional_bytes(channels_path) != channels_written:
                raise AdapterConfigConflictError(
                    "adapter channels changed during removal"
                )
            transaction.commit()
            if _optional_bytes(channels_path) != channels_written:
                raise AdapterConfigConflictError(
                    "adapter channels changed before credential removal"
                )
            if delete_secret:
                _unlink_if_current(secret_path, backups[secret_path])
        except BaseException as operation_error:
            rollback_errors: list[BaseException] = []
            rollback_targets: list[tuple[Path, bytes | None, bytes | None]] = []
            if delete_secret:
                rollback_targets.append(
                    (secret_path, None, backups[secret_path])
                )
            rollback_targets.append(
                (channels_path, channels_written, backups[channels_path])
            )
            for path, expected, prior in rollback_targets:
                try:
                    _restore_if_current(
                        path, expected_current=expected, prior=prior
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                _raise_rollback_failure(
                    "adapter file removal and rollback failed",
                    operation_error,
                    rollback_errors,
                )
            raise

    assert registry_snapshot is not None
    harness_present = registry_snapshot.harness.spec is not None
    legacy_pinned_actor = registry_snapshot.harness.legacy_pin
    database_pin = registry_snapshot.agent_pin
    pinned_actor = database_pin or legacy_pinned_actor
    removed: dict[str, object] = {
        "channelsEntry": True,
        "secretFile": delete_secret and backups[secret_path] is not None,
        "desiredStateProvider": harness_present,
        "adapterPin": pinned_actor is not None,
    }
    if pinned_actor is not None:
        removed["pinnedActor"] = pinned_actor
    result: dict[str, object] = {
        "ok": True,
        "adapter": {"provider": "lark", "name": name},
        "removed": removed,
        "channelsPath": str(channels_path),
        "note": (
            "Run 'hyprial adapter reload' so a running daemon refreshes its "
            "snapshot of the gateway list and 'hyprial adapter list' forgets "
            "this adapter without a daemon restart."
        ),
    }
    if delete_secret:
        result["secretPath"] = str(secret_path)
    else:
        # The credential file did not match lark-<name>; it was NOT deleted.
        result["secretKept"] = str(secret_path)
    return result


class RouteExistsError(PersistentConfigError):
    """Raised when a route name is already bound on the gateway."""

    code = "ROUTE_EXISTS"


class RouteNotFoundError(PersistentConfigError):
    """Raised when the named route is not bound on the gateway."""

    code = "ROUTE_NOT_FOUND"


class RouteInUseError(PersistentConfigError):
    """Raised when removing a route would silently drop the gateway default."""

    code = "ROUTE_IN_USE"


def _require_gateway(
    existing: ChannelConfiguration, name: str
) -> LarkGatewayConfig:
    gateway = next((item for item in existing.gateways if item.name == name), None)
    if gateway is None:
        configured = ", ".join(item.name for item in existing.gateways) or "(none)"
        raise AdapterNotFoundError(
            f"adapter {name!r} is not configured; configured adapters: {configured}"
        )
    return gateway


def _gateway_summary(gateway: LarkGatewayConfig) -> dict[str, object]:
    return {
        "provider": "lark",
        "name": gateway.name,
        "appId": gateway.app_id,
        "credentialRef": gateway.credential_ref,
        "routes": [route.to_json() for route in gateway.routes],
        **(
            {"defaultRoute": gateway.default_route}
            if gateway.default_route is not None
            else {}
        ),
    }


def list_gateway_routes(
    *, hyprial_home: Path, name: str | None = None
) -> dict[str, object]:
    """Read the configured routes. Pure read: takes no lease, writes nothing."""

    channels_path = Path(hyprial_home) / "channels.json"
    existing = _load_existing(channels_path)
    if name is None:
        gateways = existing.gateways
    else:
        _validate_name(name)
        gateways = (_require_gateway(existing, name),)
    return {
        "ok": True,
        "adapters": [_gateway_summary(item) for item in gateways],
        "channelsPath": str(channels_path),
    }


def _write_gateway_routes(
    *,
    hyprial_home: Path,
    name: str,
    routes: tuple[ChannelRouteConfig, ...],
    default_route: str | None,
) -> dict[str, object]:
    """Replace one gateway's routes, leaving every other field untouched.

    The credential file is never opened: a route change has nothing to say
    about the App's secret, and the rollback story stays one file wide.
    Validation is the same full round trip ``add_lark_gateway`` performs, so a
    route this loader would reject is rejected before any byte is written.
    """

    hyprial_home = Path(hyprial_home)
    channels_path = hyprial_home / "channels.json"
    existing = _load_existing(channels_path)
    gateway = _require_gateway(existing, name)

    updated = LarkGatewayConfig(
        name=gateway.name,
        app_id=gateway.app_id,
        credential_ref=gateway.credential_ref,
        routes=routes,
        default_route=default_route,
    )
    others = tuple(item for item in existing.gateways if item.name != name)
    validated = ChannelConfiguration.from_json(
        ChannelConfiguration(gateways=(*others, updated)).to_json()
    )

    channels_backup = _optional_bytes(channels_path)
    channels_written = _json_bytes(validated.to_json())
    try:
        atomic_json_write(channels_path, validated.to_json())
        if _optional_bytes(channels_path) != channels_written:
            raise AdapterConfigConflictError(
                "adapter channels changed during route update"
            )
    except BaseException as operation_error:
        try:
            _restore_if_current(
                channels_path,
                expected_current=channels_written,
                prior=channels_backup,
            )
        except BaseException as rollback_error:
            _raise_rollback_failure(
                "adapter route update and rollback failed",
                operation_error,
                [rollback_error],
            )
        raise

    written = next(item for item in validated.gateways if item.name == name)
    return {
        "ok": True,
        "adapter": _gateway_summary(written),
        "channelsPath": str(channels_path),
    }


def add_gateway_route(
    *,
    hyprial_home: Path,
    name: str,
    route: RouteInput,
    make_default: bool = False,
    force: bool = False,
) -> dict[str, object]:
    """Bind one named route on an existing gateway.

    ``force`` rebinds a name that is already taken; without it an existing name
    is an error rather than a silent retarget, because the name is what senders
    address and the native id is invisible to them.
    """

    _validate_name(name)
    if not route.name or not route.native_id:
        raise PersistentConfigError("route name and native id must be non-empty")
    with _adapter_config_mutation(Path(hyprial_home)):
        existing = _load_existing(Path(hyprial_home) / "channels.json")
        gateway = _require_gateway(existing, name)
        present = next(
            (item for item in gateway.routes if item.name == route.name), None
        )
        if present is not None and not force:
            raise RouteExistsError(
                f"route {route.name!r} already exists on adapter {name!r} "
                f"(native id {present.native_id!r}); pass --force to rebind it"
            )
        replacement = ChannelRouteConfig(
            name=route.name, type="direct", native_id=route.native_id
        )
        routes = tuple(
            replacement if item.name == route.name else item
            for item in gateway.routes
        )
        if present is None:
            routes = (*gateway.routes, replacement)
        default_route = route.name if make_default else gateway.default_route
        return _write_gateway_routes(
            hyprial_home=hyprial_home,
            name=name,
            routes=routes,
            default_route=default_route,
        )


def remove_gateway_route(
    *,
    hyprial_home: Path,
    name: str,
    route_name: str,
    force: bool = False,
) -> dict[str, object]:
    """Unbind one named route.

    Removing the gateway's default route is refused unless ``force`` is given:
    dropping it silently changes where every un-addressed send lands, which is
    a routing change disguised as a deletion.
    """

    _validate_name(name)
    with _adapter_config_mutation(Path(hyprial_home)):
        existing = _load_existing(Path(hyprial_home) / "channels.json")
        gateway = _require_gateway(existing, name)
        if all(item.name != route_name for item in gateway.routes):
            bound = ", ".join(item.name for item in gateway.routes) or "(none)"
            raise RouteNotFoundError(
                f"route {route_name!r} is not bound on adapter {name!r}; "
                f"bound routes: {bound}"
            )
        if gateway.default_route == route_name and not force:
            raise RouteInUseError(
                f"route {route_name!r} is the default route of adapter {name!r}; "
                "pass --force to remove it and clear the default"
            )
        routes = tuple(item for item in gateway.routes if item.name != route_name)
        default_route = (
            None if gateway.default_route == route_name else gateway.default_route
        )
        return _write_gateway_routes(
            hyprial_home=hyprial_home,
            name=name,
            routes=routes,
            default_route=default_route,
        )
