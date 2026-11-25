"""Existing secret authority through isolated HTTP; no provider authentication."""
import json

from hyprial.kernel import daemon_request_once
from tests.integration.request_http.conftest import DaemonProcess, running_http

def test_setup_secret_authority_and_unsupported_fence_over_http(isolation):
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        with running_http(isolation) as http:
            key = 'synthetic-isolated-setup-not-a-real-key'
            body = {'entryId': 'setup-http', 'fieldName': 'key', 'value': key}
            path = '/daemon/request/agent.secret.provider-write'
            destination = isolation.home / 'secrets/providers/setup-http.json'
            status, _, denied = http.request('POST', path, body=body, token='wrong-token')
            assert (status, denied['dispatch']) == (401, 'not-dispatched')
            assert not destination.exists()
            status, _, saved = http.request('POST', path, body=body)
            assert (status, saved['dispatch']) == (200, 'dispatched'), saved
            assert saved['result'] == {'ok': True, 'entryId': 'setup-http', 'fieldNames': ['key']}
            assert key not in json.dumps(saved)
            assert json.loads(destination.read_text()) == {'key': key}
            status, _, listed = http.request('POST', '/daemon/request/agent.secret.provider-list', body={})
            assert status == 200, listed
            assert listed['result'] == {'ok': True, 'entries': [{'entryId': 'setup-http', 'fieldNames': ['key']}]}
            assert key not in json.dumps(listed)

            daemon_request_once('agent.create', {'name': 'setup-worker'}, socket_path=isolation.socket_path, timeout=10)
            grant = {'actor': 'setup-worker', 'grantId': 'setup-http', 'source': 'user-provider',
                     'entryId': 'setup-http', 'fieldName': 'key', 'environmentNames': ['TEST_MODEL_KEY'], 'revision': 1}
            path = '/daemon/request/agent.secret.grant'
            for unsupported in ({'uri': 'wrong', 'createdAtMs': 1}, None, True):
                status, _, denied = http.request('POST', path, body={**grant, 'expectedAgent': unsupported})
                assert (status, denied['code'], denied['dispatch']) == (400, 'INVALID_ARGUMENT', 'not-dispatched')
            before = daemon_request_once('agent.secret.list', {'actor': 'setup-worker'}, socket_path=isolation.socket_path, timeout=10)
            assert before['grants'] == []
            status, _, result = http.request('POST', path, body=grant)
            assert (status, result['dispatch']) == (200, 'dispatched'), result
            assert key not in json.dumps(result)
            after = daemon_request_once('agent.secret.list', {'actor': 'setup-worker'}, socket_path=isolation.socket_path, timeout=10)
            assert result['result']['grant'] in after['grants']
            status, _, repeated = http.request('POST', path, body=grant)
            assert status >= 400
            assert (repeated['code'], repeated['dispatch']) == ('AGENT_ERROR', 'dispatched')
            revoke = {'actor': 'setup-worker', 'grantId': 'setup-http'}
            path = '/daemon/request/agent.secret.revoke'
            status, _, denied = http.request('POST', path, body={**revoke, 'expectedAgent': None})
            assert (status, denied['code'], denied['dispatch']) == (400, 'INVALID_ARGUMENT', 'not-dispatched')
            status, _, denied = http.request('POST', path, body=revoke, token='wrong-token')
            assert (status, denied['dispatch']) == (401, 'not-dispatched')
            status, _, revoked = http.request('POST', path, body=revoke)
            assert status == 200, revoked
            assert revoked['result'] == {'ok': True, 'revoked': True}
            after = daemon_request_once('agent.secret.list', {'actor': 'setup-worker'}, socket_path=isolation.socket_path, timeout=10)
            assert after['grants'] == []
            assert json.loads(destination.read_text()) == {'key': key}
            assert key not in http.log_path.read_text()
    finally:
        daemon.stop()


def test_model_service_configuration_and_rotation_over_real_http(isolation):
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        with running_http(isolation) as http:
            key = "synthetic-route-key"
            params = {"entryId": "route-http", "fieldName": "key", "value": key,
                      "modelProvider": "deepseek", "protocol": "openai-completions",
                      "endpoint": "https://model.example/v1"}
            path = "/daemon/request/agent.secret.provider-configure"
            destination = isolation.home / "secrets/providers/route-http.json"
            status, _, denied = http.request("POST", path, body=params, token="wrong-token")
            assert (status, denied["dispatch"]) == (401, "not-dispatched")
            assert not destination.exists()
            status, _, refused = http.request("POST", path, body={**params, "routeRequired": False})
            assert (status, refused["dispatch"]) == (400, "not-dispatched")
            assert not destination.exists()
            status, _, saved = http.request("POST", path, body={**params, "createOnly": True})
            assert (status, saved["dispatch"]) == (200, "dispatched"), saved
            route = saved["result"]["routes"]["key"]
            assert route == {"endpoint": params["endpoint"], "protocol": "openai-completions", "vendorId": "deepseek"}
            original = destination.read_bytes()
            status, _, collision = http.request("POST", path,
                body={**params, "createOnly": True, "value": "must-not-overwrite"})
            assert (status, collision["code"]) == (400, "INVALID_ARGUMENT")
            assert destination.read_bytes() == original
            daemon_request_once("agent.create", {"name": "route-worker"}, socket_path=isolation.socket_path, timeout=10)
            grant = {"actor": "route-worker", "grantId": "route-http", "source": "user-provider",
                     "entryId": "route-http", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1}
            status, _, granted = http.request("POST", "/daemon/request/agent.secret.grant", body=grant)
            assert (status, granted["dispatch"]) == (200, "dispatched"), granted
            status, _, rotated = http.request("POST", "/daemon/request/agent.secret.provider-write",
                body={"entryId": "route-http", "fieldName": "key", "value": "synthetic-next-key"})
            assert status == 200, rotated
            assert json.loads(destination.read_text())["values"]["key"] == "synthetic-next-key"
            status, _, listed = http.request("POST", "/daemon/request/agent.secret.provider-list", body={})
            assert status == 200, listed
            assert listed["result"]["entries"] == [{"entryId": "route-http", "fieldNames": ["key"], "routes": {"key": route}}]
            assert key not in json.dumps([saved, granted, rotated, listed])
            assert key not in http.log_path.read_text()
    finally:
        daemon.stop()
