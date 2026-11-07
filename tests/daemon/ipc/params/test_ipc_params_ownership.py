"""One physical IPC helper owner, with unchanged parameter and peer-PID semantics."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.ipc import params
from hyprial.kernel import DaemonRequestError, ipc_errors

HELPERS = (
    "_required_string", "_optional_string_param", "_optional_positive_integer",
    "_optional_boolean", "_actor", "_peer_process_id",
)


def _tree() -> ast.Module:
    return ast.parse(Path(inspect.getsourcefile(params)).read_text())


@pytest.mark.parametrize("name", HELPERS)
def test_each_consumed_helper_has_one_physical_definition(name: str) -> None:
    definitions = [node for node in ast.walk(_tree())
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    assert len(definitions) == 1, (name, [node.lineno for node in definitions])


def test_json_object_has_one_explicit_definition_before_helpers() -> None:
    tree = _tree()
    aliases = [node for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "JsonObject"
                       for target in node.targets)]
    assert len(aliases) == 1
    assert aliases[0].lineno < min(node.lineno for node in tree.body if isinstance(node, ast.FunctionDef))
    assert params.JsonObject == dict[str, Any]


def test_future_import_and_type_checking_scaffolding_are_not_duplicated() -> None:
    tree = _tree()
    futures = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
    assert len(futures) == 1
    assert [name.name for name in futures[0].names] == ["annotations"]
    assert not any(isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                   and node.test.id == "TYPE_CHECKING" and all(isinstance(item, ast.Pass) for item in node.body)
                   for node in ast.walk(tree))


@pytest.mark.parametrize("helper,value,expected", [
    ("_required_string", "actor", "actor"),
    ("_required_string", " ", " "),
    ("_optional_string_param", None, None),
    ("_optional_string_param", "", None),
    ("_optional_string_param", " ", " "),
    ("_optional_string_param", "actor", "actor"),
    ("_optional_positive_integer", None, None),
    ("_optional_positive_integer", 1, 1),
    ("_optional_positive_integer", 2**64, 2**64),
    ("_optional_boolean", None, None),
    ("_optional_boolean", True, True),
    ("_optional_boolean", False, False),
])
def test_valid_parameter_values_keep_original_result(helper, value, expected) -> None:
    assert getattr(params, helper)(value, "field") == expected


@pytest.mark.parametrize("helper,values,detail", [
    ("_required_string", (None, "", 0, False, [], {}), "must be a non-empty string"),
    ("_optional_string_param", (0, False, [], {}), "must be a string when present"),
    ("_optional_positive_integer", (True, False, 0, -1, 1.0, "1", []), "must be a positive integer"),
    ("_optional_boolean", (0, 1, "true", [], {}), "must be a boolean"),
])
def test_invalid_parameters_keep_original_error_code_message_and_data(helper, values, detail) -> None:
    for value in values:
        with pytest.raises(DaemonRequestError) as caught:
            getattr(params, helper)(value, "field")
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
        assert str(caught.value) == f"field {detail}"
        assert caught.value.data is None


@pytest.mark.parametrize("envelope,expected", [
    ({"actor": "mcp", "from": "cli"}, "mcp"),
    ({"from": "cli"}, "cli"),
    ({"actor": "", "from": "cli"}, "cli"),
    ({"actor": None, "from": "cli"}, "cli"),
    ({"actor": 0, "from": "cli"}, "cli"),
])
def test_actor_keeps_mcp_priority_and_cli_truthy_fallback(envelope, expected) -> None:
    assert params._actor(envelope) == expected


@pytest.mark.parametrize("envelope", [{}, {"actor": ""}, {"actor": 1, "from": "cli"}])
def test_actor_invalid_identity_is_the_original_permanent_error(envelope) -> None:
    with pytest.raises(DaemonRequestError) as caught:
        params._actor(envelope)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(caught.value) == "actor must be a non-empty string"


class _CredentialSocket:
    def __init__(self, payload: bytes | OSError):
        self.payload = payload
        self.calls: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.calls.append((level, option, size))
        if isinstance(self.payload, OSError):
            raise self.payload
        return self.payload


@pytest.mark.parametrize("platform,credential,payload,expected", [
    ("linux", 91, struct.pack("3i", 123, 4, 5), 123),
    ("darwin", 91, struct.pack("3i", 123, 4, 5), 123),
    ("linux", 0, struct.pack("3i", 123, 4, 5), 123),
    ("linux", 91, struct.pack("3i", 0, 4, 5), None),
    ("linux", 91, struct.pack("3i", -1, 4, 5), None),
    ("darwin", 91, b"short", None),
    ("darwin", 91, OSError("unavailable"), None),
])
def test_peer_credentials_take_precedence_without_darwin_fallback(
    monkeypatch, platform, credential, payload, expected,
) -> None:
    # Rebind only the consumer's module globals, never shared socket/sys state.
    monkeypatch.setattr(params, "socket", SimpleNamespace(SOL_SOCKET=77, SO_PEERCRED=credential))
    monkeypatch.setattr(params, "sys", SimpleNamespace(platform=platform))
    connection = _CredentialSocket(payload)
    assert params._peer_process_id(connection) == expected
    assert connection.calls == [(77, credential, 12)]


@pytest.mark.parametrize("payload,expected", [
    (struct.pack("i", 123), 123), (struct.pack("i", 0), None),
    (struct.pack("i", -1), None), (b"short", None), (OSError("unavailable"), None),
])
def test_darwin_local_peerpid_keeps_positive_and_fail_closed_semantics(monkeypatch, payload, expected) -> None:
    monkeypatch.setattr(params, "socket", SimpleNamespace(SOL_SOCKET=77))
    monkeypatch.setattr(params, "sys", SimpleNamespace(platform="darwin"))
    connection = _CredentialSocket(payload)
    assert params._peer_process_id(connection) == expected
    assert connection.calls == [(0, 0x002, 4)]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_unsupported_peer_pid_is_none_without_any_socket_operation(monkeypatch, platform) -> None:
    monkeypatch.setattr(params, "socket", SimpleNamespace(SOL_SOCKET=77, SO_PEERCRED=None))
    monkeypatch.setattr(params, "sys", SimpleNamespace(platform=platform))
    connection = _CredentialSocket(OSError("must not be called"))
    assert params._peer_process_id(connection) is None
    assert connection.calls == []
