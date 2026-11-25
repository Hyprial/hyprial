"""Phase-aware HTTPX effect translation without provider retry policy."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeAlias

import httpx

from .types import (
    HttpAccepted,
    HttpEffectObservation,
    HttpEffectState,
    HttpRequestPhase,
)


_LOCAL_FINAL = (
    httpx.CookieConflict,
    httpx.InvalidURL,
    httpx.RequestNotRead,
    httpx.ResponseNotRead,
    httpx.StreamClosed,
    httpx.StreamConsumed,
    httpx.UnsupportedProtocol,
)
_DOCUMENTED = (
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.CookieConflict,
    httpx.StreamError,
)


class HttpEffectAttempt:
    """Mutable phase marker owned by one in-flight HTTP effect."""

    __slots__ = ("_phase",)

    def __init__(self) -> None:
        self._phase = HttpRequestPhase.PRE_SEND

    @property
    def phase(self) -> HttpRequestPhase:
        return self._phase

    def mark_may_have_sent(self) -> None:
        if self._phase is HttpRequestPhase.RESPONSE_RECEIVED:
            raise RuntimeError("HTTP effect phase cannot regress")
        self._phase = HttpRequestPhase.MAY_HAVE_SENT

    def mark_response_received(self) -> None:
        if self._phase is HttpRequestPhase.PRE_SEND:
            raise RuntimeError("response cannot precede request admission")
        self._phase = HttpRequestPhase.RESPONSE_RECEIVED


HttpOperation: TypeAlias = Callable[[HttpEffectAttempt], Awaitable[httpx.Response]]
SuccessParser: TypeAlias = Callable[[httpx.Response], HttpAccepted]
Settlement: TypeAlias = Callable[[HttpEffectObservation], Awaitable[None]]


def classify_httpx_exception(
    error: Exception, phase: HttpRequestPhase
) -> HttpEffectObservation:
    """Classify every documented HTTPX exception at an explicit effect phase."""

    if not isinstance(phase, HttpRequestPhase):
        raise TypeError("phase must be HttpRequestPhase")
    if not isinstance(error, _DOCUMENTED):
        raise TypeError(f"unsupported HTTPX exception: {type(error).__name__}")
    name = type(error).__name__
    if phase is not HttpRequestPhase.PRE_SEND:
        state = HttpEffectState.UNKNOWN
        definitely_not_sent = False
        reason = "http-effect-unknown"
    elif isinstance(error, _LOCAL_FINAL):
        return HttpEffectObservation(
            phase,
            HttpEffectState.REJECTED,
            True,
            None,
            None,
            name,
            "http-local-final",
        )
    else:
        state = HttpEffectState.RETRYABLE
        definitely_not_sent = True
        reason = "http-pre-send-failure"
    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    return HttpEffectObservation(
        phase,
        state,
        definitely_not_sent,
        status,
        None,
        name,
        reason,
    )


async def _settle(
    settle: Settlement | None, observation: HttpEffectObservation
) -> None:
    if settle is None:
        return
    task = asyncio.ensure_future(settle(observation))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
    await task  # Already done: propagate its outcome without a blocking Future API.
    if cancellation is not None:
        raise cancellation


def _response_observation(
    response: httpx.Response,
    *,
    effecting: bool,
    parse_success: SuccessParser,
) -> HttpEffectObservation:
    status = response.status_code
    if 200 <= status < 300:
        try:
            accepted = parse_success(response)
            if not isinstance(accepted, HttpAccepted):
                raise TypeError("success parser must return HttpAccepted")
        except (TypeError, ValueError, KeyError, httpx.DecodingError):
            return HttpEffectObservation(
                HttpRequestPhase.RESPONSE_RECEIVED,
                HttpEffectState.UNKNOWN,
                False,
                status,
                None,
                None,
                "http-success-malformed",
            )
        except _DOCUMENTED as error:
            return HttpEffectObservation(
                HttpRequestPhase.RESPONSE_RECEIVED,
                HttpEffectState.UNKNOWN,
                False,
                status,
                None,
                type(error).__name__,
                "http-effect-unknown",
            )
        return HttpEffectObservation(
            HttpRequestPhase.RESPONSE_RECEIVED,
            HttpEffectState.ACCEPTED,
            False,
            status,
            accepted.native_id,
            None,
            "http-effect-accepted",
        )
    if 500 <= status < 600 and effecting:
        return HttpEffectObservation(
            HttpRequestPhase.RESPONSE_RECEIVED,
            HttpEffectState.UNKNOWN,
            False,
            status,
            None,
            None,
            "http-server-effect-unknown",
        )
    if 500 <= status < 600:
        state = HttpEffectState.RETRYABLE
        reason = "http-server-retryable"
    else:
        state = HttpEffectState.REJECTED
        reason = "http-response-rejected"
    return HttpEffectObservation(
        HttpRequestPhase.RESPONSE_RECEIVED,
        state,
        True,
        status,
        None,
        None,
        reason,
    )


async def execute_http_effect(
    operation: HttpOperation,
    *,
    effecting: bool,
    parse_success: SuccessParser,
    settle: Settlement | None = None,
) -> HttpEffectObservation:
    """Execute one HTTP effect and settle cancellation uncertainty before re-raise."""

    if not isinstance(effecting, bool):
        raise TypeError("effecting must be bool")
    attempt = HttpEffectAttempt()
    try:
        response = await operation(attempt)
    except asyncio.CancelledError:
        observation = HttpEffectObservation(
            attempt.phase,
            (
                HttpEffectState.RETRYABLE
                if attempt.phase is HttpRequestPhase.PRE_SEND
                else HttpEffectState.UNKNOWN
            ),
            attempt.phase is HttpRequestPhase.PRE_SEND,
            None,
            None,
            "CancelledError",
            "http-effect-cancelled",
        )
        await _settle(settle, observation)
        raise
    except Exception as error:
        if not isinstance(error, _DOCUMENTED):
            raise
        observation = classify_httpx_exception(error, attempt.phase)
        await _settle(settle, observation)
        return observation
    if attempt.phase is not HttpRequestPhase.RESPONSE_RECEIVED:
        attempt.mark_response_received()
    observation = _response_observation(
        response, effecting=effecting, parse_success=parse_success
    )
    await _settle(settle, observation)
    return observation


__all__ = [
    "HttpEffectAttempt",
    "classify_httpx_exception",
    "execute_http_effect",
]
