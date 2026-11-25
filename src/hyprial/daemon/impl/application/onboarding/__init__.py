"""Assembly-only acquisition point for the M2 onboarding request port.

The single public acquisition point is ``DaemonApplication.onboarding``.  This
mixin owns no identity, credential, journal or phase state: it stores the one
request port the bootstrap/promotion wiring assembled and runs its daemon-ready
hook at the existing ``_write_pid_file`` boundary.

The admitted ``CallContext`` is attached by the public control/HTTP transport
with ``admitted_onboarding_context`` for exactly the duration of one handler and
cleared in a ``finally`` block.  Request bodies, bearer tokens and
``trusted_message_origin`` cannot construct or modify it.  This keeps caller
identity out of the request parameter domain before any handler runs.

The MRO line that adds this mixin to ``DaemonApplication`` is a public-line
(contract/assembly) edit.  Here the mixin is deliberately independent of the
concrete port and of the runtime.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface

from .errors import OnboardingGatewayError

_EXACT_PAIRS = frozenset(
    {("bootstrap", "local-bootstrap"), ("serving", "local-gui")}
)
_FRAME = threading.local()


def _frame_stack() -> list[CallContext]:
    stack = getattr(_FRAME, "contexts", None)
    if stack is None:
        stack = []
        _FRAME.contexts = stack
    return stack


@contextmanager
def admitted_onboarding_context(context: CallContext) -> Iterator[None]:
    """Attach one already-admitted frame and clear it in ``finally``.

    Only the public control/HTTP transport calls this, after it has verified the
    OS peer or the authenticated HTTP session and resolved the registered
    host/view binding.  The exact phase/caller pair is re-checked here so a
    bootstrap frame cannot be smuggled into a serving request.  Nested frames
    are supported for tests but only the innermost is visible.
    """

    if not isinstance(context, CallContext):
        raise TypeError("an admitted CallContext is required")
    if (context.phase, context.caller) not in _EXACT_PAIRS:
        raise ValueError("the onboarding phase/caller pair is not admitted")
    stack = _frame_stack()
    stack.append(context)
    try:
        yield
    finally:
        stack.pop()
        if not stack:
            try:
                del _FRAME.contexts
            except AttributeError:
                pass


def current_onboarding_context() -> CallContext | None:
    """The innermost admitted frame for this thread, if any."""

    stack = getattr(_FRAME, "contexts", None)
    return stack[-1] if stack else None


class _OnboardingAssemblyMixin:
    """Hold the one assembled onboarding request port for this application."""

    _onboarding_port: Any | None = None
    #: True only when this daemon assembled a *serving* account port; the IPC
    #: operator context is synthesized solely behind this marker.
    _onboarding_serving: bool = False
    #: One operator surface per daemon generation (never re-minted per request),
    #: so get/cancel see the same view that begin_auth recorded.
    _onboarding_operator_surface: Any | None = None

    @property
    def onboarding(self) -> Any:
        """The single acquisition point used by the six request handlers."""

        port = self._onboarding_port
        if port is None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_NOT_READY,
                "onboarding services are not assembled",
            )
        return port

    def install_onboarding(self, port: Any) -> None:
        """Install the request port once; assembly code calls this, requests never do."""

        if port is None:
            raise ValueError("an onboarding request port is required")
        for name in (
            "status",
            "begin_auth",
            "get",
            "cancel",
            "ensure_device",
            "initialize",
            "take_post_reply",
        ):
            if not callable(getattr(port, name, None)):
                raise ValueError(
                    f"the onboarding request port is missing {name}()"
                )
        if self._onboarding_port is not None:
            raise RuntimeError("onboarding services are already assembled")
        self._onboarding_port = port
        # The catalog projects the onboarding methods from "a host is registered"
        # (catalog.py), so the marker belongs to registration, not to the one
        # assembly path that happens to install the port today.
        self._onboarding_http_ready = True

    def onboarding_context(
        self, *, trusted_message_origin: str | None
    ) -> CallContext:
        """Return the admitted frame; never derive identity from request input."""

        context = current_onboarding_context()
        if context is not None:
            # An admitted control frame always wins: the GUI's own v2 frames also
            # arrive with origin "ipc", and they must keep their local-gui caller.
            return context
        if (
            trusted_message_origin == "ipc"
            and self._onboarding_serving
            and self._onboarding_port is not None
        ):
            # The daemon's own local operator (the CLI forwarded over the request
            # socket).  Identity comes from this process, never from params.
            return CallContext(
                surface=self._operator_surface(),
                phase="serving",
                caller="local-operator",
            )
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "no admitted control frame is attached to this request",
        )

    def _operator_surface(self) -> Any:
        surface = self._onboarding_operator_surface
        if surface is None:
            surface = Surface(
                home_binding=str(self.hyprial_home),
                host_control_session_id=f"daemon-operator-{self.epoch}",
                view_session_id="local-operator",
                transport_generation=self.epoch,
            )
            self._onboarding_operator_surface = surface
        return surface

    def _write_pid_file(self) -> None:
        """Assemble the account port, publish daemon.json, then signal ready.

        Invariants, in order:

        * the port is assembled *before* ``daemon.json`` is published, so the
          file a CLI treats as "the daemon is ready" never appears before the
          account port exists;
        * requests cannot be handled in between: ``_start_server`` only binds,
          listens and sets a timeout, and the accept loop starts later in
          ``_serve`` (DaemonStartupPhase after PID_FILE), so an early client
          only queues in the kernel backlog;
        * direct-``_serve`` test drivers bypass this hook entirely (they never
          run the startup sequence), so such a fixture must call
          ``_assemble_onboarding()`` itself instead of relying on startup.
        """

        self._assemble_onboarding()
        super()._write_pid_file()
        port = self._onboarding_port
        hook = getattr(port, "on_daemon_ready", None) if port is not None else None
        if callable(hook):
            hook()

    def _assemble_onboarding(self) -> None:
        """Install the serving account port exactly once, from durable state.

        A promotion may already have installed a port in this same process (it
        owns the bootstrap journal); attach the reply service to that port
        without reopening its journal.  An already-authenticated home gets
        the one journal owner it needs; an ownerless home gets no port at all,
        and ``auth.logout`` answers DAEMON_NOT_READY truthfully.  The port's
        daemon-owned logout requests this daemon's own stop through
        ``self._request_stop`` (the same fire-and-forget entry the internal
        ``shutdown`` path uses) after a terminal receipt is durable.
        """

        from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
            ServingOnboardingGateway,
            startup_decision,
        )
        from hyprial.daemon.impl.onboarding.operations import OperationService

        if self._onboarding_port is not None:
            port = self._onboarding_port
            if isinstance(port, ServingOnboardingGateway) and getattr(port, "first_run_attempt", None) is None:
                port.first_run_attempt = self._first_run_attempt_port(port, port._operations.root)
            return

        decision = startup_decision(home=self.hyprial_home)
        if decision.mode != "serving":
            return
        service = OperationService.open(self.state_dir / "onboarding")
        gateway = ServingOnboardingGateway(
            operations=service,
            home=self.hyprial_home,
            request_stop=lambda: self._request_stop("auth-logout"),
        )
        gateway.first_run_attempt = self._first_run_attempt_port(gateway, service.root)
        self.install_onboarding(gateway)
        self._onboarding_serving = True

    def _first_run_attempt_port(self, gateway: Any, onboarding_root: Any) -> Any:
        """Compose the P3 attempt over the identity registry and live inbox."""

        from hyprial.daemon.impl.application.onboarding.first_run.port import (
            FirstRunAttemptPort,
        )
        from hyprial.daemon.impl.application.onboarding.first_run.selection import (
            agent_selection_reader,
            current_attempt_reader,
            login_epoch_reader,
            reply_reader,
            send_binding_reader,
        )
        from hyprial.daemon.impl.application.onboarding.first_run.service import (
            FirstRunAttemptService,
        )
        from hyprial.daemon.impl.onboarding.first_run import FirstRunStore
        from hyprial.daemon.impl.onboarding.first_run_attempt import (
            FirstRunAttemptStore,
        )

        domains = getattr(self, "_agent_session_domains", None)
        registry = getattr(domains, "_registry", None)
        select_agent = (
            agent_selection_reader(registry) if registry is not None
            else lambda _agent_uri: None
        )
        attempt_service = FirstRunAttemptService(
            home=self.hyprial_home,
            onboarding_root=onboarding_root,
            attempts=FirstRunAttemptStore(onboarding_root),
            receipts=FirstRunStore(onboarding_root),
            login_epoch=login_epoch_reader,
            select_agent=select_agent,
            send_binding=lambda sender, recipient, conversation, key: send_binding_reader(
                getattr(self, "_inbox", None)
            )(sender, recipient, conversation, key),
            reply_for=lambda sender, recipient, request, conversation: reply_reader(
                getattr(self, "_inbox", None)
            )(sender, recipient, request, conversation),
        )

        current = current_attempt_reader(
            attempt_service,
            lambda: login_epoch_reader(self.hyprial_home),
            select_agent,
        )
        return FirstRunAttemptPort(gateway, attempt_service, current=current)


__all__ = [
    "OnboardingGatewayError",
    "_OnboardingAssemblyMixin",
    "admitted_onboarding_context",
    "current_onboarding_context",
]
