"""Independent-review regressions use only synthetic isolated credentials."""

import json
import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.request_http.errors import status_for
from tests.daemon.model_probe.test_saved_connection import saved as saved
from tests.daemon.provider_keys.legacy.test_legacy_secret_hook import real_app as real_app


def call(app, method, params):
    return METHOD_REGISTRY.invoke(app, 'agent.secret.provider-' + method, params,
                                  trusted_message_origin='ipc')


def test_mixed_catalog_preserves_legacy_fields_without_exposing_generic_key(saved):
    app, resolver, selected = saved
    resolver.configure_model_service('legacy', field_name='key', value='synthetic',
        endpoint='https://legacy.example/v1', protocol='openai-completions', vendor_id='deepseek')
    resolver.write_user_provider('custom', {'plain': 'synthetic-other'})
    rows = {row['entryId']: row for row in call(app, 'list', {})['entries']}
    assert rows['legacy']['routes']['key']['vendorId'] == 'deepseek'
    assert rows['custom']['fieldNames'] == ['plain']
    assert 'key' not in rows['custom'].get('routes', {})
    modern = {row['entryId']: row for row in call(app, 'list', {'connectionVersion': 1})['entries']}
    assert modern['custom']['connections']['key']['connectionRef'] == selected['connectionRef']


def test_legacy_configure_can_update_an_unmigrated_field_in_v2_entry(saved):
    app, resolver, selected = saved
    params = {'entryId': 'custom', 'fieldName': 'legacy', 'value': 'synthetic-legacy',
              'endpoint': 'https://legacy.example/v1', 'protocol': 'openai-completions',
              'modelProvider': 'deepseek'}
    call(app, 'configure', params)
    call(app, 'configure', dict(params, endpoint='https://updated.example/v1'))
    row = resolver.model_service_metadata('custom')
    assert row['routes']['legacy']['endpoint'] == 'https://updated.example/v1'
    assert row['connections']['key']['connectionRef'] == selected['connectionRef']


def test_json_integral_numbers_match_schema_and_runtime(saved, monkeypatch):
    app, resolver, selected = saved
    config = {key: value for key, value in resolver.model_service_metadata('custom')['connections']['key'].items()
              if key not in {'connectionRef', 'configRevision'}}
    config['version'] = 1.0
    config['preset']['version'] = 1.0
    call(app, 'configure', {'entryId': 'custom', 'fieldName': 'key', 'connection': config,
                          'expectedRevision': 1.0})
    from hyprial.daemon.impl.model_probe.request import prepare
    prepared = prepare({'provider': 'deepseek', 'secret': 'synthetic', 'model': 'hy4-flash',
                        'timeoutSeconds': 30.0})
    assert prepared.body['model'] == 'hy4-flash'


@pytest.mark.parametrize('endpoint', ['https://example.test/a"b', 'https://example.test/<x>',
                                     'https://example.test/\u00a0x'])
def test_invalid_saved_endpoint_is_rejected_before_write(saved, endpoint):
    app, resolver, _ = saved
    path = resolver.hyprial_home / 'secrets/providers/custom.json'
    before = path.read_bytes()
    connection = {'version': 1, 'endpoint': endpoint, 'protocol': 'openai-completions',
                  'profile': 'openai-chat-completions/v1'}
    with pytest.raises(DaemonRequestError) as error:
        call(app, 'configure', {'entryId': 'custom', 'fieldName': 'key', 'connection': connection,
                              'expectedRevision': 1})
    assert error.value.code == ipc_errors.MODEL_CONFIGURATION_INVALID
    assert path.read_bytes() == before


@pytest.mark.parametrize('key', ['tab\tkey', '非ASCII', 'has space'])
def test_unusable_new_connection_key_is_rejected_before_write(saved, key):
    app, resolver, _ = saved
    connection = {'version': 1, 'endpoint': 'https://model.example/v1',
                  'protocol': 'openai-completions', 'profile': 'openai-chat-completions/v1'}
    with pytest.raises(DaemonRequestError) as error:
        call(app, 'configure', {'entryId': 'new', 'fieldName': 'key', 'connection': connection,
                              'expectedRevision': 0, 'value': key})
    assert error.value.code in {ipc_errors.MODEL_CONFIGURATION_INVALID, ipc_errors.INVALID_ARGUMENT}
    assert not (resolver.hyprial_home / 'secrets/providers/new.json').exists()


def test_missing_first_key_is_configuration_error(saved):
    app, _, _ = saved
    connection = {'version': 1, 'endpoint': 'https://model.example/v1',
                  'protocol': 'openai-completions', 'profile': 'openai-chat-completions/v1'}
    with pytest.raises(DaemonRequestError) as error:
        call(app, 'configure', {'entryId': 'new', 'fieldName': 'key', 'connection': connection,
                              'expectedRevision': 0})
    assert error.value.code == ipc_errors.MODEL_CONFIGURATION_INVALID
    assert error.value.data == {'phase': 'validate', 'reason': 'invalid-connection'}


