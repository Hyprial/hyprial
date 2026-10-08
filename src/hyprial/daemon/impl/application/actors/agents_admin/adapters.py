"""Adapter administration IPC family: start/stop/pin/list/status/reload."""

from __future__ import annotations

from __future__ import annotations
from typing import Any, TYPE_CHECKING
from hyprial.daemon.impl.adapters.lark.contracts import lifecycle as lark_lifecycle
from hyprial.identity import (
    Agent,
    PinConflictError,
)
from hyprial.daemon.impl.adapters.lark.contracts.errors import (
    AdapterStartError,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
from hyprial.kernel import (
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    _required_string,
)


class _AdapterAdminMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

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
        try:
            self._reload_user_adapters()
        except (OSError, ValueError) as error:
            raise DaemonRequestError(
                ipc_errors.ADAPTER_RELOAD_FAILED,
                f"gateway configs reloaded ({summary}) but receiver-owned "
                f"adapters failed to reload: {error}",
                {"reloaded": summary},
            ) from error
        return {"ok": True, **summary}

    def _ipc_adapter_list(self) -> Any:
        assert self._lark_client is not None
        return {
            "ok": True,
            "adapters": [
                item.to_payload() for item in self._lark_client.read_adapters()
            ],
        }

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

    def _ipc_adapter_pin(self, params) -> Any:
        assert self._adapters is not None
        name = self._require_adapter(params.get("name"))
        actor = _required_string(params.get("actor"), "actor")
        # The pin target must be an existing agent on THIS machine; the
        # value stored is always the canonical four-segment URI, so a
        # bare-name pin can never again drift apart from the URI its
        # connector registers (the bare-name/canonical-pin incident).
        agent = self._require_pinnable_agent(actor)
        # The one-to-one rule is the pins table's UNIQUE constraints;
        # this handler only translates the typed violation.
        staged_legacy = dict(self.desired_state.load().channel_pins).get(name)
        try:
            previous = self.agents.pin(name, agent.actor)
        except PinConflictError as error:
            raise DaemonRequestError(
                error.code,
                str(error),
                {
                    "actor": agent.uri,
                    "adapter": name,
                    "pinnedBy": error.holder,
                },
            ) from error
        # A staged legacy entry for this adapter (an unmigrated
        # ``channelPins`` value) is superseded by this explicit write and
        # must not resurrect through the next startup migration.
        _state, legacy = self.desired_state.remove_channel_pin(name)
        legacy = staged_legacy if legacy is None else legacy
        if previous is None:
            previous = legacy
        return {
            "ok": True,
            "adapter": name,
            "actor": agent.uri,
            "previous": previous,
            "changed": previous != agent.uri,
            "pins": self._adapter_pins(),
        }

    def _ipc_adapter_unpin(self, params) -> Any:
        assert self._adapters is not None
        name = self._require_adapter(params.get("name"))
        staged_legacy = dict(self.desired_state.load().channel_pins).get(name)
        previous = self.agents.unpin(name)
        # Also drop any unmigrated legacy staging entry, for the same
        # no-resurrection reason as adapter.pin.
        _state, legacy = self.desired_state.remove_channel_pin(name)
        legacy = staged_legacy if legacy is None else legacy
        if previous is None:
            previous = legacy
        return {
            "ok": True,
            "adapter": name,
            "previous": previous,
            "changed": previous is not None,
            "pins": self._adapter_pins(),
        }

    def _ipc_adapter_pins(self) -> Any:
        assert self._adapters is not None
        return {
            "ok": True,
            "pins": self._adapter_pins(),
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

    def _adapter_pins(self) -> dict[str, str]:
        """Adapter -> agent-URI pins, one query against the pins table.

        Deliberately unfiltered: every pin that exists is visible here, so an
        entry left behind by a removed adapter can be seen and cleaned instead
        of lingering invisibly (the old two-view filter hid exactly those).
        """

        return self.agents.pins()

    def _require_pinnable_agent(self, actor: str) -> Agent:
        """Resolve a pin target to an existing agent on this machine, loudly.

        A pin is the decision "every DM this adapter receives goes to that
        agent" -- aiming it at a name nobody registered would send messages
        into a queue nobody can ever drain, so a nonexistent target is an
        error, not a deferred binding.  Creating the agent must come first
        (``hyprial agent create`` / ``hyprial start``), then the pin.
        """

        agent = self.agents.get(actor)
        if agent is not None:
            return agent
        parsed_actor = parse_agent_uri(actor)
        if parsed_actor is not None:
            if (
                parsed_actor[0] != self.owner
                or parsed_actor[1] != self.node_id
            ):
                raise DaemonRequestError(
                    ipc_errors.AGENT_NOT_FOUND,
                    f"cannot pin {actor!r}: that URI names an agent of owner "
                    f"{parsed_actor[0]!r} on machine {parsed_actor[1]!r}, and a pin can only "
                    f"bind an agent registered on this machine "
                    f"({self.owner}@{self.node_id}).",
                    {"actor": actor},
                )
        raise DaemonRequestError(
            ipc_errors.AGENT_NOT_FOUND,
            f"no agent named {actor!r} on this machine. A pin binds an "
            f"adapter to an existing agent: create it first with "
            f"'hyprial agent create --name {actor}' (or 'hyprial start'), then pin.",
            {"actor": actor},
        )
