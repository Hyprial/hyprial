"""Generation-fenced socket close and once-only terminal resource finalization."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from .types import ResourceCloseOutcome, ResourceCloseState


@dataclass(slots=True)
class _SocketBinding:
    resource: Any
    generation: int
    owned: bool


async def _call_close(resource: Any, *args: Any, **kwargs: Any) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        close = getattr(resource, "aclose", None)
        args = ()
        kwargs = {}
    if not callable(close):
        raise TypeError("owned resource has no async close method")
    result = close(*args, **kwargs)
    if inspect.isawaitable(result):
        await result


class TransportResourceOwner:
    """Own one HTTP client and successive socket generations."""

    def __init__(self, client: Any, *, owns_client: bool) -> None:
        if not isinstance(owns_client, bool):
            raise TypeError("owns_client must be bool")
        self._client = client
        self._owns_client = owns_client
        self._socket: _SocketBinding | None = None
        self._pending_socket_closes: set[asyncio.Task[None]] = set()
        self._finalizer: asyncio.Task[ResourceCloseOutcome] | None = None

    def bind_socket(self, socket: Any, *, generation: int, owns_socket: bool) -> None:
        if self._finalizer is not None:
            raise RuntimeError("transport owner is finalizing")
        if self._socket is not None:
            raise RuntimeError("prior socket generation is still bound")
        if generation < 1:
            raise ValueError("generation must be positive")
        if not isinstance(owns_socket, bool):
            raise TypeError("owns_socket must be bool")
        self._socket = _SocketBinding(socket, generation, owns_socket)

    def _track_socket_close(self, task: asyncio.Task[None]) -> None:
        self._pending_socket_closes.add(task)
        task.add_done_callback(self._pending_socket_closes.discard)

    @staticmethod
    def _remaining(timeout_at: float) -> float:
        return max(0.0, timeout_at - asyncio.get_running_loop().time())

    async def close_socket(
        self,
        *,
        generation: int,
        code: int | None,
        reason: str | None,
        timeout_at: float,
    ) -> ResourceCloseOutcome:
        binding = self._socket
        if binding is None or binding.generation != generation:
            return ResourceCloseOutcome(
                ResourceCloseState.STALE_GENERATION,
                generation,
                False,
                False,
                "transport-socket-generation-stale",
            )
        self._socket = None
        if not binding.owned:
            return ResourceCloseOutcome(
                ResourceCloseState.CLOSED, generation, False, False, None
            )
        task = asyncio.create_task(_call_close(binding.resource, code, reason))
        self._track_socket_close(task)
        try:
            async with asyncio.timeout(self._remaining(timeout_at)):
                await asyncio.shield(task)
        except TimeoutError:
            return ResourceCloseOutcome(
                ResourceCloseState.UNKNOWN,
                generation,
                False,
                False,
                "transport-close-deadline",
            )
        return ResourceCloseOutcome(ResourceCloseState.CLOSED, generation, True, False, None)

    async def _finalize(self, timeout_at: float) -> ResourceCloseOutcome:
        binding = self._socket
        self._socket = None
        socket_generation = binding.generation if binding is not None else None
        socket_owned = binding is not None and binding.owned
        tasks = set(self._pending_socket_closes)
        socket_task: asyncio.Task[None] | None = None
        client_task: asyncio.Task[None] | None = None
        if socket_owned:
            socket_task = asyncio.create_task(_call_close(binding.resource, None, None))
            self._track_socket_close(socket_task)
            tasks.add(socket_task)
        if self._owns_client:
            client_task = asyncio.create_task(_call_close(self._client))
            tasks.add(client_task)
        if not tasks:
            return ResourceCloseOutcome(
                ResourceCloseState.CLOSED, socket_generation, False, False, None
            )
        joined = asyncio.gather(*tasks, return_exceptions=True)
        try:
            async with asyncio.timeout(self._remaining(timeout_at)):
                results = await asyncio.shield(joined)
        except TimeoutError:
            return ResourceCloseOutcome(
                ResourceCloseState.UNKNOWN,
                socket_generation,
                socket_task is not None and socket_task.done() and not socket_task.cancelled(),
                client_task is not None and client_task.done() and not client_task.cancelled(),
                "transport-close-deadline",
            )
        if any(isinstance(result, BaseException) for result in results):
            return ResourceCloseOutcome(
                ResourceCloseState.UNKNOWN,
                socket_generation,
                socket_task is not None and socket_task.done() and socket_task.exception() is None,
                client_task is not None and client_task.done() and client_task.exception() is None,
                "transport-close-error",
            )
        return ResourceCloseOutcome(
            ResourceCloseState.CLOSED,
            socket_generation,
            socket_task is not None,
            client_task is not None,
            None,
        )

    async def aclose(self, timeout_at: float) -> ResourceCloseOutcome:
        """Join one shielded finalizer; caller cancellation doesn't cancel ownership."""

        if self._finalizer is None:
            self._finalizer = asyncio.create_task(self._finalize(timeout_at))
        return await asyncio.shield(self._finalizer)


__all__ = ["TransportResourceOwner"]
