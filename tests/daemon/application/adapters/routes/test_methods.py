"""Route requests reuse the config writer and observed-chat authority."""

import json
from types import SimpleNamespace

import pytest

from hyprial.kernel import DaemonRequestError
from tests.daemon.application.adapters.authorization.test_status import app as app_fixture

app = app_fixture


@pytest.fixture(autouse=True)
def chat_names(monkeypatch):
    from hyprial.daemon.impl.adapters.lark.credentials import identities

    pages = {None: ((SimpleNamespace(chat_id='oc_group', name='测试群'),), None)}
    calls = []

    class Gateway:
        def list_chats_page(self, *, page_token=None):
            calls.append(page_token)
            value = pages[page_token]
            if isinstance(value, Exception):
                raise value
            return value

    def configured(home, state, name, *, http_timeout_seconds):
        assert name == 'demo' and http_timeout_seconds == 2.0
        return 'cli_test', Gateway()

    monkeypatch.setattr(identities, 'configured_identity_gateway', configured)
    return pages, calls


def routes(app):
    return app.handle('adapter.routes.list', {'name': 'demo'})['adapter']


def add(app, *, route='main', chat='oc_group', default=False, revision=None):
    return app.handle('adapter.routes.add', {'name': 'demo', 'route': route, 'chatId': chat,
        'makeDefault': default, 'expectedRevision': revision or routes(app)['revision']})


def test_route_add_read_remove_uses_existing_configuration(app):
    before = routes(app)
    assert before['routes'] == [] and before['defaultRoute'] is None
    secret = app.hyprial_home / 'secrets/lark-demo.json'
    secret_before = secret.read_bytes()
    saved = add(app)
    assert saved['configured'] is True and saved['reloaded'] is True
    after = routes(app)
    assert after['routes'] == [{'name': 'main', 'nativeId': 'oc_group', 'type': 'direct', 'chatName': '测试群'}]
    assert after['revision'] != before['revision']
    assert 'credentialRef' not in json.dumps(after)
    result = app.handle('adapter.routes.remove', {'name': 'demo', 'route': 'main',
        'expectedRevision': after['revision'], 'clearDefault': False})
    assert result['adapter']['routes'] == []
    assert secret.read_bytes() == secret_before


def test_stale_route_revision_does_not_change_configuration(app):
    before = routes(app)
    add(app, route='first')
    with pytest.raises(DaemonRequestError) as error:
        add(app, route='second', revision=before['revision'])
    assert error.value.code == 'ADAPTER_CONFIG_CONFLICT'
    assert len(routes(app)['routes']) == 1


def test_default_route_removal_requires_explicit_clear(app):
    add(app, default=True)
    before = routes(app)
    params = {'name': 'demo', 'route': 'main', 'expectedRevision': before['revision'], 'clearDefault': False}
    with pytest.raises(DaemonRequestError) as error:
        app.handle('adapter.routes.remove', params)
    assert error.value.code == 'ROUTE_IN_USE'
    assert routes(app) == before
    result = app.handle('adapter.routes.remove', {**params, 'clearDefault': True})
    assert result['adapter']['defaultRoute'] is None


def test_app_replacement_invalidates_route_expectation(app):
    from hyprial.daemon.impl.bootstrap.adapter_registration import add_lark_gateway

    before = routes(app)
    add_lark_gateway(hyprial_home=app.hyprial_home, state_dir=app.state_dir,
        name='demo', app_id='cli_replaced', app_secret='synthetic-replacement',
        routes=(), allow_no_routes=True, force=True)
    with pytest.raises(DaemonRequestError) as error:
        add(app, revision=before['revision'])
    assert error.value.code == 'ADAPTER_CONFIG_CONFLICT'
    assert routes(app)['routes'] == []


def test_unknown_mutation_is_not_retried(app, monkeypatch):
    from hyprial.daemon.impl.bootstrap import adapter_registration

    calls = []

    def uncertain(**kwargs):
        calls.append(kwargs)
        raise OSError('synthetic secret diagnostic must not escape')

    monkeypatch.setattr(adapter_registration, 'add_gateway_route', uncertain)
    with pytest.raises(DaemonRequestError) as error:
        add(app)
    assert error.value.code == 'SUBMIT_OUTCOME_UNKNOWN'
    assert 'secret diagnostic' not in str(error.value)
    assert len(calls) == 1


