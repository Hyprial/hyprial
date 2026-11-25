"""The launcher keeps no chat adapter, logout coordinator or lifecycle shims."""

import importlib

import pytest


@pytest.mark.parametrize("name", [
    "hyprial.shell.impl.gui.adapter.chat",
    "hyprial.shell.impl.gui.adapter.host",
    "hyprial.shell.impl.gui.adapter.errors",
])
def test_chat_adapter_is_gone(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


@pytest.mark.parametrize("name", [
    "hyprial.shell.impl.gui.backend.onboarding",
    "hyprial.shell.impl.gui.backend.transport.replacement",
    "hyprial.shell.impl.gui.backend.transport.client",
    "hyprial.shell.impl.gui.backend.transport.probe",
])
def test_launcher_has_no_logout_coordinator(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


def test_launcher_session_cannot_replace_or_stop_a_backend_generation():
    from hyprial.shell.impl.gui.backend import lifecycle

    for name in ("replace_backend", "stop_backend_generation"):
        assert not hasattr(lifecycle.GuiSession, name)
    assert not hasattr(lifecycle, "StopProof")


def test_shell_public_face_has_no_gui_lifecycle():
    import hyprial.shell as shell

    for name in (
        "gui_status",
        "start_gui_background",
        "stop_gui",
        "perform_gui_action",
        "resolve_gui_invocation",
        "upgrade_gui",
    ):
        assert name not in shell.__all__
        assert not hasattr(shell, name)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.shell.impl.gui.runtime")


def test_gui_apps_keeps_only_the_upgrade_placeholder():
    from hyprial.shell.impl.gui import apps

    assert not hasattr(apps, "perform")
    assert not hasattr(apps, "resolve_invocation")


def test_cli_services_carry_no_gui_background_start():
    from hyprial.shell.impl.cli.commands.common.services import CliServices

    assert "start_gui_background" not in CliServices.__dataclass_fields__


@pytest.mark.parametrize("name", [
    "hyprial.shell.impl.gui.backend.transport",
    "hyprial.shell.impl.gui.backend.transport.binding",
    "hyprial.shell.impl.gui.backend.lifecycle.rebind",
])
def test_launcher_has_no_request_http_transport_binding_or_rebind(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


@pytest.mark.parametrize("name", [
    "TransportExpectation",
    "request_http_argv",
    "request_http_environment",
    "request_http_log_path",
    "write_request_http_config",
    "load_request_http_authority",
    "_verify_transport_document",
    "_assert_no_live_request_http",
])
def test_launcher_source_has_no_request_http_child_helper(name):
    from hyprial.shell.impl.gui.backend import source

    assert not hasattr(source, name)
    assert name not in getattr(source, "__all__", ())


def test_launcher_lifecycle_waits_for_no_request_http_capability():
    from hyprial.shell.impl.gui.backend import lifecycle
    from hyprial.shell.impl.gui.backend.lifecycle import process

    assert not hasattr(process, "_wait_for_capability")
    assert not hasattr(lifecycle, "_wait_for_capability")
    assert "transport" not in lifecycle.GuiSession.__dataclass_fields__


def test_request_http_entry_has_no_gui_child_serve():
    from hyprial.shell.impl import request_http
    from hyprial.shell.impl.request_http.bootstrap import gui

    for name in ("_gui_host", "_gui_home"):
        assert not hasattr(request_http, name)
    assert not hasattr(gui, "serve_gui")
    for name in ("watch_principal", "LiveHost", "reopen", "local_gui_config", "open_gui_host"):
        assert hasattr(gui, name)


def test_the_host_has_no_file_authority_reader():
    import dataclasses
    from pathlib import Path

    import hyprial
    from hyprial.shell.impl.gui.host import config

    assert not hasattr(config, "AuthorityReader")
    assert not hasattr(config, "AUTHORITY_MAX_BYTES")
    assert "authority_path" not in {field.name for field in dataclasses.fields(config.HostConfig)}
    retired = "HYPRIAL_GUI_" + "AUTHORITY_PATH"
    holders = [
        str(path)
        for path in Path(hyprial.__file__).parent.rglob("*.py")
        if retired in path.read_text(encoding="utf-8")
    ]
    assert holders == []
