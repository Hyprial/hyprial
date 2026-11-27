"""Typed values and error translation for the local hc protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CoreIdentity:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class HelloResult:
    generation: int
    version: str
    core: CoreIdentity
    framed: bool
    methods: tuple[str, ...]
    features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HolderInfo:
    pid: int
    instance: str
    current: bool
    terminals: int


@dataclass(frozen=True, slots=True)
class CoreStatus:
    core: CoreIdentity
    version: str
    pid: int | None
    uptime_ms: int
    holder: HolderInfo | None
    holders: tuple[HolderInfo, ...]
    live: int
    finished: int
    installed_by: str | None
    generation: int | None = None


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    state: str
    kind: str | None = None
    app: str | None = None
    msg: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalRecord:
    address: str
    name: str | None
    labels: Mapping[str, str]
    argv: tuple[str, ...]
    cwd: str
    state: str
    pid: int | None
    exit_code: int | None
    signal: int | None
    reason: str | None
    status: TerminalStatus | None
    status_ms: int | None
    created_ms: int
    finished_ms: int | None
    cols: int
    rows: int


@dataclass(frozen=True, slots=True)
class SubscribeSnapshot:
    seq: int
    channel: str | None
    terminals: tuple[TerminalRecord, ...]


@dataclass(frozen=True, slots=True)
class HcStreamEvent:
    seq: int
    type: str
    terminal: str | None
    data: Mapping[str, Any]
    snapshot: SubscribeSnapshot | None = None


class HcTransportError(Exception):
    """A local IPC failure while exchanging a protocol frame."""


class HcStreamClosed(HcTransportError):
    """The hc event stream ended without a protocol error frame."""


class HcHeartbeatTimeout(HcTransportError):
    """No event or heartbeat arrived before the bounded read deadline."""


class HcRpcError(HcTransportError):
    """An error response returned by the hc JSON-RPC server."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        kind: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind
        self.data = dict(data or {})

    @classmethod
    def from_response(cls, response: Mapping[str, Any]) -> HcRpcError:
        error = response.get("error")
        if not isinstance(error, Mapping):
            raise cls(-32603, "invalid RPC error response", kind="internal")
        data = error.get("data")
        error_data = dict(data) if isinstance(data, Mapping) else {}
        kind = error_data.get("kind")
        if not isinstance(kind, str):
            kind = _kind_from_code(error.get("code"))
        code = error.get("code", -32603)
        if not isinstance(code, int):
            code = -32603
        message = error.get("message", "hc RPC request failed")
        if not isinstance(message, str):
            message = "hc RPC request failed"
        return cls(code, message, kind=kind, data=error_data)


def parse_hello(value: Mapping[str, Any]) -> HelloResult:
    return HelloResult(
        generation=int(value["generation"]),
        version=str(value["version"]),
        core=parse_core_identity(value["core"]),
        framed=bool(value["framed"]),
        methods=tuple(str(item) for item in value.get("methods", ())),
        features=tuple(str(item) for item in value.get("features", ())),
    )


def parse_core_status(value: Mapping[str, Any]) -> CoreStatus:
    raw_holders = value.get("holders", ())
    holders = tuple(parse_holder(item) for item in raw_holders if isinstance(item, Mapping))
    generation = value.get("generation")
    return CoreStatus(
        core=parse_core_identity(value["core"]),
        version=str(value["version"]),
        pid=_optional_int(value.get("pid")),
        uptime_ms=int(value.get("uptime_ms", 0)),
        holder=parse_holder(value["holder"])
        if isinstance(value.get("holder"), Mapping)
        else None,
        holders=holders,
        live=int(value.get("live", 0)),
        finished=int(value.get("finished", 0)),
        installed_by=value.get("installed_by") if isinstance(value.get("installed_by"), str) else None,
        generation=int(generation) if isinstance(generation, int) else None,
    )


def parse_terminal(value: Mapping[str, Any]) -> TerminalRecord:
    raw_status = value.get("status")
    return TerminalRecord(
        address=str(value["address"]),
        name=value.get("name") if isinstance(value.get("name"), str) else None,
        labels={str(key): str(item) for key, item in dict(value.get("labels", {})).items()},
        argv=tuple(str(item) for item in value.get("argv", ())),
        cwd=str(value.get("cwd", "")),
        state=str(value["state"]),
        pid=_optional_int(value.get("pid")),
        exit_code=_optional_int(value.get("exit_code")),
        signal=_optional_int(value.get("signal")),
        reason=value.get("reason") if isinstance(value.get("reason"), str) else None,
        status=parse_terminal_status(raw_status) if isinstance(raw_status, Mapping) else None,
        status_ms=_optional_int(value.get("status_ms")),
        created_ms=int(value.get("created_ms", 0)),
        finished_ms=_optional_int(value.get("finished_ms")),
        cols=int(value.get("cols", 120)),
        rows=int(value.get("rows", 40)),
    )


def parse_snapshot(value: Mapping[str, Any]) -> SubscribeSnapshot:
    return SubscribeSnapshot(
        seq=int(value.get("seq", 0)),
        channel=value.get("channel") if isinstance(value.get("channel"), str) else None,
        terminals=tuple(
            parse_terminal(item)
            for item in value.get("terminals", ())
            if isinstance(item, Mapping)
        ),
    )


def parse_core_identity(value: Any) -> CoreIdentity:
    if not isinstance(value, Mapping):
        raise ValueError("core identity must be an object")
    return CoreIdentity(id=str(value["id"]), name=str(value["name"]))


def parse_holder(value: Mapping[str, Any]) -> HolderInfo:
    return HolderInfo(
        pid=int(value["pid"]),
        instance=str(value["instance"]),
        current=bool(value["current"]),
        terminals=int(value.get("terminals", 0)),
    )


def parse_terminal_status(value: Mapping[str, Any]) -> TerminalStatus:
    return TerminalStatus(
        state=str(value["state"]),
        kind=value.get("kind") if isinstance(value.get("kind"), str) else None,
        app=value.get("app") if isinstance(value.get("app"), str) else None,
        msg=value.get("msg") if isinstance(value.get("msg"), str) else None,
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _kind_from_code(code: Any) -> str:
    if code in (-32602,):
        return "invalid_params"
    if code in (-32603, -32000):
        return "internal"
    return "internal"