def test_corrupt_gate_has_safe_authority_error(saved, monkeypatch):
    app, _, selected = saved
    from hyprial.daemon.impl.provider_keys import legacy

    def corrupt(*args):
        raise ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    monkeypatch.setattr(legacy, '_gate_closed', corrupt)
    with pytest.raises(DaemonRequestError) as error:
        call(app, 'test', {'connection': selected, 'model': 'hy4-flash'})
    assert error.value.code == ipc_errors.MODEL_AUTH_UNAVAILABLE
    assert error.value.data == {'phase': 'authorize', 'reason': 'unreadable'}


def test_connection_errors_have_http_status():
    assert status_for(ipc_errors.MODEL_CONFIGURATION_CONFLICT) == 409
    assert status_for(ipc_errors.MODEL_AUTH_UNAVAILABLE) == 403
    assert status_for(ipc_errors.MODEL_CONFIGURATION_INVALID) == 400


def test_legacy_timeout_has_phase_without_changing_legacy_input(saved, monkeypatch):
    app, _, _ = saved
    from hyprial.daemon.impl import model_probe

    async def timeout(params):
        return {'ok': True, 'outcome': 'timeout', 'httpStatus': None, 'latencyMs': 10}

    monkeypatch.setattr(model_probe, 'probe', timeout)
    result = call(app, 'test', {'provider': 'deepseek', 'secret': 'synthetic', 'model': 'x'})
    assert result['phase'] == 'probe'
    assert result['reason'] == 'deadline-exceeded'


def test_saved_shape_http_preserves_precise_errors_and_revision(saved, tmp_path, monkeypatch):
    from hyprial.daemon.impl import model_probe
    from hyprial.shell.impl.request_http.server import RequestHttpServer
    from tests.shell.request_http.test_server import _config, _entries, _fake, _http

    app, resolver, selected = saved
    handshake, _ = _fake(_entries())
    sent = []

    async def probe(params):
        sent.append(params)
        return {'ok': True, 'outcome': 'passed', 'httpStatus': 200, 'latencyMs': 1}

    monkeypatch.setattr(model_probe, 'probe', probe)

    def request(method, params, **kwargs):
        if method != 'agent.secret.provider-test':
            return handshake(method, params, **kwargs)
        return METHOD_REGISTRY.invoke(app, method, params, trusted_message_origin='ipc')

    server = RequestHttpServer(_config(tmp_path), request_once=request)
    server.start()
    try:
        body = {'connection': selected, 'model': 'hy4-flash', 'timeoutSeconds': 30.0}
        status, _, response = _http(server, 'POST', '/daemon/request/agent.secret.provider-test',
                                    token=server.token, body=body)
        assert status == 200, response
        assert sent[0]['model'] == 'hy4-flash'
        status, _, response = _http(server, 'POST', '/daemon/request/agent.secret.provider-test',
            token=server.token, body={**body, 'connection': dict(selected, configRevision=2)})
        assert status == 409
        assert json.loads(response)['data'] == {'phase': 'bind', 'reason': 'revision-mismatch'}
        path = resolver.hyprial_home / 'secrets/providers/custom.json'
        path.unlink()
        status, _, response = _http(server, 'POST', '/daemon/request/agent.secret.provider-test',
                                    token=server.token, body=body)
        assert status == 403
        assert json.loads(response)['data'] == {'phase': 'authorize', 'reason': 'missing'}
        assert len(sent) == 1
    finally:
        server.shutdown()


def test_method_level_old_writer_preserves_v2_route_and_rejects_bad_key(real_app):
    from hyprial.identity import SecretResolver

    connection = {'version': 1, 'endpoint': 'https://model.example/v1',
                  'protocol': 'openai-completions', 'profile': 'openai-chat-completions/v1'}
    call(real_app, 'configure', {'entryId': 'custom', 'fieldName': 'key', 'value': 'synthetic',
                                'connection': connection, 'expectedRevision': 0})
    resolver = SecretResolver(real_app.hyprial_home, real_app._agent_registry)
    before = resolver.model_service_metadata('custom')
    call(real_app, 'write', {'entryId': 'custom', 'fieldName': 'key', 'value': 'synthetic-rotated'})
    assert resolver.model_service_metadata('custom') == before
    path = resolver.hyprial_home / 'secrets/providers/custom.json'
    data = path.read_bytes()
    with pytest.raises(DaemonRequestError):
        call(real_app, 'write', {'entryId': 'custom', 'fieldName': 'key', 'value': 'invalid\tkey'})
    assert path.read_bytes() == data
