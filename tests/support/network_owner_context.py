"""Test-only tripwire for daemon owners entering network effect boundaries.

The profiler observes Python call frames, including frames created on actor and
IPC worker threads.  Production code has no dependency on this module.  Tests
install it around a scenario that can black-hole an effect; entering a marked
boundary while a marked owner remains on the stack fails immediately.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import sys
import threading
from types import CodeType, FrameType
from typing import Any


@dataclass(frozen=True, slots=True)
class FunctionPoint:
    """One owner or effect function identified without wrapping production code."""

    label: str
    target: str
    code: CodeType | None = None

    @classmethod
    def from_callable(cls, label: str, function: Callable[..., object]) -> "FunctionPoint":
        return cls(
            label=label,
            target=f"{function.__module__}:{function.__qualname__}",
            code=function.__code__,
        )


class OwnerNetworkViolation(AssertionError):
    """A scarce daemon owner entered a remote/external effect boundary."""


DEFAULT_OWNER_POINTS = (
    FunctionPoint(
        "ipc_request",
        "hyprial.daemon.impl.ipc.server:_IpcServerMixin._timed_handle",
    ),
    FunctionPoint(
        "orgfs_space_lock",
        "hyprial.daemon.impl.orgfs.document.space_state:FacadeSpaceState._execute_space_state",
    ),
    FunctionPoint(
        "inbox_writer",
        "hyprial.daemon.impl.inbox.actor.custody.lifecycle:DeliveryCustodyLifecycleMixin.__call__",
    ),
    FunctionPoint(
        "state_writer",
        "hyprial.daemon.impl.state_persistence.ports:_Generation.__call__",
    ),
)

DEFAULT_BOUNDARY_POINTS = (
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.put",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.get",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.query",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.reconfigure_connect",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.subscribe",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.declare_queryable",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.declare_query_handler",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.get_liveliness",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.declare_liveliness",
    ),
    FunctionPoint(
        "zenoh_transport",
        "hyprial.daemon.impl.transport.zenoh.transport:ZenohTransport.observe_liveliness",
    ),
    FunctionPoint(
        "session_remote",
        "hyprial.daemon.impl.transport.session_actor:TransportSessionAuthority._call",
    ),
    FunctionPoint(
        "sidecar_wire",
        "hyprial.daemon.impl.forwarding.controller:ForwardingSidecarController._send",
    ),
    FunctionPoint(
        "sidecar_wire",
        "hyprial.daemon.impl.forwarding.controller:ForwardingSidecarController._request",
    ),
    FunctionPoint(
        "workflow_remote",
        "hyprial.daemon.impl.dispatch.remote.wire:RemoteWire.call",
    ),
    FunctionPoint(
        "casdoor_api",
        "hyprial.daemon.impl.org.network.casdoor.client:HttpCasdoorGroups._bearer",
    ),
    FunctionPoint(
        "casdoor_api",
        "hyprial.daemon.impl.org.network.casdoor.client:HttpCasdoorGroups._call",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.bot_info",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.list_chats_page",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.list_chat_members_page",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.add_reaction",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.send_owner_dm",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.send_chat",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.send_chat_file",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.send_chat_image",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.clear_reaction",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.reply",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.get_message",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.get_inbound_message",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.download_message_resource",
    ),
    FunctionPoint(
        "lark_api",
        "hyprial.daemon.impl.adapters.lark.outbound.sdk:LarkSdkGateway.list_chat_messages",
    ),
)


def _resolve(point: FunctionPoint) -> CodeType:
    if point.code is not None:
        return point.code
    module_name, separator, qualname = point.target.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(f"invalid function point: {point.target!r}")
    value: Any = importlib.import_module(module_name)
    for component in qualname.split("."):
        value = getattr(value, component)
    code = getattr(value, "__code__", None)
    if not isinstance(code, CodeType):
        raise TypeError(f"function point has no Python code: {point.target}")
    return code


class OwnerContextTracer:
    """Fail when a boundary call frame has an owner call frame as an ancestor."""

    def __init__(
        self,
        *,
        owners: tuple[FunctionPoint, ...] = DEFAULT_OWNER_POINTS,
        boundaries: tuple[FunctionPoint, ...] = DEFAULT_BOUNDARY_POINTS,
    ) -> None:
        self._owners = {_resolve(point): point for point in owners}
        self._boundaries = {_resolve(point): point for point in boundaries}

    def _profile(self, frame: FrameType, event: str, _arg: object) -> None:
        if event != "call":
            return
        boundary = self._boundaries.get(frame.f_code)
        if boundary is None:
            return
        caller = frame.f_back
        while caller is not None:
            owner = self._owners.get(caller.f_code)
            if owner is not None:
                raise OwnerNetworkViolation(
                    f"daemon owner {owner.label} ({owner.target}) entered "
                    f"network boundary {boundary.label} ({boundary.target})"
                )
            caller = caller.f_back

    @contextmanager
    def installed(self) -> Iterator["OwnerContextTracer"]:
        """Install on this thread and threads created during the context."""

        previous_current = sys.getprofile()
        previous_threads = threading.getprofile()

        def profile(frame: FrameType, event: str, arg: object) -> None:
            if previous_current is not None:
                previous_current(frame, event, arg)
            self._profile(frame, event, arg)

        def thread_profile(frame: FrameType, event: str, arg: object) -> None:
            if previous_threads is not None:
                previous_threads(frame, event, arg)
            self._profile(frame, event, arg)

        set_all_threads = getattr(threading, "setprofile_all_threads", None)
        if set_all_threads is None:
            threading.setprofile(thread_profile)
        else:
            set_all_threads(thread_profile)
        sys.setprofile(profile)
        try:
            yield self
        finally:
            if set_all_threads is None:
                threading.setprofile(previous_threads)
            else:
                set_all_threads(previous_threads)
            sys.setprofile(previous_current)
