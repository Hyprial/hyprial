"""Real HTTP/IPC maintenance reads through the ordinary production registry."""
import json

from tests.integration.request_http.conftest import DaemonProcess, running_http


def test_maintenance_reads_and_invalid_logs_over_isolated_http(isolation):
    logs = isolation.state / 'logs'
    logs.mkdir(exist_ok=True)
    rows = [{'ts': '2026-11-04T00:00:00Z', 'level': level, 'component': 'daemon',
             'event': 'daemon.started', 'message': 'PRIVATE_LOG_SENTINEL'}
            for level in ['info', [], {}, 'warn']]
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        (logs / 'settings-probe.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with running_http(isolation) as http:
            for method in ['daemon.status', 'autoupdate.status', 'upgrade.steps.status', 'log.page']:
                status, _, payload = http.request('POST', '/daemon/request/' + method, body={})
                assert status == 200, payload
                assert payload['dispatch'] == 'dispatched'
                assert payload['requestId']
                result = payload['result']
                if method == 'daemon.status':
                    assert set(result) == {'running', 'version', 'phase'}
                    assert result['running'] is True
                if method == 'log.page':
                    assert result['partial'] is True
                    assert 'invalid-record' in result['partialReasons']
                    assert result['skippedLines'] >= 2
                    assert 'PRIVATE_LOG_SENTINEL' not in json.dumps(result)
            for params in [{'path': '/private'}, {'limit': True}, {'cursor': None}]:
                status, _, payload = http.request('POST', '/daemon/request/log.page', body=params)
                assert (status, payload['code'], payload['dispatch']) == (400, 'INVALID_ARGUMENT', 'not-dispatched')
            status, _, payload = http.request('POST', '/daemon/request/daemon.status', body={}, token='wrong-test-token')
            assert (status, payload['code'], payload['dispatch']) == (401, 'HTTP_UNAUTHORIZED', 'not-dispatched')
            status, _, payload = http.request('POST', '/daemon/request/daemon.status', body={},
                headers={'X-Hyprial-Catalog-Digest': 'sha256:' + '0' * 64})
            assert (status, payload['code'], payload['dispatch']) == (409, 'CATALOG_MISMATCH', 'not-dispatched')
    finally:
        daemon.stop()
