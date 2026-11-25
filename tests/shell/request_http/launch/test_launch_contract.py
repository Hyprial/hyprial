"""Launch surface of the request-http child: what it no longer accepts or publishes."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http import (
    RequestHttpConfig,
    RequestHttpServer,
    serve,
)
from hyprial.shell.impl.request_http import __main__ as main_module
from hyprial.shell.impl.request_http.config import Budgets

OWNER = "native-host-owner"
ACTOR = f"user:{OWNER}"
NONCE = "0123456789abcdef0123456789abcdef"
ROOT = Path(__file__).resolve().parents[4]


def _config(tmp_path: Path) -> RequestHttpConfig:
    return RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"),
        owner=OWNER,
        actor=ACTOR,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=tmp_path / "runtime",
        source=tmp_path / "config.json",
    )


def _write_config(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    for directory in (state, runtime):
        directory.mkdir(mode=0o700, exist_ok=True)
    path = tmp_path / "request-http.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "backend": {"kind": "local-daemon", "socket": str(state / "daemon.sock")},
                "principal": {"kind": "local-operator", "owner": OWNER, "actor": ACTOR},
                "listen": {"host": "127.0.0.1", "port": 0},
                "runtime": {"dir": str(runtime)},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _entries() -> list[dict]:
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = "available" if entry["httpExposed"] else "disabled"
        item["callable"] = item["state"] == "available"
        item["reason"] = None if item["state"] == "available" else "SERVICE_NOT_READY"
        entries.append(item)
    return entries


def _fake():
    def request_once(method: str, params: dict, **_kwargs):
        if method == "ping":
            return {
                "version": __version__,
                "nodeId": "native-host-node",
                "owner": OWNER,
                "epoch": "epoch-1",
                "phase": "serving",
                "restorePending": False,
            }
        return {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": __version__,
            "methods": _entries(),
        }

    return request_once


def _read_server_json(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "runtime" / "server.json").read_text(encoding="utf-8")
    )


def test_server_json_omits_launch_nonce_without_launch_ownership(tmp_path: Path) -> None:
    server = RequestHttpServer(_config(tmp_path), request_once=_fake())
    server.start()
    try:
        document = _read_server_json(tmp_path)
    finally:
        server.shutdown()
    assert "launchNonce" not in document


def test_serve_builds_one_server_without_a_host_for_a_operator_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    constructor: dict = {}
    served: dict = {}

    class FakeServer:
        def __init__(self, config, **kwargs):
            constructor.update(kwargs=kwargs)

    def fake_serve_server(server):
        served["server"] = server
        return 0

    monkeypatch.setattr("hyprial.shell.impl.request_http.RequestHttpServer", FakeServer)
    monkeypatch.setattr("hyprial.shell.impl.request_http._serve_server", fake_serve_server)
    assert serve(_write_config(tmp_path)) == 0
    assert constructor == {"kwargs": {}}
    assert isinstance(served["server"], FakeServer)


def test_native_host_key_is_refused_as_unknown(tmp_path: Path) -> None:
    from hyprial.shell.impl.request_http.config import ConfigFailure, load_config

    path = _write_config(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["nativeHost"] = {"promotion": "external", "contractVersion": 1}
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigFailure, match="unknown keys"):
        load_config(path)


def test_catalog_has_no_host_contract(tmp_path: Path) -> None:
    server = RequestHttpServer(_config(tmp_path), request_once=_fake())
    server.start()
    try:
        document = _read_server_json(tmp_path)
        envelope = server._catalog("no-host-contract", time.monotonic())
    finally:
        server.shutdown()
    for payload in (document, envelope.payload):
        assert "hostContract" not in payload
        assert "hostContractVersion" not in payload
    contract = json.loads(
        (ROOT / "contract" / "http-request" / "http-contract.v1.json").read_text(
            encoding="utf-8"
        )
    )
    catalog = contract["catalogResponse"] if "catalogResponse" in contract else contract
    text = json.dumps(catalog)
    assert "hostContract" not in text
    assert not (ROOT / "contract" / "native-host").exists()


def test_external_promotion_modules_are_gone() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.shell.impl.request_http.bootstrap.external")
    assert not hasattr(
        importlib.import_module("hyprial.shell.impl.request_http.catalog"),
        "host_contract_document",
    )
    assert "nativeHost" not in importlib.import_module(
        "hyprial.shell.impl.request_http.config"
    ).CONFIG_KEYS


def test_launch_nonce_flag_is_an_argparse_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as usage:
        main_module.main(
            ["serve", "--config", str(_write_config(tmp_path)), "--launch-nonce-stdin"]
        )
    assert usage.value.code == 2


def test_the_launch_nonce_and_owner_stop_channel_are_gone(tmp_path: Path) -> None:
    import inspect

    assert not hasattr(main_module, "watch_launch_channel")
    assert list(inspect.signature(serve).parameters) == ["config_path"]
    with pytest.raises(TypeError):
        RequestHttpServer(_config(tmp_path), request_once=_fake(), launch_nonce=NONCE)
    assert not hasattr(RequestHttpServer(_config(tmp_path), request_once=_fake()), "launch_nonce")


def test_bootstrap_subcommand_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as usage:
        main_module.main(["bootstrap", "--home", str(tmp_path / "home")])
    assert usage.value.code == 2


def test_a_local_bootstrap_principal_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    from hyprial.shell.impl.request_http.server import StartupRefusal

    config = replace(
        _config(tmp_path), principal_kind="local-bootstrap", owner=None, actor=None
    )
    with pytest.raises(StartupRefusal) as refusal:
        RequestHttpServer(config, request_once=_fake())
    assert refusal.value.code == "UNSUPPORTED_PRINCIPAL"


def test_the_ownerless_http_login_host_is_gone() -> None:
    import importlib

    import hyprial.daemon as daemon_face

    assert not hasattr(daemon_face, "BootstrapHttpHost")
    assert "BootstrapHttpHost" not in daemon_face.__all__
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.daemon.impl.application.onboarding.http")
    package = importlib.import_module("hyprial.shell.impl.request_http.bootstrap")
    assert not hasattr(package, "bootstrap_http")
    assert not hasattr(package, "serve_bootstrap")
    assert not (ROOT / "contract" / "wire" / "daemon-login-http-bootstrap.md").exists()


def test_the_http_post_reply_response_is_gone() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.shell.impl.request_http.bootstrap.response")
    app_module = importlib.import_module("hyprial.shell.impl.request_http.app")
    assert not hasattr(app_module, "after_reply_response")
    assert not hasattr(app_module, "InitializeResponse")
