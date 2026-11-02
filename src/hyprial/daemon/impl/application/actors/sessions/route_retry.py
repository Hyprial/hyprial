"""Bounded session route settlement and transport-rebuild intent replay."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteSpec,
)
from hyprial.daemon.impl.route_registration import (
    RouteCommandError,
    RouteRegistrationClient,
)

_ROUTE_RETRY_MAX_ATTEMPTS = 12
_ROUTE_RETRY_DEADLINE_SECONDS = 3.0

#: Distinguishes "no intent recorded" from a recorded drop intent (``None``).
_UNSET = object()


class _SessionRouteRetryMixin:
    """Retry/replay behavior; DaemonApplication owns the state and route lock."""

    def _session_route_intents(self) -> dict[tuple[str, str], RouteSpec | None]:
        """The desired route state per ``(actor, owner_lease)``.

        Must be read/written under ``_interactive_route_lock``; the same lock
        covers registration (before a mutation waits) and the post-rebuild
        drain, so no registration/replay window can open.
        """

        intents = getattr(self, "_session_route_pending_intents", None)
        if intents is None:
            intents = {}
            self._session_route_pending_intents = intents
        return intents

    def _install_session_route_rebuild_hook(self) -> None:
        """Re-drain pending route intents after a transport rebuild.

        Installed lazily on the first route mutation, when the transport is
        guaranteed to exist.  The hook runs on its own daemon thread so the
        transport's nonblocking rebuild-complete hook never waits on a route
        mutation.
        """

        if getattr(self, "_session_route_rebuild_hook", None) is not None:
            return
        transport = getattr(self, "_transport", None)
        register = getattr(transport, "on_rebuild_complete", None)
        if not callable(register):
            return

        def _schedule() -> None:
            threading.Thread(
                target=self._replay_pending_session_routes,
                name="session-route-rebuild-replay",
                daemon=True,
            ).start()

        try:
            self._session_route_rebuild_hook = register(_schedule)
        except BaseException:  # noqa: BLE001 - hook install must not fail a route
            self._session_route_rebuild_hook = None

    def _replay_pending_session_routes(self) -> None:
        """Re-establish routes that were pending when a rebuild completed.

        The refresh used at startup only walks ``read_sessions()``; a route
        created during the rebuild window is not in that snapshot yet.  This
        drains the recorded intents instead, including those created mid-flight.
        """

        routes = getattr(self, "_routes", None)
        if routes is None:
            return
        with self._interactive_route_lock:
            pending = dict(self._session_route_intents())
        if not pending:
            return
        replayed = failed = pruned = 0
        for (actor, owner_lease), ensure_spec in sorted(pending.items()):
            if ensure_spec is not None and not self._pending_intent_is_live(
                actor, owner_lease
            ):
                # The session/lease the ensure was recorded for is gone; never
                # re-establish it (that would leak a zombie route forever).
                with self._interactive_route_lock:
                    intents = self._session_route_intents()
                    if intents.get((actor, owner_lease), _UNSET) is ensure_spec:
                        intents.pop((actor, owner_lease), None)
                pruned += 1
                continue
            try:
                self._settled_route_mutation(
                    routes,
                    operation_id=f"session-route:rebuild:{uuid4().hex}",
                    generation=0,
                    version=0,
                    actor=actor,
                    owner_lease=owner_lease,
                    ensure_spec=ensure_spec,
                )
            except BaseException:  # noqa: BLE001 - one route must not stop the rest
                failed += 1
            else:
                replayed += 1
        self._log(
            "info",
            "daemon",
            "session_route.rebuild_replay",
            replayed=replayed,
            failed=failed,
            pruned=pruned,
        )

    def _pending_intent_is_live(self, actor: str, owner_lease: str) -> bool:
        """An ensure intent dies with the session lease it was recorded for.

        Drop intents (``ensure_spec is None``) are always safe to retry; this
        judge only guards re-ensuring a route whose session has been replaced
        or retired.  A read failure keeps the intent (fail-open) rather than
        drop a possibly live route.
        """

        prefix = "session:"
        if not owner_lease.startswith(prefix):
            return True
        ref = owner_lease[len(prefix):]
        if ref.startswith("legacy:"):
            # Actor-keyed legacy route; the actor destroy path drops it.
            return True
        try:
            current = self._agent_session_domains.session.read_session(actor)
        except BaseException:  # noqa: BLE001 - never drop a route on a read fault
            return True
        return current is not None and current.session_ref == ref

    def _settled_route_mutation(
        self,
        routes: RouteRegistrationClient,
        *,
        operation_id: str,
        generation: int,
        version: int,
        actor: str,
        owner_lease: str,
        ensure_spec: RouteSpec | None = None,
        fence: Callable[[], bool] | None = None,
    ) -> Any:
        if not isinstance(routes, RouteRegistrationClient):
            if ensure_spec is not None:
                return routes.ensure(ensure_spec, owner_lease=owner_lease)
            return routes.drop(actor, owner_lease=owner_lease)
        key = (actor, owner_lease)
        self._install_session_route_rebuild_hook()
        with self._interactive_route_lock:
            expected_token = self._interactive_route_resource_tokens.get(key)
            self._session_route_intents()[key] = ensure_spec
        attempt_token = f"{operation_id}:attempt:0"
        correlation_id = f"{attempt_token}:completion"
        payload = (
            EnsureRouteCommand(
                correlation_id,
                attempt_token,
                generation,
                version,
                ensure_spec,
                owner_lease,
                fence,
            )
            if ensure_spec is not None
            else DropRouteCommand(
                correlation_id,
                attempt_token,
                generation,
                version,
                actor,
                owner_lease,
            )
        )
        request = LifecycleMutationRequest(
            correlation_id,
            attempt_token,
            operation_id,
            expected_token if ensure_spec is None else None,
            payload,
        )
        retry = 0
        deadline = time.monotonic() + _ROUTE_RETRY_DEADLINE_SECONDS
        while True:
            try:
                completed = routes.call_settled(request)
                break
            except RouteCommandError as error:
                if error.code in {"PORT_CLOSING", "ROUTE_LEASE_STALE"}:
                    raise
                retry += 1
                remaining = deadline - time.monotonic()
                if retry >= _ROUTE_RETRY_MAX_ATTEMPTS or remaining <= 0:
                    # A persistent retryable error must not block the caller
                    # forever: surface a terminal, attributable failure.
                    self._log(
                        "error",
                        "daemon",
                        "session_route.route_exhausted",
                        actor=actor,
                        ownerLease=owner_lease,
                        operationId=operation_id,
                        attemptToken=attempt_token,
                        attempts=retry,
                        deadlineSeconds=_ROUTE_RETRY_DEADLINE_SECONDS,
                        lastErrorCode=error.code,
                        detail=error.detail,
                    )
                    raise RouteCommandError(
                        "session_route.route_exhausted",
                        f"route mutation did not settle after {retry} "
                        f"attempts: {error.code}",
                    ) from error
                self._log(
                    "warn",
                    "daemon",
                    "session_route.route_retry",
                    actor=actor,
                    ownerLease=owner_lease,
                    operationId=operation_id,
                    attemptToken=attempt_token,
                    retry=retry - 1,
                    errorCode=error.code,
                    detail=error.detail,
                )
                time.sleep(
                    min(0.005 * (2 ** min(retry, 7)), 0.5, max(0.0, remaining))
                )
        token = completed.provenance.resource_token
        with self._interactive_route_lock:
            intents = self._session_route_intents()
            if intents.get(key, _UNSET) is ensure_spec:
                intents.pop(key, None)
            if ensure_spec is not None:
                self._interactive_route_resource_tokens[key] = token
            elif expected_token is None or token == expected_token:
                self._interactive_route_resource_tokens.pop(key, None)
            else:
                self._interactive_route_resource_tokens[key] = token
        return completed
