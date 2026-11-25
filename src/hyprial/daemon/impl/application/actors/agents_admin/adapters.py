"""Adapter administration IPC family: start/stop/list/status/reload."""

from __future__ import annotations
import sqlite3
from typing import Any, TYPE_CHECKING
from hyprial.daemon.impl.adapters.lark.contracts import lifecycle as lark_lifecycle
from hyprial.identity import (
    Agent,
)
from hyprial.daemon.impl.adapters.lark.contracts.errors import (
    AdapterStartError,
)
from hyprial.kernel import ipc_errors, parse_agent_uri
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    _required_string,
)


_ROUTE_SEND_REFUSALS = {
    "route-unconfigured": ("ROUTE_NOT_FOUND", "no configured adapter has this route"),
    "route-ambiguous": (ipc_errors.INVALID_ARGUMENT, "more than one configured adapter has this route; rename one"),
    "adapter-unconfigured": ("ROUTE_NOT_FOUND", "the adapter of this route is no longer configured"),
    "route-fanout-invalid": (ipc_errors.INVALID_ARGUMENT, "the route's fanout names a route that is not direct"),
    "adapter-unavailable": (ipc_errors.USER_DELIVERY_UNAVAILABLE, "the adapter could not be prepared; nothing was sent"),
    "provider-refused": (ipc_errors.USER_DELIVERY_FAILED, "the platform refused the post"),
    "outcome-unknown": (ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "the post may have arrived; check the chat before retrying"),
}


