"""The transport-neutral dispatcher of the ownerless (bootstrap) phase.

Decision: daemon-first-z1-dispatcher-extraction-20261115.  The part that
admits a ``BootstrapRuntime`` (two continuous locks and ``state/bootstrap.json``),
mounts the bootstrap onboarding gateway and answers ``ping``, ``daemon.catalog``,
the callable onboarding methods and every refusal lives here, with no HTTP,
listener or GUI control-session knowledge.  The daemon-socket wire host is its
one caller and states what is specific to it through ``WIRE_POLICY``: a declared
method that is not callable is refused with the dedicated ``ACCOUNT_REQUIRED``
code (owner decision OD-Z5, 2026-11-15) and ``data.reason`` of the same name, and
so is a name the serving router answers from its own branches without a
declaration (``ROUTER_ONLY_METHODS``); a name no serving daemon answers gets
``METHOD_NOT_FOUND`` exactly as the serving daemon does, an HTTP-exposed method
that is not callable is projected ``not-ready``
with that reason so the method set does not depend on the phase, and ``ping``
additionally carries the fields existing clients read (``running``, ``pid``,
``socket``, ``restorePending``).  Methods with no HTTP exposure stay
``disabled`` in every phase.

Decision: daemon-first-z6b-no-hosted-bootstrap-20261117.  The GUI-hosted caller
and its ``GUI_POLICY`` (``CALLER_NOT_AUTHORIZED`` refusal, ping without the
wire fields, GUI control-session parameters on ``admit``) are deleted: the GUI
child is a client of the daemon's wire host and projects the daemon's catalog
itself (``GuiRuntimeHost``).  ``BootstrapPolicy`` stays as the one dataclass
``WIRE_POLICY`` instantiates.

Decision: daemon-first-z4b-router-only-refusal-20261117.  A name the serving
daemon answers is ``ACCOUNT_REQUIRED`` here, declared or not; the one list of
the undeclared names is ``ROUTER_ONLY_METHODS``.

``ACCOUNT_REQUIRED`` is only a daemon-local constant, not a declared error:
unlike the onboarding codes a MethodSpec declares (published in the OpenAPI)
it is in no MethodSpec error list nor the HTTP contract yet (Z-6c); it is also
the catalog reason token the serving projection publishes.  No identity is invented: the dispatcher never
accepts an owner, phase or caller from request input, and
``onboarding.initialize`` is admitted only after a foreground owner registered
a wake callback with ``enable_promotion``.

Decision: daemon-first-z5-first-login-20261118.  The dispatcher owns the one
``FirstLogin`` controller of its runtime (the daemon-driven login of
``auth.login.begin`` with ``until: serving``).  ``wait`` answers an
``auth.operation.get`` that asks for ``waitSeconds`` from the run's published
view WITHOUT the dispatcher lock and without the journal, so ``ping`` and
``cancel`` are answered meanwhile and a wait never overruns its seconds by a lock
hold (the driver holds the lock across a device registration).  The driver, not
this class, makes sure ``promote`` is signalled only after it left the lock (see
the driver's header), so no barrier is needed here.  ``close`` ends every driver
before it interrupts the runtime.  Every wait is bounded by the caller's seconds.
"""

from __future__ import annotations

import os
import threading
import time
from functools import lru_cache
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from hyprial import __version__
from hyprial.daemon.impl.onboarding.runtime import BootstrapRuntime
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.daemon.impl.request_catalog.onboarding.specs import AUTH_OPERATION_GET_SPEC
from hyprial.kernel import (
    DaemonRequestError,
    ipc_errors,
    resolve_node_id,
)

from .. import admitted_onboarding_context
from ..first_login import FirstLogin
from ..service import BootstrapOnboardingGateway

BOOTSTRAP_CALLABLE_METHODS = frozenset(
    {
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
        "auth.relogin",
        "auth.logout",
        "onboarding.device.ensure",
    }
)
INITIALIZE_METHOD = "onboarding.initialize"
ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
WAITABLE_METHOD = "auth.operation.get"
MAX_WAIT_SECONDS = 900
_WAIT_SLICE_SECONDS = 0.25


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@lru_cache(maxsize=None)
def _params_validator() -> Draft202012Validator:
    return Draft202012Validator(
        _plain(AUTH_OPERATION_GET_SPEC.params_schema), format_checker=FormatChecker()
    )


@lru_cache(maxsize=None)
def _result_validator() -> Draft202012Validator:
    return Draft202012Validator(
        _plain(AUTH_OPERATION_GET_SPEC.result_schema), format_checker=FormatChecker()
    )


