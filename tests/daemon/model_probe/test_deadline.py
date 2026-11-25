"""Explicit probe deadlines preserve the legacy budget and bound the entire response."""

import asyncio

import httpx
import pytest

from hyprial.daemon.impl.model_probe import network
from hyprial.daemon.impl.model_probe.request import prepare

PARAMS = {"provider": "custom", "secret": "synthetic-key", "model": "hy4-flash",
          "endpoint": "https://model.example/v1", "protocol": "openai-completions"}


def test_explicit_deadline_allows_response_after_legacy_budget(monkeypatch):
    monkeypatch.setattr(network, "DEADLINE_SECONDS", 0.01)

    async def respond(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, json={"model": "hy4-flash", "choices": [
            {"message": {"role": "assistant", "content": "OK"}}]})

    transport = httpx.MockTransport(respond)
    legacy = asyncio.run(network.probe(PARAMS, transport=transport))
    explicit = asyncio.run(network.probe(dict(PARAMS, timeoutSeconds=1), transport=transport))
    assert legacy["outcome"] == "timeout"
    assert explicit["outcome"] == "passed"


@pytest.mark.parametrize("value", [None, True, False, 0, 61, 1.5, "30"])
def test_invalid_explicit_deadline_is_rejected_before_network(value):
    with pytest.raises(ValueError, match="Invalid credential probe input"):
        prepare(dict(PARAMS, timeoutSeconds=value))


@pytest.mark.parametrize("value", [1, 30, 60])
def test_explicit_deadline_never_enters_upstream_body(value):
    request = prepare(dict(PARAMS, timeoutSeconds=value))
    assert request.body["model"] == "hy4-flash"
    assert "timeoutSeconds" not in request.body