class _AdapterAdminMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _init_adapter_operations(self):
        self._lark_authorization = None
        self._lark_onboarding = None
        self._lark_authorization_closed = False

    def _close_adapter_operations(self):
        with self._registry_management_lock:
            self._lark_authorization_closed = True
            services = (self._lark_authorization, self._lark_onboarding)
        errors = []
        for operation_service in services:
            if operation_service is not None:
                try:
                    operation_service.close()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise ExceptionGroup("adapter operation cleanup unconfirmed", errors)

    def _adapter_authorization_service(self):
        from hyprial.daemon.impl.adapters.lark.credentials.authorization import create_lark_authorization_service

        with self._registry_management_lock:
            if self._lark_authorization_closed:
                raise RuntimeError("authorization service is closed")
            if self._lark_authorization is None:
                self._lark_authorization = create_lark_authorization_service(
                    self.hyprial_home, self.state_dir,
                    receipt_path=self.state_dir / "adapter-authorization" / "receipts.json",
                )
            return self._lark_authorization

    def _ipc_adapter_authorization_operation(self, params, *, action, trusted_message_origin=None):
        from hyprial.daemon.impl.adapters.lark.credentials.authorization.receipts import AuthorizationReceiptError

        operator, _ = self._identity_binding_operator(params, trusted_message_origin=trusted_message_origin)
        if not operator:
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "local operator required")
        if action == "begin":
            self._require_adapter(params.get("name"))
        try:
            service = self._adapter_authorization_service()
            if action == "begin":
                result = service.start(params["name"], capabilities=params["capabilities"], operation_key=params["operationKey"])
            elif action == "cancel":
                result = service.cancel(params["operationId"])
            else:
                result = service.operation(params["operationId"]) if "operationId" in params else service.current_operation(params["name"])
            if result is None:
                raise KeyError("missing operation")
        except KeyError as error:
            raise DaemonRequestError("ADAPTER_OPERATION_NOT_FOUND", "authorization operation not found") from error
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "authorization parameters conflict or permissions are incomplete") from error
        except AuthorizationReceiptError as error:
            raise DaemonRequestError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "authorization receipt unavailable; inspect before retrying") from error
        except RuntimeError as error:
            raise DaemonRequestError("ADAPTER_AUTHORIZATION_UNAVAILABLE", "authorization service unavailable") from error
        grant = result["authorization"]
        return {
            "ok": True, "operationId": result["operationId"], "adapter": result["adapter"],
            "provider": "lark", "kind": "authorization", "status": result["status"],
            "authorization": {
                "adapter": result["adapter"], "appId": grant.get("appId"), "state": grant["authorization"],
                "capabilities": grant["capabilities"], "missingCapabilities": grant.get("missingCapabilities", []),
                "checkedAt": int(grant["checkedAt"] * 1000),
                **({"reason": grant["reason"]} if "reason" in grant else {}),
            },
            **({"verificationUrl": result["verificationUrl"]} if "verificationUrl" in result else {}),
            **({"expiresAt": int(result["expiresAt"] * 1000)} if "expiresAt" in result else {}),
            **({"errorCode": result["errorCode"]} if "errorCode" in result else {}),
        }

    def _ipc_adapter_register(self, params, *, trusted_message_origin=None) -> Any:
        operator, _ = self._identity_binding_operator(params, trusted_message_origin=trusted_message_origin)
        if not operator:
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "local operator required")
        name = _required_string(params.get("name"), "name")
        return self._register_adapter_configuration(name, params["appId"], params["appSecret"])

    def _register_adapter_configuration(self, name, app_id, app_secret):
        from hyprial.daemon.impl.bootstrap import adapter_registration
        from hyprial.kernel import PersistentConfigError

        try:
            saved = adapter_registration.add_lark_gateway(
                hyprial_home=self.hyprial_home, state_dir=self.state_dir,
                name=name, app_id=app_id, app_secret=app_secret,
                routes=(), allow_no_routes=True,
            )
        except adapter_registration.AdapterExistsError as error:
            raise DaemonRequestError("ADAPTER_EXISTS", "adapter already exists") from error
        except adapter_registration.AdapterConfigConflictError as error:
            raise DaemonRequestError("ADAPTER_CONFIG_CONFLICT", "adapter configuration changed; inspect before retrying") from error
        except PersistentConfigError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "adapter registration rejected; check name and configuration") from error
        except Exception as error:
            raise DaemonRequestError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "adapter registration outcome unknown; reload and inspect configuration") from error
        reloaded = False
        try:
            self._ipc_adapter_reload()
            projection = self._lark_client.read_adapter(name)
            reloaded = projection is not None and projection.configured
        except Exception:
            pass
        return {
            "ok": True,
            "adapter": {key: saved["adapter"][key] for key in ("provider", "name", "appId")},
            "configured": True, "reloaded": reloaded, "nextAction": "none" if reloaded else "reload",
        }

    def _ipc_adapter_onboard(self, params, *, action, trusted_message_origin=None):
        from hyprial.daemon.impl.adapters.lark.credentials.onboard import LarkOnboardingService
        from hyprial.daemon.impl.adapters.lark.credentials.onboard.journal import OnboardingReceiptError
        from hyprial.daemon.impl.bootstrap.adapter_registration import AdapterExistsError
        from hyprial.kernel import PersistentConfigError

        operator, _ = self._identity_binding_operator(params, trusted_message_origin=trusted_message_origin)
        if not operator:
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "local operator required")
        try:
            with self._registry_management_lock:
                if self._lark_authorization_closed:
                    raise RuntimeError("adapter operations closed")
                if self._lark_onboarding is None:
                    self._lark_onboarding = LarkOnboardingService(
                        self.hyprial_home, self.state_dir, register=self._register_adapter_configuration,
                    )
                service = self._lark_onboarding
            if action == "begin":
                result = service.start(params["name"], params["operationKey"])
            elif action == "cancel":
                result = service.cancel(params["operationId"])
            else:
                result = service.operation(params["operationId"]) if "operationId" in params else service.current_operation(params["name"])
            if result is None:
                raise KeyError("missing operation")
        except AdapterExistsError as error:
            raise DaemonRequestError("ADAPTER_EXISTS", "adapter already exists") from error
        except (ValueError, PersistentConfigError) as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "app creation parameters or configuration rejected") from error
        except KeyError as error:
            raise DaemonRequestError("ADAPTER_OPERATION_NOT_FOUND", "app creation operation not found") from error
        except OnboardingReceiptError as error:
            raise DaemonRequestError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "app creation receipt unavailable; inspect before retrying") from error
        except RuntimeError as error:
            raise DaemonRequestError("ADAPTER_ONBOARD_UNAVAILABLE", "app creation unavailable") from error
        return {"ok": True, **{key: value for key, value in result.items() if key not in {"operationKey", "expiresAt"}},
                **({"expiresAt": int(result["expiresAt"] * 1000)} if "expiresAt" in result else {})}

    def _admit_configured_adapter(self, params, *, trusted_message_origin=None) -> str:
        operator, _ = self._identity_binding_operator(
            params, trusted_message_origin=trusted_message_origin
        )
        if not operator:
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "local operator required")
        return self._require_adapter(params.get("name"))

    def _ipc_adapter_routes(self, params, *, action, trusted_message_origin=None):
        from hyprial.daemon.impl.adapters.lark.credentials.chat_names import read_chat_names
        from hyprial.daemon.impl.adapters.lark.credentials.identities import adapter_namespace
        from hyprial.daemon.impl.adapters.lark.state.readers import observed_chats
        from hyprial.daemon.impl.bootstrap import adapter_registration as routes
        from hyprial.kernel import PersistentConfigError

        name = self._admit_configured_adapter(params, trusted_message_origin=trusted_message_origin)
        writing = action in {"add", "remove"}
        try:
            if action == "candidates":
                observed = observed_chats(self.state_dir / "adapters.sqlite3", adapter=adapter_namespace(name), strict=True)
                names, names_status = read_chat_names(self.hyprial_home, self.state_dir, name, {row.chat_id for row in observed})
                return {"ok": True, "adapter": name, "namesStatus": names_status, "candidates": [
                    {"chatId": row.chat_id, "chatType": row.chat_type, "chatName": names.get(row.chat_id)} for row in observed
                ]}
            if action == "list":
                row = routes.list_gateway_routes(hyprial_home=self.hyprial_home, name=name)["adapters"][0]
            elif action == "add":
                row = routes.add_gateway_route(
                    hyprial_home=self.hyprial_home, name=name,
                    route=routes.RouteInput(params["route"], params["chatId"]),
                    make_default=params["makeDefault"], expected_revision=params.get("expectedRevision"),
                )["adapter"]
            else:
                row = routes.remove_gateway_route(
                    hyprial_home=self.hyprial_home, name=name, route_name=params["route"],
                    force=params["clearDefault"], expected_revision=params.get("expectedRevision"),
                )["adapter"]
        except routes.AdapterNotFoundError as error:
            raise DaemonRequestError(ipc_errors.ADAPTER_NOT_FOUND, "adapter is not configured") from error
        except PersistentConfigError as error:
            raise DaemonRequestError(getattr(error, "code", ipc_errors.INVALID_ARGUMENT), "route configuration rejected; refresh before retrying") from error
        except Exception as error:
            raise DaemonRequestError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN if writing else ipc_errors.DAEMON_ERROR, "route operation unavailable; inspect current configuration") from error
        result = {"ok": True, "adapter": {
            "name": row["name"], "provider": row["provider"], "appId": row["appId"],
            "routes": row["routes"], "defaultRoute": row.get("defaultRoute"),
            "revision": routes.gateway_routes_revision(row),
        }}
        if not writing:
            names, names_status = read_chat_names(self.hyprial_home, self.state_dir, name, {
                route["nativeId"] for route in row["routes"] if route.get("nativeId")
            })
            result["adapter"]["namesStatus"] = names_status
            result["adapter"]["routes"] = [
                {**route, "chatName": names.get(route.get("nativeId"))} for route in row["routes"]
            ]
            return result
        reloaded = False
        try:
            self._ipc_adapter_reload()
            reloaded = True
        except Exception:
            pass
        return {**result, "configured": True, "reloaded": reloaded, "nextAction": "none" if reloaded else "reload"}

    def _ipc_adapter_routes_send(self, params, *, trusted_message_origin=None) -> Any:
        from uuid import uuid4

        from hyprial.daemon.impl.network.route_push import PushFailure, push_to_route_name

        operator, _ = self._identity_binding_operator(params, trusted_message_origin=trusted_message_origin)
        if not operator:
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "local operator required")
        route = _required_string(params.get("route"), "route")
        text = _required_string(params.get("message"), "message")
        sender = self._route_send_sender(_required_string(params.get("sender"), "sender"))
        try:
            pushed = push_to_route_name(
                self, route=route, text=text, sender=sender,
                idempotency_key=f"hyprial:route-send:{uuid4()}",
            )
        except PushFailure as failure:
            code, text = _ROUTE_SEND_REFUSALS.get(failure.reason, _ROUTE_SEND_REFUSALS["outcome-unknown"])
            cause = failure.__cause__
            self._log(
                "warn", "daemon", "adapter.routes.send", adapter=failure.adapter,
                route=failure.route or route, sender=sender, state=failure.state,
                reason=failure.reason, code=code,
                **({"chatId": failure.chat_id} if failure.chat_id else {}),
                **({"larkCode": cause.code} if isinstance(cause, LarkApiError) else {}),
            )
            raise DaemonRequestError(code, text) from failure
        self._log(
            "info", "daemon", "adapter.routes.send", adapter=pushed.adapter, route=pushed.route,
            sender=sender, state="sent", chats=[post.chat_id for post in pushed.posts],
        )
        return {"ok": True, "adapter": pushed.adapter, "route": pushed.route, "posts": [
            {"chatId": post.chat_id, "nativeMessageId": post.native_message_id} for post in pushed.posts
        ]}

    def _route_send_sender(self, actor: str) -> str:
        """The relayed-for agent of ``adapter.routes.send``: registered on this node or refused."""

        sender = self._resolve_on_behalf_actor(actor)
        if ":" not in actor:
            return sender
        parsed = parse_agent_uri(sender)
        if parsed is not None and sender in self._local_agent_candidates(parsed[2]):
            return sender
        try:
            agent = self.agents.get(sender)
        except Exception:  # noqa: BLE001 - an unreadable registry resolves nothing
            agent = None
        if agent is not None and agent.uri == sender:
            return sender
        raise DaemonRequestError(
            ipc_errors.SENDER_UNRESOLVED,
            f"sender {actor!r} is not a registered agent on this node",
            {"sender": actor},
        )

    def _ipc_adapter_identities_list(self, params, *, trusted_message_origin=None) -> Any:
        from hyprial.daemon.impl.adapters.lark.credentials.identities import open_identity_store

        name = self._admit_configured_adapter(params, trusted_message_origin=trusted_message_origin)
        try:
            store = open_identity_store(self.state_dir, name)
            try:
                rows = store.identities(kind=params.get("kind"))
                records = [{
                    "kind": row.kind, "platformId": row.platform_id,
                    "displayName": row.display_name, "unionId": row.union_id,
                    "standing": row.standing,
                    "source": row.source, "firstSeenMs": row.first_seen_ms,
                    "lastSeenMs": row.last_seen_ms,
                } for row in rows]
                return {"ok": True, "adapter": name, "count": len(records), "identities": records}
            finally:
                store.close()
        except (OSError, sqlite3.Error, ValueError) as error:
            raise DaemonRequestError(ipc_errors.DAEMON_ERROR, "platform identity records unavailable") from error

    def _ipc_adapter_authorization_status(self, params, *, trusted_message_origin=None) -> Any:
        from hyprial.daemon.impl.adapters.lark.credentials.authorization.inventory import scope_inventory

        name = self._admit_configured_adapter(params, trusted_message_origin=trusted_message_origin)
        result = scope_inventory(self.hyprial_home, self.state_dir, name, http_timeout_seconds=2.0)
        if self._lark_authorization is not None:
            from hyprial.daemon.impl.adapters.lark.credentials.authorization.receipts import AuthorizationReceiptError

            try:
                self._lark_authorization.observe(name, result)
            except AuthorizationReceiptError:
                pass
        return {
            "ok": True, "adapter": name, "appId": result["appId"], "state": result["authorization"],
            "capabilities": result["capabilities"], "missingCapabilities": result.get("missingCapabilities", []),
            "checkedAt": int(result["checkedAt"] * 1000),
            **({"reason": result["reason"]} if "reason" in result else {}),
        }

    def _ipc_adapter_identities_sync(self, params, *, trusted_message_origin=None) -> Any:
        from hyprial.daemon.impl.adapters.lark.credentials.identities import (
            configured_identity_gateway, open_identity_store, sync_identities,
        )

        name = self._admit_configured_adapter(params, trusted_message_origin=trusted_message_origin)
        try:
            app_id, gateway = configured_identity_gateway(
                self.hyprial_home, self.state_dir, name, http_timeout_seconds=2.0
            )
            store = open_identity_store(self.state_dir, name)
            try:
                report = sync_identities(
                    state=store, gateway=gateway, app_id=app_id, max_duration_seconds=6.0
                )
                return {"adapter": name, **report}
            finally:
                store.close()
        except Exception as error:
            raise DaemonRequestError(
                ipc_errors.DAEMON_ERROR,
                "platform identity sync incomplete; read recorded identities before retrying",
            ) from error

    def _ipc_adapter_reload(self) -> Any:
        assert self._lark_client is not None
        completed = self._call_lark_reload()
        summary = (
            completed.reload.to_payload()
            if completed.reload is not None
            else {
                "added": [],
                "updated": [],
                "removed": [],
                "removedRunning": [],
            }
        )
        return {"ok": True, **summary}

    def _ipc_adapter_list(self) -> Any:
        assert self._lark_client is not None
        routed = self._adapter_routed_agents()
        return {
            "ok": True,
            "adapters": [
                {**item.to_payload(), "agent": routed.get(item.name)}
                for item in self._lark_client.read_adapters()
            ],
        }

    def _adapter_routed_agents(self) -> dict[str, str]:
        """Adapter name -> bound agent URI, resolved from live cell documents.

        Cell-document adapter members are the only binding authority (I6-4).
        An unavailable cell runtime is refused with ``DAEMON_NOT_READY`` instead
        of being reported as "unbound": missing authority is not proof of no
        binding, so the Lark worker keeps its last binding or preserves the
        message rather than dead-lettering it as ``no-target``.  A cross-cell
        conflict fails closed.
        """

        runtime = getattr(self, "_cell_runtime", None)
        if runtime is None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_NOT_READY, "cell runtime is unavailable"
            )
        from hyprial.daemon.impl.cells.adapter_routing import (
            build_adapter_routing_index,
        )

        index = build_adapter_routing_index(runtime.index())
        if not index.adapters:
            return {}
        by_cell: dict[str, Agent] = {}
        for agent in self.agents.list():
            by_cell.setdefault(agent.cell_id, agent)
        routed: dict[str, str] = {}
        for adapter in index.adapters:
            target = index.lookup(adapter)
            if target is None:
                continue
            agent = by_cell.get(target.cell_id)
            if agent is not None:
                routed[adapter] = agent.uri
        return routed

    def _ipc_adapter_status(self, params) -> Any:
        assert self._lark_client is not None
        name = _required_string(params.get("name"), "name")
        status = self._lark_client.read_adapter(name)
        if status is None:
            raise DaemonRequestError(
                ipc_errors.ADAPTER_NOT_FOUND, f"adapter is not configured: {name}"
            )
        return {"ok": True, "adapter": status.to_payload()}

    def _ipc_adapter_start(self, params) -> Any:
        assert self._lark_client is not None
        name = _required_string(params.get("name"), "name")
        from hyprial.daemon.impl.bootstrap.names import require_adapter_name_available

        require_adapter_name_available(self, name)
        try:
            event = self._lark_client.start(name)
        except (AdapterStartError, DomainCommandError) as error:
            # Lifecycle verdicts keep their stable code (G1/G2); the rest
            # of the start failures keep the historical mapping.
            verdict = getattr(error, "code", None)
            code = (
                verdict
                if verdict == lark_lifecycle.ADAPTER_START_TIMEOUT
                else (
                    ipc_errors.ADAPTER_NOT_FOUND
                    if "not configured" in str(error)
                    else "ADAPTER_START_FAILED"
                )
            )
            raise DaemonRequestError(code, str(error)) from error
        projection = self._lark_client.read_adapter(name)
        if projection is None:
            raise DaemonRequestError(
                ipc_errors.ADAPTER_NOT_FOUND, f"adapter is not configured: {name}"
            )
        return {
            "ok": True,
            "changed": event.changed,
            "adapter": projection.to_payload(),
        }

    def _ipc_adapter_stop(self, params) -> Any:
        assert self._lark_client is not None
        name = _required_string(params.get("name"), "name")
        event = self._lark_client.stop(name)
        status = self._lark_client.read_adapter(name)
        if status is None:
            if event.changed:
                # A detached worker (config removed by a reload): the
                # stop did happen, and the name is now gone from every
                # view.  Report the fact instead of raising an error for
                # a stop that succeeded.
                return {
                    "ok": True,
                    "changed": True,
                    "adapter": {
                        "name": name,
                        "status": "stopped",
                        "configured": False,
                    },
                }
            raise DaemonRequestError(
                ipc_errors.ADAPTER_NOT_FOUND, f"adapter is not configured: {name}"
            )
        return {
            "ok": True,
            "changed": event.changed,
            "adapter": status.to_payload(),
        }

    def _require_adapter(self, raw: object) -> str:
        """Validate an adapter name against the configured gateways.

        Mirrors the ``adapter.status`` ADAPTER_NOT_FOUND precedent, but the
        message lists the available adapters so a mistyped name is diagnosable.
        """

        name = _required_string(raw, "name")
        available = self._lark_gateway_names()
        if name not in available:
            listing = ", ".join(available) if available else "(none configured)"
            raise DaemonRequestError(
                ipc_errors.ADAPTER_NOT_FOUND,
                f"adapter is not configured: {name}; available adapters: {listing}",
            )
        return name
