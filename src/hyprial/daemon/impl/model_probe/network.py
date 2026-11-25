"""A bounded, non-retrying model probe. Response bodies and exceptions never leave this module.

The total asyncio deadline includes reading the response, not only socket inactivity.
Redirects, compression and ambient proxy credentials are refused. Transport injection
is solely for isolated tests; production always constructs the standard HTTP client.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping

import httpx
from hyprial.kernel import current_request_cancellation

from .request import prepare

DEADLINE_SECONDS = 10.0
MAX_RESPONSE_BYTES = 65536


def _model_response(document: object, protocol: str) -> bool:
    if not isinstance(document, dict) or "error" in document or not isinstance(document.get("model"), str) or not document["model"].strip():
        return False
    if protocol == "anthropic-messages":
        content = document.get("content")
        return (document.get("type") == "message" and document.get("role") == "assistant"
                and isinstance(content, list) and any(
                    isinstance(row, dict) and row.get("type") == "text"
                    and isinstance(row.get("text"), str) and bool(row["text"].strip()) for row in content))
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    return (isinstance(message, dict) and message.get("role") == "assistant"
            and isinstance(message.get("content"), str) and bool(message["content"].strip()))


def _http_outcome(status: int) -> str | None:
    if status in (401, 403):
        return "authentication-rejected"
    if status == 429:
        return "rate-limited"
    if 300 <= status < 400:
        return "redirect-refused"
    if 400 <= status < 500:
        return "request-rejected"
    if status >= 500:
        return "upstream-error"
    return None if status == 200 else "invalid-response"


async def probe(params: Mapping[str, object], *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, object]:
    request = prepare(params)
    deadline = params.get("timeoutSeconds", DEADLINE_SECONDS)
    started = time.monotonic()
    status = None
    outcome = "invalid-response"
    cancellation = current_request_cancellation()
    unsubscribe = None
    if cancellation is not None:
        if cancellation.cancelled:
            raise asyncio.CancelledError
        loop, task = asyncio.get_running_loop(), asyncio.current_task()

        def cancel() -> None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

        unsubscribe = cancellation.subscribe(cancel)
    try:
        async with asyncio.timeout(deadline):
            async with httpx.AsyncClient(timeout=deadline, follow_redirects=False,
                                         trust_env=False, transport=transport) as client:
                async with client.stream("POST", request.url, headers=request.headers, json=request.body) as response:
                    status = response.status_code
                    failure = _http_outcome(status)
                    if failure:
                        outcome = failure
                    elif response.headers.get("content-encoding", "identity").lower() == "identity":
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                break
                            content.extend(chunk)
                        else:
                            document = json.loads(content)
                            if _model_response(document, request.protocol):
                                outcome = "passed"
    except (TimeoutError, httpx.TimeoutException):
        outcome = "timeout"
    except httpx.HTTPError:
        outcome = "unreachable"
    except (ValueError, UnicodeError, RecursionError):
        outcome = "invalid-response"
    finally:
        if unsubscribe is not None:
            unsubscribe()
    return {"ok": True, "outcome": outcome, "httpStatus": status,
            "latencyMs": max(0, round((time.monotonic() - started) * 1000))}