def _refuse(validator: Draft202012Validator, value: Any, kind: str) -> None:
    """Refuse ``value`` against the declared schema exactly as the registry does.

    The parked wait answers without the registry, so it validates the same
    declaration itself; the message keeps only the declared path, never the value.
    """

    error = next(iter(sorted(validator.iter_errors(value), key=lambda e: list(map(str, e.absolute_path)))), None)
    if error is None:
        return
    path = "/".join(str(part) for part in error.absolute_path) or "<root>"
    code = ipc_errors.INVALID_ARGUMENT if kind == "params" else ipc_errors.DAEMON_ERROR
    raise DaemonRequestError(
        code, f"{WAITABLE_METHOD} {kind} do not match the declared schema at {path}"
    )


def _wire_refusal(method: str) -> DaemonRequestError:
    return DaemonRequestError(
        ACCOUNT_REQUIRED,
        "this daemon has no account yet; sign in first",
        {"reason": ACCOUNT_REQUIRED, "phase": "bootstrap", "method": method},
    )


def _unknown_method(method: str) -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
    )


def _is_served(name: str) -> bool:
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
    from hyprial.daemon.impl.request_catalog.migration import ROUTER_ONLY_METHODS

    return name in METHOD_REGISTRY or name in ROUTER_ONLY_METHODS


def _wire_ping(dispatcher: BootstrapDispatcher) -> Mapping[str, Any]:
    return {
        "running": True,
        "pid": os.getpid(),
        "socket": str(dispatcher.socket_path),
        "restorePending": False,
    }


@dataclass(frozen=True, slots=True)
class BootstrapPolicy:
    """What differs between the transports that share one dispatcher."""

    refuse: Callable[[str], DaemonRequestError]
    refuse_undeclared: Callable[[str], DaemonRequestError] | None
    exposed_state: str
    exposed_reason: str | None
    ping_extra: Callable[[BootstrapDispatcher], Mapping[str, Any]]


WIRE_POLICY = BootstrapPolicy(
    _wire_refusal, _unknown_method, "not-ready", ACCOUNT_REQUIRED, _wire_ping
)


