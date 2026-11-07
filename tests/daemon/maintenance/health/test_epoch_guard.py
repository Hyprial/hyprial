"""Delayed destructive requests cannot stop a replacement generation."""
from types import SimpleNamespace
import pytest
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.ipc.params import DaemonRequestError


def test_shutdown_rejects_stale_epoch_without_stop():
    stops=[]
    owner=SimpleNamespace(epoch="new", _request_stop=stops.append)
    with pytest.raises(DaemonRequestError) as error:
        _IpcDispatchMixin._ipc_shutdown(owner, {"expectedEpoch":"old"})
    assert error.value.code == "DAEMON_EPOCH_MISMATCH"
    assert stops == []


def test_shutdown_accepts_current_epoch(tmp_path):
    stops=[]
    owner=SimpleNamespace(epoch="new", _request_stop=stops.append,
                          state_dir=tmp_path, _lock_path=tmp_path/"daemon.lock")
    result=_IpcDispatchMixin._ipc_shutdown(owner, {"expectedEpoch":"new"})
    assert result["stopping"] and stops == ["ipc-shutdown"]


@pytest.mark.parametrize("value", [None, "", 1, True])
def test_explicit_invalid_epoch_never_falls_back_to_unfenced_stop(tmp_path, value):
    stops = []
    owner = SimpleNamespace(epoch="new", _request_stop=stops.append,
                            state_dir=tmp_path, _lock_path=tmp_path/"daemon.lock")
    with pytest.raises(DaemonRequestError) as error:
        _IpcDispatchMixin._ipc_shutdown(owner, {"expectedEpoch": value})
    assert error.value.code == "INVALID_ARGUMENT"
    assert stops == []
