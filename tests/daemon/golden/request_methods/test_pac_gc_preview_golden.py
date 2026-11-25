"""PAC GC preview with a real empty and populated collector."""

from collections.abc import Callable

from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from tests.daemon.pac.lifecycle.test_pac_gc import (
    Application, Runtime, _close, _gc, _register_owned_worker, _service, _start_owned,
)
from tests.support.golden.app import GoldenDaemon, golden_daemon as golden_daemon


def test_pac_gc_preview_golden(golden_daemon: Callable[..., GoldenDaemon]) -> None:
    daemon = golden_daemon()
    root = daemon.root / 'gc-preview'
    root.mkdir()
    application = Application(root)
    collector = _gc(root, application, Runtime(), [])
    daemon.app._pac_gc = collector
    golden = daemon.golden('request_methods', 'pac-gc-preview')
    try:
        empty = golden.call('pac.gc.preview', {}, label='empty')
        assert empty['selected'] == []
        service = _service(root)
        try:
            graph_id = _start_owned(service, 'golden-gc')
            store = PacGraphStore(service.database)
            try:
                _close(store, graph_id)
                actor_name, _ = _register_owned_worker(application, store, graph_id)
            finally:
                store.close()
            result = golden.call('pac.gc.preview', {'ignored': {'nested': True}}, label='populated')
            assert result['selectedCount'] == 1
            assert result['selected'][0]['agent'] == actor_name
            assert application.agents.get(actor_name) is not None
            assert application.destroyed == []
        finally:
            service.close()
        golden.check()
    finally:
        application.agents.close()
