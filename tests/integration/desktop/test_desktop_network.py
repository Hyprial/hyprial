"""Desktop identity/registration integration without touching a live network."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests/fixtures'))
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc
import hyprial.shell.impl.desktop_network as api
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.daemon.impl.network import tailcat as tailcat_module
from hyprial.identity.impl.device import DeviceRecord, write_device_record


def _record(owner: str = "alicesmith") -> DeviceRecord:
    return DeviceRecord(
        device_id="desktop-test",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        key_generation=1,
    )


def _profile(server) -> NetworkProfile:
    return NetworkProfile("test", server.issuer, "fake-client-id")


def test_desktop_requires_identity_even_with_a_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "locate_tailcat_sidecar", lambda home, environ=None: tmp_path / "sidecar")
    value = api.status(tmp_path)
    assert value["authenticated"] is False
    assert value["ready"] is False
    assert value["deviceReady"] is False
    assert value["sidecarError"] is None


def test_desktop_status_reports_missing_sidecar_without_failing(tmp_path, monkeypatch):
    def missing(home, environ=None):
        raise tailcat_module.TailcatSidecarError("SIDECAR_MISSING", "no binary")

    monkeypatch.setattr(api, "locate_tailcat_sidecar", missing)
    write_device_record(tmp_path, _record())
    value = api.status(tmp_path)
    assert value["sidecarError"] == "SIDECAR_MISSING"
    assert value["deviceReady"] is True
    assert value["ready"] is False  # authenticated is still False
    assert value["profile"]["version"] == 2


def test_real_oidc_private_login_then_device_provisioning(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setenv("HYPRIAL_NODE_ID", "desktop-test")
    monkeypatch.setattr(api, "locate_tailcat_sidecar", lambda home, environ=None: tmp_path / "sidecar")
    events = []
    config = FakeOidcConfig(pending_polls=0)
    with fake_oidc(config) as server:
        monkeypatch.setattr(api, "resolve_profile", lambda **_: (_profile(server), "test fixture"))
        provisioned: list[dict[str, str]] = []

        def ensure(home, *, owner, device_id, environ=None):
            provisioned.append({"owner": owner, "deviceId": device_id})
            record = DeviceRecord(
                device_id=device_id,
                owner=owner,
                server_public="nodekey:server",
                client_public="nodekey:client",
                key_generation=1,
            )
            write_device_record(home, record)
            return record

        monkeypatch.setattr(api, "ensure_device_key", ensure)
        result = api.onboard(tmp_path, "login", lambda kind, data: events.append({"event": kind, **data}))
    assert result["ok"] is True
    assert result["device"]["ready"] is True
    assert result["device"]["deviceId"] == "desktop-test"
    assert provisioned == [{"owner": "alicesmith", "deviceId": "desktop-test"}]
    assert result["state"]["ready"] is True
    assert result["state"]["authenticated"] is True
    assert result["state"]["deviceReady"] is True
    credential = json.loads((tmp_path / "secrets/login.json").read_text())
    assert credential["refreshToken"] not in json.dumps({"result": result, "events": events})
    assert json.loads((tmp_path / "settings.json").read_text())["owner"] == "alicesmith"


def test_login_succeeds_when_the_sidecar_is_missing(tmp_path, monkeypatch):
    """D12 on the desktop surface: identity is committed, device reports the
    typed error, and the login action itself stays ok."""
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)

    def missing(home, environ=None):
        raise tailcat_module.TailcatSidecarError("SIDECAR_MISSING", "no binary")

    monkeypatch.setattr(api, "locate_tailcat_sidecar", missing)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        monkeypatch.setattr(api, "resolve_profile", lambda **_: (_profile(server), "test fixture"))

        def ensure(home, *, owner, device_id, environ=None):
            raise tailcat_module.TailcatSidecarError("SIDECAR_MISSING", "no binary")

        monkeypatch.setattr(api, "ensure_device_key", ensure)
        result = api.onboard(tmp_path, "login", lambda *_: None)
    assert result["ok"] is True
    assert result["device"]["ready"] is False
    assert result["device"]["error"]["code"] == "SIDECAR_MISSING"
    assert "hyprial-tailcat" in result["device"]["nextStep"]
    assert json.loads((tmp_path / "settings.json").read_text())["owner"] == "alicesmith"


def test_device_action_retries_provisioning_after_login(tmp_path, monkeypatch):
    """action=device skips OIDC and runs only the device stage; its own
    failure is reported as not-ok (unlike the login action's D12 semantics)."""
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setattr(api, "locate_tailcat_sidecar", lambda home, environ=None: tmp_path / "sidecar")
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        profile = _profile(server)
        monkeypatch.setattr(api, "resolve_profile", lambda **_: (profile, "test fixture"))
        monkeypatch.setattr(api, "ensure_device_key", lambda home, *, owner, device_id, environ=None: _record(owner))
        assert api.onboard(tmp_path, "login", lambda *_: None)["ok"] is True

        def failing(home, *, owner, device_id, environ=None):
            raise tailcat_module.TailcatSidecarError("KEYGEN_FAILED", "genkey exited 1")

        monkeypatch.setattr(api, "ensure_device_key", failing)
        failed = api.onboard(tmp_path, "device", lambda *_: None)
        assert failed["ok"] is False
        assert failed["device"]["error"]["code"] == "KEYGEN_FAILED"

        monkeypatch.setattr(api, "ensure_device_key", lambda home, *, owner, device_id, environ=None: _record(owner))
        recovered = api.onboard(tmp_path, "device", lambda *_: None)
        assert recovered["ok"] is True
        assert recovered["device"]["ready"] is True


def test_device_action_requires_a_committed_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setattr(api, "locate_tailcat_sidecar", lambda home, environ=None: tmp_path / "sidecar")
    monkeypatch.setattr(
        api,
        "ensure_device_key",
        lambda *a, **kw: pytest.fail("device stage must not run without identity"),
    )
    result = api.onboard(tmp_path, "device", lambda *_: None)
    assert result["ok"] is False
    assert result["code"] == "NOT_LOGGED_IN"


def test_login_refuses_managed_owner_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_OWNER", "production-owner")
    with pytest.raises(ValueError, match="owner override"):
        api.onboard(tmp_path, "login", lambda *_: pytest.fail("unexpected login"))
    assert not (tmp_path / "secrets").exists()


def test_onboard_rejects_unknown_actions(tmp_path):
    with pytest.raises(ValueError, match="Unsupported desktop onboarding action"):
        api.onboard(tmp_path, "join", lambda *_: None)


@pytest.mark.parametrize('record', [[], None, 'unexpected'])
def test_malformed_saved_records_do_not_enable_network(tmp_path, monkeypatch, record):
    monkeypatch.setattr(api, "locate_tailcat_sidecar", lambda home, environ=None: tmp_path / "sidecar")
    (tmp_path / 'settings.json').write_text(json.dumps(record))
    result = api.status(tmp_path)
    assert result['ready'] is False
    assert result['deviceReady'] is False
    assert result['owner'] is None


def test_desktop_login_commits_its_generation_under_the_identity_lock(tmp_path, monkeypatch):
    """The generation is read-modify-written; outside the lock two writers
    could mint the same number and a gap takeover would go undetected."""
    from hyprial.kernel import ProcessFileLock
    from hyprial.shell.impl.login import generation as generation_module

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.setattr(api, "locate_tailcat_sidecar", lambda home, environ=None: tmp_path / "sidecar")
    seen: list[str] = []
    real_next = generation_module.next_identity_generation

    def next_generation(home):
        seen.append(ProcessFileLock(home / ".identity-transaction.lock").probe())
        return real_next(home)

    monkeypatch.setattr(generation_module, "next_identity_generation", next_generation)
    with fake_oidc(FakeOidcConfig(pending_polls=0)) as server:
        monkeypatch.setattr(api, "resolve_profile", lambda **_: (_profile(server), "test fixture"))
        monkeypatch.setattr(
            api, "ensure_device_key", lambda home, *, owner, device_id, environ=None: _record(owner)
        )
        api.onboard(tmp_path, "login", lambda *_: None)
    assert seen == ["self-owned"]
    assert ProcessFileLock(tmp_path / ".identity-transaction.lock").probe() == "free"
