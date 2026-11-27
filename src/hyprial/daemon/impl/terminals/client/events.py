"""Bounded JSON-RPC event subscriptions for a private hc core."""

from __future__ import annotations

import json
from collections.abc import Mapping
import threading
from typing import Any, TYPE_CHECKING

from .protocol import (
    HcHeartbeatTimeout,
    HcStreamClosed,
    HcStreamEvent,
    HcTransportError,
    parse_snapshot,
)
from .rpc import _read_line

if TYPE_CHECKING:
    from .rpc import HcClient

_EVENT_TYPES = frozenset(
    {
        "created",
        "foreground",
        "status",
        "title",
        "turn",
        "labels",
        "size",
        "keep",
        "lost",
        "restarted",
        "finished",
        "removed",
    }
)


class HcEventSubscription:
    """Read an hc event stream with a finite deadline for every line."""

    def __init__(
        self,
        client: HcClient,
        *,
        labels: Mapping[str, str] | None = None,
        read_timeout: float = 45.0,
    ) -> None:
        if read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        if labels is not None and any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in labels.items()
        ):
            raise ValueError("label values must be strings")
        params = {} if labels is None else {"labels": dict(labels)}
        self._stream, self.snapshot = client._open_subscription(params)
        self.read_timeout = read_timeout
        self.last_seq = self.snapshot.seq
        self._closed = False
        self._buffer = bytearray()
        self._close_lock = threading.Lock()

    def next_event(self) -> HcStreamEvent:
        while True:
            frame = self._read_frame()
            method = frame.get("method")
            params = frame.get("params")
            if not isinstance(method, str) or not isinstance(params, Mapping):
                raise HcTransportError("hc event frame is malformed")
            if method == "heartbeat":
                return HcStreamEvent(
                    seq=self.last_seq,
                    type="heartbeat",
                    terminal=None,
                    data=dict(params),
                )
            if method == "missed":
                snapshot = parse_snapshot(params)
                self.snapshot = snapshot
                self.last_seq = snapshot.seq
                return HcStreamEvent(
                    seq=snapshot.seq,
                    type="missed",
                    terminal=None,
                    data={"terminals": snapshot.terminals},
                    snapshot=snapshot,
                )
            if method != "event":
                continue
            event_type = params.get("type")
            sequence = params.get("seq")
            if not isinstance(event_type, str) or not isinstance(sequence, int):
                raise HcTransportError("hc event notification is malformed")
            if sequence <= self.last_seq or event_type not in _EVENT_TYPES:
                continue
            self.last_seq = sequence
            data = params.get("data")
            event_data = dict(data) if isinstance(data, Mapping) else {}
            terminal = params.get("terminal")
            return HcStreamEvent(
                seq=sequence,
                type=event_type,
                terminal=terminal if isinstance(terminal, str) else None,
                data=event_data,
            )

    def __iter__(self) -> HcEventSubscription:
        return self

    def __next__(self) -> HcStreamEvent:
        return self.next_event()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stream.close()

    def _read_frame(self) -> Mapping[str, Any]:
        with self._close_lock:
            if self._closed:
                raise HcStreamClosed("hc event subscription is closed")
        try:
            line = _read_line(self._stream, self.read_timeout, self._buffer)
        except HcTransportError as error:
            if self._closed:
                raise HcStreamClosed("hc event subscription is closed") from error
            if _is_timeout(error):
                raise HcHeartbeatTimeout(
                    f"no hc event or heartbeat within {self.read_timeout:.3f}s"
                ) from error
            raise HcStreamClosed("hc event stream ended") from error
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise HcTransportError("hc returned invalid event JSON") from error
        if not isinstance(value, Mapping):
            raise HcTransportError("hc returned a non-object event frame")
        return value


def _is_timeout(error: HcTransportError) -> bool:
    cause = error.__cause__
    return isinstance(cause, (TimeoutError,)) or "timed out" in str(error).lower()
