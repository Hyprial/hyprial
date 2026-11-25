"""Credential probes require a bounded model response, not an HTTP success page."""

import asyncio
import importlib
import json

import httpx
import pytest

KEY = "synthetic-probe-secret"
TEXT = "private-provider-output"
PARAMS = {"provider": "deepseek", "secret": KEY, "model": "test-model"}


def api():
    return importlib.import_module("hyprial.daemon.impl.model_probe")


def completion(protocol="openai-completions"):
    if protocol == "anthropic-messages":
        return {"type": "message", "role": "assistant", "model": "test-model",
                "content": [{"type": "text", "text": TEXT}]}
    return {"model": "test-model", "choices": [{"message": {"role": "assistant", "content": TEXT}}]}


def run(response, params=None):
    requests = []

    def transport(request):
        requests.append(request)
        return response

    result = asyncio.run(api().probe(params or PARAMS, transport=httpx.MockTransport(transport)))
    assert len(requests) == 1
    assert KEY not in json.dumps(result)
    assert TEXT not in json.dumps(result)
    return result, requests[0]


@pytest.mark.parametrize("protocol,path,header", [
    ("openai-completions", "/v1/chat/completions", "authorization"),
    ("anthropic-messages", "/v1/messages", "x-api-key"),
])
def test_one_model_request_without_persisting_or_returning_content(protocol, path, header, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    params = dict(PARAMS, endpoint="https://example.invalid/v1", protocol=protocol)
    result, request = run(httpx.Response(200, json=completion(protocol)), params)
    assert result["outcome"] == "passed"
    assert result["httpStatus"] == 200
    assert result["latencyMs"] >= 0
    assert request.url.path == path
    assert KEY in request.headers[header]
    body = json.loads(request.content)
    assert body["model"] == params["model"]
    assert body["stream"] is False
    assert body["max_tokens"] == 64
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("body", [{}, {"ok": True}, {"error": KEY}, {"choices": []},
    {"model": "m", "choices": [{"message": {"role": "assistant", "content": " "}}]},
    {"model": "m", "choices": [{"message": {"role": "user", "content": "OK"}}]},
])
def test_http_200_without_model_text_is_not_pass(body):
    assert run(httpx.Response(200, json=body))[0]["outcome"] == "invalid-response"


@pytest.mark.parametrize("status,outcome", [(301, "redirect-refused"), (307, "redirect-refused"),
    (401, "authentication-rejected"), (403, "authentication-rejected"), (429, "rate-limited"),
    (400, "request-rejected"), (503, "upstream-error")])
def test_failure_is_redacted_and_never_retried_or_redirected(status, outcome):
    result, _ = run(httpx.Response(status, text=KEY, headers={"location": "https://elsewhere.invalid"}))
    assert result["outcome"] == outcome
    assert result["httpStatus"] == status


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>OK</html>"),
    httpx.Response(200, content=b"x" * 65537), httpx.Response(200, content=b"\xff")])
def test_invalid_or_oversized_response_is_not_pass(response):
    assert run(response)[0]["outcome"] == "invalid-response"


@pytest.mark.parametrize("endpoint", ["http://example.com", "file:///tmp/key", "https://user:pass@example.com",
    "https://example.com?key=secret", "https://example.com#fragment", "https://example.com/ bad",
    "https://example.com:99999", "https://example.com\\evil", "https://example.com/" + "x" * 2048])
def test_bad_endpoint_never_dispatches(endpoint):
    def forbidden(request):
        pytest.fail("invalid input dispatched")
    with pytest.raises(ValueError, match="Invalid credential probe input"):
        asyncio.run(api().probe(dict(PARAMS, endpoint=endpoint, protocol="openai-completions"),
                                transport=httpx.MockTransport(forbidden)))


@pytest.mark.parametrize("change", [{"secret": ""}, {"secret": KEY + "\n"}, {"secret": "x" * 16385},
    {"secret": "\ud800"}, {"secret": "é"}, {"model": " "}, {"model": "x" * 257},
    {"model": "m\u007f"}, {"model": "m\u0080"}, {"model": "m\ud800"},
    {"protocol": "unknown"}, {"provider": "unknown"}, {"actor": "pretend-owner"},
    {"endpoint": "https://example.com"}])
def test_invalid_fields_never_dispatch(change):
    def forbidden(request):
        pytest.fail("invalid input dispatched")
    with pytest.raises(ValueError, match="Invalid credential probe input"):
        asyncio.run(api().probe(dict(PARAMS, **change), transport=httpx.MockTransport(forbidden)))


def test_total_deadline_cancels_slow_response_and_closes_stream(monkeypatch):
    network = importlib.import_module("hyprial.daemon.impl.model_probe.network")
    monkeypatch.setattr(network, "DEADLINE_SECONDS", 0.03)
    closed = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self):
            closed.append(True)

    result = asyncio.run(api().probe(PARAMS, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=SlowBody()))))
    assert result["outcome"] == "timeout"
    assert closed == [True]


def test_transport_error_never_leaks_request_or_retries():
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ConnectError(KEY, request=request)
    result = asyncio.run(api().probe(PARAMS, transport=httpx.MockTransport(fail)))
    assert result["outcome"] == "unreachable"
    assert len(calls) == 1
    assert KEY not in json.dumps(result)


def test_probe_deadline_leaves_time_for_the_default_http_envelope():
    from hyprial.daemon.impl.model_probe.network import DEADLINE_SECONDS
    from hyprial.shell.impl.request_http.config import DEFAULT_BUDGETS
    assert DEADLINE_SECONDS <= DEFAULT_BUDGETS["requestTimeoutSeconds"] - 5
