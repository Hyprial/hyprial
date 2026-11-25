"""Catalog registration for the existing PAC GC preview."""

from hyprial.daemon.impl.pac.actors.gc import PacGc
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app


def test_real_empty_preview_has_a_registry_binding(app, tmp_path):
    collector = PacGc(state_dir=tmp_path, application=object(), runtime=object(),
                      logger=lambda *args, **kwargs: None, start_thread=False)
    app._pac_gc = collector
    result = app.handle('pac.gc.preview', {'ignored': {'x': 1}})
    assert result == {'ok': True, 'dryRun': True, 'selected': [], 'selectedCount': 0,
                      'removableBacklog': 0, 'skipped': {}, 'errors': []}
    binding = METHOD_REGISTRY.get('pac.gc.preview')
    assert binding is not None
    assert binding.spec.http_exposed is False
    assert binding.spec.mutates is True
    assert binding.spec.retry == 'never'


def test_preview_with_database_advances_observation_without_destroying(app):
    from hyprial.daemon.impl.pac.graphs.reactor import NullSender
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

    service = GraphWorkflowService(state_dir=app.state_dir, owner=app.owner, machine=app.node_id,
                                   sender=NullSender(), start_thread=False)
    try:
        app.agents.create('gc-observation')
        collector = PacGc(state_dir=app.state_dir, application=app, runtime=object(),
                          logger=lambda *args, **kwargs: None, start_thread=False)
        app._pac_gc = collector
        before = app._agent_session_domains.cells._runs.revision
        result = app.handle('pac.gc.preview', {})
        assert result['selected'] == []
        assert app._agent_session_domains.cells._runs.revision == before + 1
        assert app.agents.get('gc-observation') is not None
    finally:
        service.close()


def test_unavailable_preserves_code_message_and_no_data(app):
    import pytest
    from hyprial.kernel import DaemonRequestError

    app._pac_gc = None
    with pytest.raises(DaemonRequestError) as caught:
        app.handle('pac.gc.preview', {'ignored': True})
    assert caught.value.code == 'PAC_GC_UNAVAILABLE'
    assert str(caught.value) == 'PAC GC service is unavailable'
    assert caught.value.data is None


def test_restore_gate_and_missing_binding_do_not_call_collector(app, monkeypatch):
    import pytest
    from hyprial.kernel import DaemonRequestError, ipc_errors

    class Collector:
        def preview(self):
            pytest.fail('collector must not run')

    previous = app._pac_gc
    app._pac_gc = Collector()
    try:
        app._restore_done.clear()
        try:
            with pytest.raises(ipc_errors.DaemonRestoringError):
                app.handle('pac.gc.preview', {})
        finally:
            app._restore_done.set()
        monkeypatch.delitem(METHOD_REGISTRY._bindings, 'pac.gc.preview')
        with pytest.raises(DaemonRequestError) as caught:
            app.handle('pac.gc.preview', {})
        assert caught.value.code == ipc_errors.METHOD_NOT_FOUND
    finally:
        app._pac_gc = previous


def test_worker_relay_does_not_expose_preview():
    import pytest
    from hyprial.kernel import WorkerBinding, WorkerChannelError, bound_request, ipc_errors

    binding = WorkerBinding('agent:owner:node:worker', 'session-1', 'epoch-1')
    with pytest.raises(WorkerChannelError) as caught:
        bound_request({'version': 1, 'id': 'one', 'method': 'pac.gc.preview', 'params': {}}, binding)
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND


def test_real_socket_refuses_nonobject_before_unavailable(app):
    import threading
    from pathlib import Path
    from uuid import uuid4

    import pytest
    from hyprial.kernel import RequestPortError, daemon_request, example_to_json, ipc_errors
    from hyprial.daemon.impl.request_catalog.pac_gc.examples import EXAMPLES
    from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime

    app.socket_path = Path('/tmp') / f'pac-gc-{uuid4().hex}.sock'
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._pac_gc = None
    app._start_server()
    thread = threading.Thread(target=app._serve, daemon=True)
    thread.start()
    try:
        cases = {case.name: example_to_json(case) for case in EXAMPLES[0].cases}
        with pytest.raises(RequestPortError) as invalid:
            daemon_request('pac.gc.preview', cases['non-object']['params'], socket_path=app.socket_path, timeout=10, restore_wait=0)
        assert invalid.value.code == cases['non-object']['error']['code'] == ipc_errors.INVALID_ARGUMENT
        assert str(invalid.value) == cases['non-object']['error']['message']
        with pytest.raises(RequestPortError) as unavailable:
            daemon_request('pac.gc.preview', cases['unavailable']['params'], socket_path=app.socket_path, timeout=10, restore_wait=0)
        assert unavailable.value.code == cases['unavailable']['error']['code']
        assert str(unavailable.value) == cases['unavailable']['error']['message']
        assert unavailable.value.data is None
        with pytest.raises(RequestPortError) as extra:
            daemon_request('pac.gc.preview', {'ignored': [1]}, socket_path=app.socket_path, timeout=10, restore_wait=0)
        assert extra.value.code == unavailable.value.code and str(extra.value) == str(unavailable.value)
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        thread.join(2)
        app.socket_path.unlink(missing_ok=True)
        assert not thread.is_alive()


def test_real_socket_returns_populated_collector_without_deleting(app, tmp_path):
    import threading
    from pathlib import Path
    from uuid import uuid4

    from hyprial.daemon.impl.pac.storage.store import PacGraphStore
    from hyprial.kernel import daemon_request
    from tests.daemon.pac.lifecycle.test_pac_gc import (
        Application, Runtime, _close, _gc, _register_owned_worker, _service, _start_owned,
    )
    from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime

    root = tmp_path / 'actual-gc'
    root.mkdir()
    application = Application(root)
    service = _service(root)
    collector = None
    try:
        graph_id = _start_owned(service, 'ipc-gc')
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            actor_name, _ = _register_owned_worker(application, store, graph_id)
        finally:
            store.close()
        collector = _gc(root, application, Runtime(), [])
        app._pac_gc = collector
        app.socket_path = Path('/tmp') / f'pac-gc-{uuid4().hex}.sock'
        app._runtime = _IdleRuntime()
        app._maintenance_generation = 1
        app._start_server()
        thread = threading.Thread(target=app._serve, daemon=True)
        thread.start()
        try:
            result = daemon_request('pac.gc.preview', {'ignored': {'nested': True}},
                                    socket_path=app.socket_path, timeout=10, restore_wait=0)
            assert result['selectedCount'] == result['removableBacklog'] == 1
            assert result['selected'] == [{'graphId': graph_id,
                'actor': f'agent:test:machine:{actor_name}', 'agent': actor_name}]
            assert application.destroyed == [] and application.agents.get(actor_name) is not None
        finally:
            app.stop_event.set()
            if app._server is not None:
                app._server.close()
            thread.join(2)
            app.socket_path.unlink(missing_ok=True)
            assert not thread.is_alive()
    finally:
        app._pac_gc = None
        if collector is not None:
            collector.close()
        service.close()
        application.agents.close()
