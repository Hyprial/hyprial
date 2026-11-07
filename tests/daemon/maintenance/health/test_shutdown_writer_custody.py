"""The previous epoch must stop its heartbeat writer before releasing home."""
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
import pytest


@pytest.mark.parametrize("first_timeout", [False, True])
def test_heartbeat_writer_drains_before_home_lock_release(tmp_path, own_daemon_application, first_timeout):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state = home / "state"
    state.mkdir(mode=0o700)
    app = own_daemon_application(DaemonApplication(
        state_dir=state, socket_path=state / "daemon.sock", node_id="health-close",
        hyprial_home=home, owner="fixture", **daemon_dependencies(),
    ))
    observations = []

    class Lock:
        closed = False

        def close(self):
            self.closed = True

    lock = Lock()

    class Health:
        closed = False
        allow = not first_timeout

        def close(self, timeout):
            if not self.closed:
                observations.append(lock.closed)
                if not self.allow:
                    return False
                self.closed = True
            return True

    app._lock_stream = lock
    health = app._database_health = Health()
    if first_timeout:
        try:
            with pytest.raises(RuntimeError, match="database health"):
                app._close()
            assert not lock.closed
        finally:
            health.allow = True
    app._close()
    assert observations == [False] * (2 if first_timeout else 1)
    assert lock.closed
