import threading
from types import SimpleNamespace

from hyprial.daemon.impl.lifecycle.duplicate_actor import DuplicateInstanceAuthority
from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport.keys import KeySpace


def test_presence_callback_only_admits_and_owner_deduplicates():
    entered, release = threading.Event(), threading.Event()
    events = []

    def logger(*args, **kwargs):
        events.append(kwargs["peerGeneration"])
        entered.set()
        assert release.wait(3)

    class Session:
        def declare_liveliness(self, *args):
            return SimpleNamespace(close=lambda: None)

        def observe_liveliness(self, key, callback, **kwargs):
            self.callback = callback
            return SimpleNamespace(close=lambda: None)

    session = Session()
    owner = DuplicateInstanceAuthority(session, "node", "self", logger=logger)
    message = TransportSample(KeySpace().daemon_liveliness("node", "peer"), b"")
    try:
        session.callback(message)
        assert entered.wait(1)
        session.callback(message)
        assert owner.duplicates == ("peer",)
    finally:
        release.set()
        owner.close()
    assert events == ["peer"]
