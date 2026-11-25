"""Unsupported key capabilities must not break the public daemon import path."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest


PROBE = '''
import builtins
original = builtins.__import__
def no_posix_in_user_key(name, globals=None, locals=None, fromlist=(), level=0):
    module = (globals or {}).get('__name__', '')
    if name == 'fcntl' and module.startswith('hyprial.identity.impl.user_key'):
        raise ModuleNotFoundError("No module named 'fcntl'", name='fcntl')
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = no_posix_in_user_key
from hyprial.daemon import DaemonApplication
print('DaemonApplication import PASS')
'''


def test_public_daemon_import_without_fcntl():
    result = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                            text=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert "DaemonApplication import PASS" in result.stdout


def test_unsupported_private_access_is_isolated(tmp_path, monkeypatch):
    from hyprial.identity.impl.user_key import store as storage
    from hyprial.daemon.impl.user_key.ipc import compose
    monkeypatch.setattr(storage, "private_access_supported", lambda: False)
    app = SimpleNamespace(hyprial_home=tmp_path, state_dir=tmp_path / "state", owner="alice")
    signer = compose(app)
    # Include valid prerequisites so absence of device/sub cannot mask the guard.
    signer.store.casdoor_sub = "synthetic-sub"
    signer.store.device_uid = "12345678123442348234123456789abc"
    assert signer.status()["state"] == "unavailable"
    assert not (tmp_path / "state").exists()
    with pytest.raises(storage.Error) as caught:
        signer.store.ensure()
    assert caught.value.code == "USER_KEY_UNBOUND"
    with pytest.raises(storage.Error) as audit_error:
        signer.audit.append({"decision": "refused", "code": "USER_KEY_UNBOUND"})
    assert audit_error.value.code == "USER_KEY_UNBOUND"
    assert not (tmp_path / "state").exists()
