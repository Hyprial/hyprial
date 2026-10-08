"""Lark gateway delivery machinery: routing, scope recovery and the outbound gateway cache."""

from __future__ import annotations

from __future__ import annotations
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority
from hyprial.daemon.impl.adapters.lark.credentials.scopes import (
    LarkScopeClient,
    LarkScopeRecovery,
    LarkScopeThrottleStore,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import Logger
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
if TYPE_CHECKING:
    pass



class _RouteGatewayCache:
    """Thread-safe cache for outbound route SDK resources.

    Keyed by adapter name AND a fingerprint of the credential the gateway was
    built from.  It used to be keyed by name alone for the daemon's lifetime,
    so a fixed secret file never reached the sender: allen-channel kept
    failing every send with Lark 10014 after its secret was repaired, until
    the daemon restarted (2026-09-26).
    """

    def __init__(self, *, retirement_capacity: int = 128, entry_capacity: int = 256) -> None:
        if retirement_capacity < 1 or entry_capacity < 1:
            raise ValueError("gateway cache capacities must be positive")
        self._lock = threading.Lock()
        self._gateways: dict[str, tuple[str, object]] = {}
        self._retiring: dict[int, object] = {}
        self._retirement_capacity = retirement_capacity
        self._entry_capacity = entry_capacity
        self._closed = False

    def get(self, name: str, fingerprint: str) -> object | None:
        with self._lock:
            entry = self._gateways.get(name)
            return entry[1] if entry is not None and entry[0] == fingerprint else None

    def put(self, name: str, fingerprint: str, gateway: object) -> object:
        # Cache values are opaque I/O-owner ports, not shared native SDKs.
        # The caller retains a new port if admission fails.
        self._drain_retired(0.0)
        with self._lock:
            if self._closed:
                raise RuntimeError("gateway cache is closed")
            entry = self._gateways.get(name)
            if entry is None and len(self._gateways) >= self._entry_capacity:
                raise RuntimeError("gateway cache entry capacity exhausted")
            same = entry is not None and entry[0] == fingerprint
            winner = entry[1] if same else gateway
            retired = gateway if same else (entry[1] if entry is not None else None)
            retires = retired is not None and retired is not winner and not any(
                key != name and value[1] is retired for key, value in self._gateways.items()
            )
            if retires and id(retired) not in self._retiring and len(self._retiring) >= self._retirement_capacity:
                raise RuntimeError("gateway cache retirement capacity exhausted")
            if not same:
                self._gateways[name] = (fingerprint, gateway)
            if retires:
                self._retiring[id(retired)] = retired
        self._drain_retired(0.0)
        return winner

    def _drain_retired(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            owners = tuple(self._retiring.items())
        for token, owner in owners:
            close = getattr(owner, "close", None)
            try:
                drained = close(max(0.0, deadline - time.monotonic())) if callable(close) else True
            except Exception:
                drained = False
            if drained is not False:
                with self._lock:
                    self._retiring.pop(token, None)
        with self._lock:
            return not self._retiring

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closed = True
            self._retiring.update((id(entry[1]), entry[1]) for entry in self._gateways.values())
            self._gateways.clear()
        return self._drain_retired(timeout)


class _LarkGatewayMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _route_lark_gateway(self, gateway_config: Any) -> Any:
        """Return a cached SDK facade for one configured Lark adapter."""

        secret_path = (
            self.hyprial_home / "secrets" / f"{gateway_config.credential_ref}.json"
        )
        secret_bytes = secret_path.read_bytes()
        fingerprint = hashlib.sha256(
            gateway_config.app_id.encode("utf-8") + b"\0" + secret_bytes
        ).hexdigest()
        existing = self._route_gateway_cache.get(gateway_config.name, fingerprint)
        if existing is not None:
            return existing
        raw_secret = json.loads(secret_bytes.decode("utf-8"))
        app_secret = (
            raw_secret.get("appSecret") if isinstance(raw_secret, dict) else None
        )
        if not isinstance(app_secret, str) or not app_secret:
            raise DaemonRequestError(
                ipc_errors.ROUTE_ADAPTER_UNCONFIGURED,
                f"credential {gateway_config.credential_ref} is missing appSecret",
                {"adapter": gateway_config.name},
            )
        owners = getattr(self, "_outbound_gateway_owners", None)
        gateway = (
            self._lark_gateway_with_scope_recovery(
                gateway_config, app_secret, self.state_dir, owned=True,
                logger=self._gateway_logger,
            )
            if owners is not None
            else self._lark_gateway_with_scope_recovery(
                gateway_config, app_secret, self.state_dir
            )
        )
        try:
            return self._route_gateway_cache.put(gateway_config.name, fingerprint, gateway)
        except BaseException:
            # The cache did not adopt this fresh SDK owner. It has never been
            # exposed to senders; close its idle mailbox before propagating.
            close = getattr(gateway, "close", None)
            if callable(close):
                close(2.0)
            raise

    @staticmethod
    def _lark_gateway_with_scope_recovery(
        gateway_config: Any, app_secret: str, state_dir: Path, *, owned: bool = False,
        logger: Logger | None = None,
    ) -> LarkSdkGateway | GatewayIoAuthority:
        """Build a gateway whose permission notification cannot recurse."""

        # Send outcomes (code + msg + native message id) belong on the
        # adapter log so a route send answers "what did Feishu return for
        # this om_" after the fact, in the same file the worker writes.
        gateway_name = getattr(gateway_config, "name", None)
        owns_logger = owned and logger is None
        gateway_logger = (
            logger.bind(name=gateway_name) if logger is not None
            else Logger.adapter(state_dir, name=gateway_name, asynchronous=owned)
        ) if isinstance(gateway_name, str) and gateway_name else None
        notification_owner = None
        try:
            gateway = LarkSdkGateway.from_credentials(gateway_config.app_id, app_secret)
            configure_logger = getattr(gateway, "set_logger", None)
            if callable(configure_logger):
                configure_logger(gateway_logger, gateway_name=gateway_name)
            default_route = next(
                (
                    item
                    for item in gateway_config.routes
                    if item.name == gateway_config.default_route
                ),
                None,
            )
            notification_routes = (
                (default_route,)
                if default_route is not None and default_route.type == "direct"
                else tuple(
                    item
                    for item in gateway_config.routes
                    if default_route is not None
                    and default_route.type == "fanout"
                    and item.name in default_route.members
                    and item.type == "direct"
                )
            )
            notification_gateway = (
                LarkSdkGateway.from_credentials(gateway_config.app_id, app_secret)
                if notification_routes
                else None
            )
            if notification_gateway is not None:
                configure_logger = getattr(notification_gateway, "set_logger", None)
                if callable(configure_logger):
                    configure_logger(gateway_logger, gateway_name=gateway_name)
            notification_owner = (
                GatewayIoAuthority(notification_gateway)
                if owned and notification_gateway is not None else None
            )

            def notify(text: str) -> None:
                target = notification_owner or notification_gateway
                if target is None:
                    return
                digest = hashlib.sha256(text.encode()).hexdigest()[:16]
                for route in notification_routes:
                    if route.native_id is not None:
                        target.send_chat(
                            route.native_id,
                            text,
                            idempotency_key=(f"scope-authorization:{route.name}:{digest}"),
                        )

            client = LarkScopeClient(gateway_config.app_id, app_secret)
            recovery = LarkScopeRecovery(
                app_id=gateway_config.app_id,
                apply=client.apply_scopes,
                notify=notify,
                throttle=LarkScopeThrottleStore(
                    state_dir / "adapters" / "lark" / "scope-authorization.json"
                ),
            )
            configure = getattr(gateway, "set_permission_recovery", None)
            if callable(configure):
                configure(recovery.handle)
            return (
                GatewayIoAuthority(
                    gateway,
                    dependents=(
                        *((notification_owner,) if notification_owner is not None else ()),
                        *((gateway_logger,) if owns_logger and gateway_logger is not None else ()),
                    ),
                )
                if owned else gateway
            )
        except BaseException:
            if notification_owner is not None:
                notification_owner.close(2.0)
            if owns_logger and gateway_logger is not None:
                gateway_logger.close(2.0)
            raise

    def _call_lark_reload(self) -> Any:
        assert self._lark_client is not None
        try:
            return self._lark_client.reload()
        except (DomainCommandError, ValueError) as error:
            raise DaemonRequestError(
                ipc_errors.ADAPTER_RELOAD_FAILED, str(error)
            ) from error

    def _lark_gateway_names(self) -> tuple[str, ...]:
        if self._lark_client is not None:
            return tuple(
                sorted(item.name for item in self._lark_client.read_adapters())
            )
        # Unit-level composition doubles may expose only the immutable name
        # projection. Production always installs `_lark_client`.
        names = getattr(self._adapters, "gateway_names", ())
        return tuple(sorted(str(item) for item in names))