class BootstrapDispatcher:
    """One admitted bootstrap runtime plus the answers for its methods."""

    def __init__(
        self,
        *,
        app: Any,
        runtime: BootstrapRuntime,
        context: CallContext,
        policy: BootstrapPolicy,
    ) -> None:
        self._app = app
        self._runtime = runtime
        self._context = context
        self._policy = policy
        self._lock = threading.RLock()
        self._closed = False
        self._promotion_enabled = False
        self._abandon: Callable[[], None] | None = None
        self._first_login: FirstLogin | None = None
        self._node_id = resolve_node_id()
        self.epoch = runtime.view.record.epoch

    @classmethod
    def admit(
        cls,
        *,
        app: Any,
        home: Path,
        state_dir: Path,
        socket_path: Path,
        policy: BootstrapPolicy,
        gateway_options: Mapping[str, Any] | None = None,
    ) -> BootstrapDispatcher:
        """Take both locks, publish ``bootstrap.json`` and mount the gateway.

        ``app`` is the object the method registry invokes: it must hold the one
        onboarding port (``install_onboarding``) and resolve the admitted frame
        (``onboarding_context``).  Any failure releases whatever was taken.
        """

        runtime = BootstrapRuntime.admit(
            home=home, state_dir=state_dir, socket_path=socket_path
        )
        try:
            surface = runtime.local_operator_surface()
            context = CallContext(
                surface=Surface(
                    home_binding=str(home),
                    host_control_session_id=surface.host_control_session_id,
                    view_session_id=surface.view_session_id,
                    transport_generation=surface.transport_generation,
                ),
                phase="bootstrap",
                caller="local-bootstrap",
            )
            gateway = BootstrapOnboardingGateway(
                runtime=runtime, **dict(gateway_options or {})
            )
            app.install_onboarding(gateway)
            dispatcher = cls(app=app, runtime=runtime, context=context, policy=policy)
            dispatcher.mount_first_login(gateway)
            return dispatcher
        except BaseException:
            runtime.interrupt()
            raise

    @property
    def runtime(self) -> BootstrapRuntime:
        return self._runtime

    @property
    def context(self) -> CallContext:
        return self._context

    @property
    def socket_path(self) -> Path:
        return self._runtime.socket_path

    @property
    def promotion_enabled(self) -> bool:
        return self._promotion_enabled

    @property
    def reopen_required(self) -> bool:
        return self._runtime.reopen_required

    @property
    def first_login(self) -> FirstLogin | None:
        return self._first_login

    def mount_first_login(self, gateway: BootstrapOnboardingGateway) -> None:
        """Give ``gateway`` the controller that drives a login until serving."""

        controller = FirstLogin(
            runtime=self._runtime,
            gateway=gateway,
            context=self._context,
            exclusive=self.exclusive,
            abandon=self.abandon_promotion,
        )
        gateway.attach_first_login(controller)
        self._first_login = controller

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Serialize a call with the post-reply claim it may arm."""

        with self._lock:
            yield

    def callable_methods(self) -> frozenset[str]:
        if self._promotion_enabled:
            return BOOTSTRAP_CALLABLE_METHODS | {INITIALIZE_METHOD}
        return BOOTSTRAP_CALLABLE_METHODS

    def ping(self) -> dict[str, Any]:
        return {
            **self._policy.ping_extra(self),
            "version": __version__,
            "nodeId": self._node_id,
            "owner": None,
            "epoch": self.epoch,
            "phase": "bootstrap",
        }

    def catalog(self) -> dict[str, Any]:
        from hyprial.daemon.impl.request_catalog import daemon_catalog

        callable_now = self.callable_methods()
        methods: list[dict[str, Any]] = []
        for entry in daemon_catalog()["methods"]:
            if entry["name"] in callable_now:
                state, reason = "available", None
            elif entry["httpExposed"]:
                state = self._policy.exposed_state
                reason = self._policy.exposed_reason
            else:
                state, reason = "disabled", None
            methods.append(
                {
                    **entry,
                    "declared": True,
                    "state": state,
                    "callable": entry["name"] in callable_now,
                    "reason": reason,
                }
            )
        return {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "packageVersion": __version__,
            "epoch": self.epoch,
            "methods": methods,
        }

    def call(self, method: str, params: Any = None) -> Any:
        """Answer one request: ping, catalog, a callable method or a refusal."""

        if self._closed:
            raise ipc_errors.DaemonUnavailableError("the bootstrap host is unavailable")
        if method == "ping":
            return self.ping()
        if method == "daemon.catalog":
            return self.catalog()
        if method not in BOOTSTRAP_CALLABLE_METHODS and (
            method != "onboarding.initialize" or not self._promotion_enabled
        ):
            if (
                self._policy.refuse_undeclared is not None
                and not _is_served(method)
            ):
                raise self._policy.refuse_undeclared(method)
            raise self._policy.refuse(method)
        from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

        with self._lock:
            with admitted_onboarding_context(self._context):
                return METHOD_REGISTRY.invoke(
                    self._app, method, params or {}, trusted_message_origin=None
                )

    def wait_seconds(self, method: str, params: Any) -> float:
        """The seconds a request asks to be parked for; 0 means answer at once.

        Only a valid ask parks.  Anything out of range is left to the registry,
        which refuses it with its declared ``INVALID_ARGUMENT``.
        """

        if method != WAITABLE_METHOD or self._first_login is None:
            return 0.0
        seconds = params.get("waitSeconds") if isinstance(params, Mapping) else None
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return 0.0
        return float(seconds) if 0 < seconds <= MAX_WAIT_SECONDS else 0.0

    def wait(
        self,
        method: str,
        params: Mapping[str, Any],
        seconds: float,
        *,
        leave: Callable[[], bool],
    ) -> Any:
        """Answer ``method`` after at most ``seconds``, parked WITHOUT the lock.

        Only a login this daemon drives parks; any other operation is answered
        at once.  The call returns when the run reached ``serving`` or
        ``stopped``, when ``leave()`` says the host is ending (the run is then
        ended ``stopped``), or when the seconds run out; in every case the answer
        is the run's last published view, so it never waits for the dispatcher
        lock or reads the journal.
        """

        controller = self._first_login
        run = None if controller is None else controller.run_for(params.get("operationId"))
        if run is None:
            with self.exclusive():
                return self.call(method, params)
        _refuse(_params_validator(), params, "params")

        def answered() -> Any:
            answer = run.answer()
            _refuse(_result_validator(), answer, "result")
            return answer

        deadline = time.monotonic() + seconds
        while True:
            seen = run.version
            if run.final:
                return answered()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return answered()
            if leave():
                run.stop()
                return answered()
            run.wait_change(seen, min(remaining, _WAIT_SLICE_SECONDS))

    def enable_promotion(
        self,
        wake_owner: Callable[[], None],
        abandon: Callable[[], None] | None = None,
    ) -> None:
        """Admit initialize only when a foreground owner can consume its claim.

        ``abandon`` is how the owner is told that an accepted initialize can
        never be acknowledged (the wire's ``initialize-unacknowledged``).
        """

        with self._lock:
            if self._promotion_enabled:
                raise RuntimeError("promotion requires an unarmed bootstrap host")
            self._runtime.register_quiesce(wake_owner)
            self._abandon = abandon
            self._promotion_enabled = True

    def abandon_promotion(self) -> None:
        """Tell the owner an accepted initialize will not be acknowledged."""

        abandon = self._abandon
        if abandon is not None:
            abandon()

    def take_post_reply(self) -> Callable[[], None] | None:
        """The one-shot action an accepted initialize armed, if any."""

        with self._lock:
            return self._runtime.take_post_reply()

    def close(self) -> None:
        """Mark unfinished work unknown and release the locks (idempotent)."""

        controller = self._first_login
        if controller is not None:
            controller.shutdown()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._runtime.interrupt()