def test_saved_route_survives_failed_reload(app, monkeypatch):
    def unavailable():
        raise RuntimeError('synthetic reload failure')

    monkeypatch.setattr(app, '_ipc_adapter_reload', unavailable)
    result = add(app)
    assert result['configured'] is True
    assert result['reloaded'] is False
    assert result['nextAction'] == 'reload'
    assert routes(app)['routes'][0]['name'] == 'main'


@pytest.mark.parametrize('method,extra', [
    ('adapter.routes.list', {}), ('adapter.routes.candidates', {}),
    ('adapter.routes.add', {'route': 'main', 'chatId': 'oc_group', 'makeDefault': False, 'expectedRevision': '0'*64}),
    ('adapter.routes.remove', {'route': 'main', 'clearDefault': False, 'expectedRevision': '0'*64}),
])
def test_worker_cannot_read_or_write_routes(app, method, extra):
    with pytest.raises(DaemonRequestError) as error:
        app.handle(method, {'name': 'demo', **extra}, _trusted_message_origin='worker')
    assert error.value.code == 'CALLER_NOT_AUTHORIZED'


def test_missing_candidates_do_not_create_database(app):
    path = app.state_dir / 'adapters.sqlite3'
    assert not path.exists()
    result = app.handle('adapter.routes.candidates', {'name': 'demo'})
    assert result['candidates'] == []
    assert not path.exists()


def test_unreadable_candidates_do_not_pretend_to_be_empty(app):
    (app.state_dir / 'adapters.sqlite3').write_text('not a database')
    with pytest.raises(DaemonRequestError) as error:
        app.handle('adapter.routes.candidates', {'name': 'demo'})
    assert error.value.code == 'DAEMON_ERROR'


def test_candidates_are_namespaced_observed_chats(app):
    from hyprial.daemon.impl.adapters.lark.state.records import RequestCorrelation
    from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore

    path = app.state_dir / 'adapters.sqlite3'
    stores = []
    try:
        for name, chat in [('demo', 'oc_visible'), ('other', 'oc_hidden')]:
            store = LarkStateStore(path, adapter='lark:' + name)
            stores.append(store)
            store.record_inbound(RequestCorrelation(harness_message_id='hm-' + name,
                message_id='om-' + name, chat_id=chat, conversation_id='lark:' + chat), event_id='evt-' + name)
            store.record_chat_type(chat, 'group')
        result = app.handle('adapter.routes.candidates', {'name': 'demo'})
        assert result['candidates'] == [{'chatId': 'oc_visible', 'chatType': 'group', 'chatName': None}]
    finally:
        for store in stores:
            store.close()


def test_route_names_are_sdk_metadata_and_do_not_change_revision(app, chat_names):
    add(app)
    initial = routes(app)
    assert initial['routes'][0]['chatName'] == '测试群'
    assert initial['namesStatus'] == 'complete'
    chat_names[0][None] = ((SimpleNamespace(chat_id='oc_group', name='改名后的群'),), None)
    changed = routes(app)
    assert changed['routes'][0]['chatName'] == '改名后的群'
    assert initial['revision'] == changed['revision']
    assert 'chatName' not in json.dumps(add(app, route='other', chat='oc_other'))


def test_name_read_failure_preserves_routes_without_secret_diagnostics(app, chat_names):
    add(app)
    chat_names[0][None] = RuntimeError('secret credential in SDK diagnostic')
    result = routes(app)
    assert result['namesStatus'] == 'unavailable'
    assert result['routes'][0]['chatName'] is None
    assert result['routes'][0]['nativeId'] == 'oc_group'
    assert 'credential' not in json.dumps(result)


def test_group_lookup_stops_at_page_bound_and_preserves_partial_names(app, chat_names):
    add(app, chat='oc_absent')
    chat_names[0].update({None: ((SimpleNamespace(chat_id='oc_unrelated', name='other'),), 'again'), 'again': ((), 'again')})
    result = routes(app)
    assert result['namesStatus'] == 'partial'
    assert result['routes'][0]['chatName'] is None
    assert len(chat_names[1]) <= 5


