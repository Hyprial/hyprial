"""Windows client/build smoke units carried by the public GitHub snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hyprial import tsnet_sidecar
from hyprial.forwarding_config import daemon_forwarding_environment


@pytest.fixture(autouse=True)
def _windows_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real Windows runner values, and emulate them on other hosts."""

    if tsnet_sidecar.platform.system().lower() != "windows":
        monkeypatch.setattr(tsnet_sidecar.platform, "system", lambda: "Windows")
    if tsnet_sidecar.platform.machine().lower() not in ("amd64", "x86_64"):
        monkeypatch.setattr(tsnet_sidecar.platform, "machine", lambda: "AMD64")
    monkeypatch.delenv("HYPRIAL_BUNDLED_TSNET_BINARY", raising=False)


def test_windows_amd64_coordinates_use_exe_and_the_published_pin(tmp_path: Path) -> None:
    assert tsnet_sidecar.current_platform() == "windows-amd64"
    assert tsnet_sidecar.sidecar_binary_path(tmp_path).name == "hyprial-tsnet.exe"
    assert tsnet_sidecar.sidecar_asset_name() == "hyprial-tsnet-windows-amd64.exe"
    assert tsnet_sidecar.sidecar_asset_url().endswith(
        "/tsnet-v0.1.6/hyprial-tsnet-windows-amd64.exe"
    )
    # Measured from the public tsnet-v0.1.6 asset on 2026-09-28.
    assert tsnet_sidecar.expected_sha256("windows-amd64") == (
        "7d35086dfc0143869e82d70b9b287d0336d65bda89d83fe3edbe25ce39a1aa8a"
    )


def test_windows_arm64_remains_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tsnet_sidecar.platform, "machine", lambda: "ARM64")
    with pytest.raises(tsnet_sidecar.SidecarError) as raised:
        tsnet_sidecar.current_platform()
    assert raised.value.code == "SIDECAR_PLATFORM_UNSUPPORTED"


def test_windows_install_without_pin_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Support and trust stay separate: a supported platform whose pin is
    # absent must still fail closed before any fetch.
    monkeypatch.delitem(tsnet_sidecar.SIDECAR_SHA256, "windows-amd64")
    fetched = False

    def fetch(*_args, **_kwargs) -> None:
        nonlocal fetched
        fetched = True

    monkeypatch.setattr(tsnet_sidecar, "fetch_sidecar_asset", fetch)
    with pytest.raises(tsnet_sidecar.SidecarError) as raised:
        tsnet_sidecar.install_sidecar(tmp_path, confirm=lambda _plan: True)
    assert raised.value.code == "SIDECAR_PIN_MISSING"
    assert fetched is False
    assert not tsnet_sidecar.sidecar_binary_path(tmp_path).exists()


def test_windows_local_release_install_lands_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"locally built windows sidecar fixture"
    asset = tsnet_sidecar.sidecar_asset_name()
    release_root = tmp_path / "release"
    release = release_root / tsnet_sidecar.sidecar_tag()
    release.mkdir(parents=True)
    (release / asset).write_bytes(content)
    monkeypatch.setenv(
        tsnet_sidecar.SIDECAR_RELEASE_BASE_URL_ENV, release_root.as_uri()
    )
    monkeypatch.setitem(
        tsnet_sidecar.SIDECAR_SHA256,
        "windows-amd64",
        hashlib.sha256(content).hexdigest(),
    )

    result = tsnet_sidecar.install_sidecar(tmp_path, confirm=lambda _plan: True)

    destination = tmp_path / "bin" / "hyprial-tsnet.exe"
    assert destination.read_bytes() == content
    assert result["destination"] == str(destination)
    assert result["source"].endswith(f"/{tsnet_sidecar.sidecar_tag()}/{asset}")


def test_windows_candidate_flows_through_forwarding_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state" / "tsnet" / "node").mkdir(parents=True)
    (home / "profile.json").write_text(
        json.dumps(
            {
                "version": 1,
                "org": "windows-ci",
                "issuer": "https://auth.test",
                "clientId": "test-client",
                "controlPlane": {"kind": "headscale", "url": "https://head.test"},
                "join": "preauthkey",
            }
        ),
        encoding="utf-8",
    )
    candidate = tmp_path / "hyprial-tsnet-windows-amd64.exe"
    candidate.write_bytes(b"candidate")
    candidate.chmod(0o755)
    result = daemon_forwarding_environment(
        home,
        {
            "HYPRIAL_FORWARDING_SIDECAR": str(candidate),
            "HYPRIAL_FORWARDING_INBOUND_TARGET": "127.0.0.1:39111",
            "HYPRIAL_ZENOH_LISTEN": "tcp/127.0.0.1:39111",
        },
        node_id="windows-ci",
    )
    assert json.loads(result["HYPRIAL_FORWARDING_COMMAND"]) == [
        str(candidate),
        "forward",
    ]
