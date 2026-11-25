"""Only an explicitly budgeted model probe may extend the carrier deadline."""

import json
from types import SimpleNamespace

import pytest

from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.request_http.test_server import _config, _entries, _fake, _http


@pytest.mark.parametrize("duration,expected", [(None, 15), (1, 6), (30, 35), (30.0, 35), (60, 65)])
def test_probe_budget_reaches_daemon_after_carrier_elapsed(tmp_path, monkeypatch, duration, expected):
    handshake, _ = _fake(_entries())
    observed = []

    def request(method, params, **kwargs):
        if method != "agent.secret.provider-test":
            return handshake(method, params, **kwargs)
        observed.append(kwargs["timeout"])
        return {"ok": True, "outcome": "passed", "httpStatus": 200, "latencyMs": 1}

    server = RequestHttpServer(_config(tmp_path), request_once=request)
    server.start()
    try:
        params = {"provider": "deepseek", "secret": "synthetic-key", "model": "hy4-flash"}
        if duration is not None:
            params["timeoutSeconds"] = duration
        from hyprial.shell.impl.request_http import server as carrier
        with monkeypatch.context() as patch:
            patch.setattr(carrier, "time", SimpleNamespace(monotonic=lambda: 102.0))
            result = server._invoke("agent.secret.provider-test", "fixed-clock", 100.0, params, None)
        assert result.status == 200, result.payload
        assert len(observed) == 1
        assert observed[0] == expected - 2
    finally:
        server.shutdown()


@pytest.mark.parametrize("duration", [0, 61, True, "30", 1.5])
def test_invalid_probe_budget_never_dispatches(tmp_path, duration):
    handshake, calls = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=handshake)
    server.start()
    before = calls["count"]
    try:
        status, _, raw = _http(server, "POST", "/daemon/request/agent.secret.provider-test",
                               token=server.token, body={"provider": "deepseek", "secret": "synthetic",
                                                        "model": "hy4-flash", "timeoutSeconds": duration})
        assert status == 400, json.loads(raw)
        assert calls["count"] == before
    finally:
        server.shutdown()


def test_exhausted_carrier_budget_never_dispatches(tmp_path, monkeypatch):
    handshake, calls = _fake(_entries())
    server = RequestHttpServer(_config(tmp_path), request_once=handshake)
    server.start()
    before = calls['count']
    try:
        from hyprial.shell.impl.request_http import server as carrier
        with monkeypatch.context() as patch:
            patch.setattr(carrier, 'time', SimpleNamespace(monotonic=lambda: 107.0))
            result = server._invoke('agent.secret.provider-test', 'expired', 100.0,
                {'provider': 'deepseek', 'secret': 'synthetic', 'model': 'x', 'timeoutSeconds': 1}, None)
        assert result.status == 408
        assert calls['count'] == before
    finally:
        server.shutdown()


def test_non_probe_method_keeps_its_original_carrier_budget(tmp_path, monkeypatch):
    handshake, _ = _fake(_entries())
    observed = []

    def request(method, params, **kwargs):
        if method != 'agent.secret.provider-list':
            return handshake(method, params, **kwargs)
        observed.append(kwargs['timeout'])
        return {'ok': True, 'entries': []}

    server = RequestHttpServer(_config(tmp_path), request_once=request)
    server.start()
    try:
        from hyprial.shell.impl.request_http import server as carrier
        with monkeypatch.context() as patch:
            patch.setattr(carrier, 'time', SimpleNamespace(monotonic=lambda: 102.0))
            result = server._invoke('agent.secret.provider-list', 'original-budget', 100.0, {}, None)
            invalid = server._invoke('agent.secret.provider-list', 'unknown-budget', 100.0,
                                     {'timeoutSeconds': 60}, None)
        assert result.status == 200
        assert observed == [13.0]
        assert invalid.status == 400
    finally:
        server.shutdown()
