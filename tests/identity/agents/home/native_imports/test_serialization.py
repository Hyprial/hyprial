"""Every native filesystem entry point shares the identity writer lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from hyprial.identity.impl.agents.home import native_credential as native


@pytest.mark.parametrize("action", ["inspect", "remove", "install", "read"])
def test_concurrent_native_access_waits_for_the_whole_install(tmp_path, monkeypatch, action):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    documents = [("auth.json", b"auth"), ("models-store.json", b"models")]
    entered, release, attempted = Event(), Event(), Event()
    write = native._write_plan

    def hold(*args):
        entered.set()
        assert release.wait(3)
        return write(*args)

    monkeypatch.setattr(native, "_write_plan", hold)

    def other():
        attempted.set()
        if action == "inspect":
            return native.inspect_native_credentials(home, "pi")
        if action == "remove":
            return native.remove_native_credentials(home, "pi")
        if action == "read":
            return native.read_native_credential_set(home, "pi")
        return native.install_native_credential_set(home, "pi", documents)

    with ThreadPoolExecutor(max_workers=2) as pool:
        importing = pool.submit(native.install_native_credential_set, home, "pi", documents)
        try:
            assert entered.wait(3)
            following = pool.submit(other)
            assert attempted.wait(3)
            with pytest.raises(TimeoutError):
                following.result(timeout=0.05)
        finally:
            release.set()
        importing.result(timeout=3)
        result = following.result(timeout=3)
    if action == "inspect":
        assert result["state"] == "stored"
    if action == "read":
        assert result == documents
    if action == "remove":
        assert native.inspect_native_credentials(home, "pi")["state"] == "absent"