def test_empty_candidates_never_query_the_platform(app, chat_names):
    assert app.handle('adapter.routes.candidates', {'name': 'demo'})['namesStatus'] == 'complete'
    assert chat_names[1] == []


@pytest.mark.parametrize('page', [(), (SimpleNamespace(chat_id='oc_other', name='其他群'),)])
def test_finished_enumeration_without_requested_chat_is_partial(app, chat_names, page):
    add(app)
    chat_names[0][None] = (page, None)
    result = routes(app)
    assert result['namesStatus'] == 'partial'
    assert result['routes'][0]['chatName'] is None
    assert result['routes'][0]['nativeId'] == 'oc_group'


def test_finished_enumeration_with_one_missing_chat_is_partial(app, chat_names):
    add(app)
    add(app, route='missing', chat='oc_absent')
    result = routes(app)
    assert result['namesStatus'] == 'partial'
    assert {row['nativeId']: row['chatName'] for row in result['routes']} == {
        'oc_group': '测试群', 'oc_absent': None,
    }


def test_platform_returned_null_name_is_a_complete_read(app, chat_names):
    add(app)
    chat_names[0][None] = ((SimpleNamespace(chat_id='oc_group', name=None),), None)
    result = routes(app)
    assert result['namesStatus'] == 'complete'
    assert result['routes'][0]['chatName'] is None


def test_writes_without_expected_revision_keep_every_route_refusal(app):
    """F1a: ``expectedRevision`` is optional; omitting it skips only the staleness check."""

    saved = app.handle('adapter.routes.add', {'name': 'demo', 'route': 'main', 'chatId': 'oc_group', 'makeDefault': True})
    assert saved['adapter']['defaultRoute'] == 'main' and saved['configured'] is True
    with pytest.raises(DaemonRequestError) as taken:
        app.handle('adapter.routes.add', {'name': 'demo', 'route': 'main', 'chatId': 'oc_other', 'makeDefault': False})
    assert taken.value.code == 'ROUTE_EXISTS'
    with pytest.raises(DaemonRequestError) as unbound:
        app.handle('adapter.routes.remove', {'name': 'demo', 'route': 'nope', 'clearDefault': False})
    assert unbound.value.code == 'ROUTE_NOT_FOUND'
    with pytest.raises(DaemonRequestError) as in_use:
        app.handle('adapter.routes.remove', {'name': 'demo', 'route': 'main', 'clearDefault': False})
    assert in_use.value.code == 'ROUTE_IN_USE'
    assert routes(app)['routes'][0]['nativeId'] == 'oc_group'
    removed = app.handle('adapter.routes.remove', {'name': 'demo', 'route': 'main', 'clearDefault': True})
    assert removed['adapter']['routes'] == [] and removed['adapter']['defaultRoute'] is None


def test_remove_with_stale_expected_revision_is_refused(app):
    before = routes(app)
    add(app, route='keep')
    with pytest.raises(DaemonRequestError) as error:
        app.handle('adapter.routes.remove', {'name': 'demo', 'route': 'keep',
            'clearDefault': False, 'expectedRevision': before['revision']})
    assert error.value.code == 'ADAPTER_CONFIG_CONFLICT'
    assert [route['name'] for route in routes(app)['routes']] == ['keep']


@pytest.mark.parametrize('method,extra', [
    ('adapter.routes.add', {'route': 'main', 'chatId': 'oc_group', 'makeDefault': False}),
    ('adapter.routes.remove', {'route': 'main', 'clearDefault': False}),
])
def test_write_schema_keeps_expected_revision_optional_but_well_formed(method, extra):
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

    schema = METHOD_REGISTRY.get(method).spec.params_schema
    assert 'expectedRevision' in schema['properties']
    assert 'expectedRevision' not in schema['required']
    assert set(extra) | {'name'} == set(schema['required'])
    assert schema['properties']['expectedRevision']['pattern'] == '^[0-9a-f]{64}$'
