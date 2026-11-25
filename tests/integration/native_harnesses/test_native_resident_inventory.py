"""Exact resident inventory and fail-closed scan regression checks."""

from pathlib import Path

import pytest

from .resident_metadata import (
    HISTORICAL_ROWS,
    _SERVICE_CONNECT_RESIDENT_ORIGIN,
    _SERVICE_CONNECT_RESIDENT_STUBS,
)
from .resident_scan import (
    _LOGIN_PATTERN,
    native_scan_sources as _scan_sources,
    native_resident_offenders as resident_offenders,
)
from .test_native_resident_tripwires import NATIVE_ORIGINS


def test_daemon_inventory_matches_live_tree() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "hyprial"
    live = {path.relative_to(root).as_posix() for path in (root / "daemon").rglob("*.py")}
    declared = {path for path in NATIVE_ORIGINS if path.startswith("daemon/")}
    daemon_historical = {path for path in HISTORICAL_ROWS if path.startswith("daemon/")}
    assert HISTORICAL_ROWS <= set(NATIVE_ORIGINS)
    assert all(not (root / path).exists() for path in HISTORICAL_ROWS)
    assert declared - daemon_historical == live


def test_service_connect_stubs_have_exact_resident_manifest_role() -> None:
    # The P0 stubs are filled by the slices; they keep their exact resident
    # role (so the strict resident scan applies) and must still exist.
    source_root = Path(__file__).resolve().parents[3] / "src" / "hyprial"
    for path in _SERVICE_CONNECT_RESIDENT_STUBS:
        assert NATIVE_ORIGINS[path] == ("resident", _SERVICE_CONNECT_RESIDENT_ORIGIN)
        assert (source_root / path).is_file()


def test_service_connect_manifest_stays_fail_closed() -> None:
    path = "daemon/impl/service_connect/unclassified.py"
    assert _scan_sources({path: ""}, _LOGIN_PATTERN) == {path: [1]}


def test_missing_native_resident_file_cannot_reduce_the_tripwire(tmp_path):
    missing = tmp_path / "hyprial/daemon/impl/ipc/router.py"
    assert missing in resident_offenders(tmp_path, _LOGIN_PATTERN)


@pytest.mark.parametrize(
    "path",
    [
        "daemon/impl/composition/agent_pins/__init__.py",
        "daemon/impl/composition/agent_pins/facade.py",
        "daemon/impl/request_catalog/adapter/pins.py",
        "daemon/impl/request_catalog/agent/authority/__init__.py",
    ],
)
def test_retired_pin_files_are_historical_rows_with_append_only_origins(path):
    assert path in HISTORICAL_ROWS
    assert path in NATIVE_ORIGINS
    assert not (Path(__file__).resolve().parents[3] / "src" / "hyprial" / path).exists()


@pytest.mark.parametrize("cluster", ["kernel/impl/facts", "kernel/impl/processes"])
def test_physical_new_lower_cluster_files_are_not_hidden_by_known_origin_paths(tmp_path, cluster):
    path = tmp_path / "hyprial" / cluster / "new_auth.py"
    path.parent.mkdir(parents=True)
    path.write_text("VALUE = 1\n")
    assert resident_offenders(tmp_path, _LOGIN_PATTERN)[path] == [1]


def test_gui_resident_modules_reject_login_credential_reads():
    for path in (
        'daemon/impl/harnesses/runtime/native_auth.py',
        'daemon/impl/request_catalog/agent/destroy/__init__.py',
        'daemon/impl/request_catalog/agent/destroy/specs.py',
        'daemon/impl/request_catalog/agent/destroy/examples.py',
        'daemon/impl/adapters/lark/credentials/chat_names.py',
        'daemon/impl/composition/agent_pins/__init__.py',
        'daemon/impl/composition/agent_pins/facade.py',
        'daemon/impl/operations/adapter_removal/__init__.py',
        'daemon/impl/operations/adapter_removal/journal.py',
        'daemon/impl/operations/adapter_removal/service.py',
        'daemon/impl/operations/adapter_removal/transaction.py',
        'daemon/impl/request_catalog/adapter/removal/__init__.py',
        'daemon/impl/request_catalog/adapter/removal/specs.py',
        'daemon/impl/request_catalog/adapter/removal/examples.py',
        'daemon/impl/adapters/lark/credentials/authorization/receipts.py',
        'daemon/impl/adapters/lark/credentials/onboard/__init__.py',
        'daemon/impl/adapters/lark/credentials/onboard/journal.py',
        'daemon/impl/adapters/lark/credentials/onboard/service.py',
        'daemon/impl/adapters/lark/credentials/onboard/worker.py',
        'daemon/impl/request_catalog/adapter/authorization.py',
        'daemon/impl/request_catalog/adapter/identities.py',
        'daemon/impl/request_catalog/adapter/registration.py',
        'daemon/impl/request_catalog/adapter/routes/__init__.py',
        'daemon/impl/request_catalog/adapter/routes/examples.py',
        'daemon/impl/request_catalog/agent/provider_auth/__init__.py',
    ):
        assert _scan_sources({path: "value = read_login_credential()\n"}, _LOGIN_PATTERN) == {path: [1]}


def test_identity_binding_request_modules_remain_resident_and_fail_closed() -> None:
    paths = [
        "daemon/impl/request_catalog/identity/binding/__init__.py",
        "daemon/impl/request_catalog/identity/binding/examples.py",
        "daemon/impl/request_catalog/identity/binding/specs.py",
        "daemon/impl/request_catalog/identity/binding/unclassified.py",
    ]
    for path in paths[:3]:
        assert NATIVE_ORIGINS[path][0] == "resident"
    assert _scan_sources({path: "value = read_login_credential()\n" for path in paths}, _LOGIN_PATTERN) == {
        path: [1] for path in paths
    }


def test_workflow_context_modules_are_resident_and_unknown_neighbors_fail_closed() -> None:
    paths = ["daemon/impl/request_catalog/workflow/__init__.py",
             "daemon/impl/request_catalog/workflow/examples.py",
             "daemon/impl/request_catalog/workflow/unclassified.py"]
    for path in paths[:2]:
        assert NATIVE_ORIGINS[path][0] == "resident"
    assert _scan_sources({path: "value = read_login_credential()\n" for path in paths}, _LOGIN_PATTERN) == {
        path: [1] for path in paths
    }
    assert _scan_sources({paths[2]: "value = 1\n"}, _LOGIN_PATTERN) == {paths[2]: [1]}


@pytest.mark.parametrize('filename', ['__init__.py', 'specs.py', 'examples.py'])
def test_grant_simulation_catalog_modules_remain_resident(filename):
    path = f'daemon/impl/request_catalog/agent/grant_simulation/{filename}'
    assert NATIVE_ORIGINS[path] == ('resident', ('new-module: grant simulation daemon requests',))
    assert _scan_sources({path: 'value = read_login_credential()\n'}, _LOGIN_PATTERN) == {path: [1]}


def test_unknown_grant_simulation_catalog_module_is_not_exempt():
    path = 'daemon/impl/request_catalog/agent/grant_simulation/unreviewed.py'
    assert _scan_sources({path: 'value = 1\n'}, _LOGIN_PATTERN) == {path: [1]}
    assert _scan_sources({path: 'value = read_login_credential()\n'}, _LOGIN_PATTERN) == {path: [1]}

@pytest.mark.parametrize('filename', ['__init__.py', 'examples.py'])
def test_pac_gc_preview_catalog_modules_remain_resident(filename):
    path = f'daemon/impl/request_catalog/pac_gc/{filename}'
    assert NATIVE_ORIGINS[path] == ('resident', ('new-module: PAC GC preview request',))
    assert _scan_sources({path: 'value = read_login_credential()\n'}, _LOGIN_PATTERN) == {path: [1]}


def test_unknown_pac_gc_catalog_module_is_not_exempt():
    path = 'daemon/impl/request_catalog/pac_gc/unreviewed.py'
    assert _scan_sources({path: 'value = 1\n'}, _LOGIN_PATTERN) == {path: [1]}
    assert _scan_sources({path: 'value = read_login_credential()\n'}, _LOGIN_PATTERN) == {path: [1]}


@pytest.mark.parametrize("path,origin", [
    ("daemon/impl/harnesses/model_routes/__init__.py", "new-module: configured model service launch admission"),
    ("daemon/impl/request_catalog/setup/routes.py", "new-module: configured model service operator requests"),
])
def test_model_service_modules_remain_resident(path, origin):
    assert NATIVE_ORIGINS[path] == ("resident", (origin,))
    assert _scan_sources({path: "value = 1\n"}, _LOGIN_PATTERN) == {}
    assert _scan_sources({path: "value = read_login_credential()\n"}, _LOGIN_PATTERN) == {path: [1]}


@pytest.mark.parametrize("module", ["harnesses/model_routes", "request_catalog/setup"])
def test_unknown_model_service_neighbors_are_not_exempt(module):
    path = f"daemon/impl/{module}/unreviewed.py"
    assert _scan_sources({path: "value = 1\n"}, _LOGIN_PATTERN) == {path: [1]}


@pytest.mark.parametrize("path", [
    "daemon/impl/model_probe/__init__.py",
    "daemon/impl/model_probe/network.py",
    "daemon/impl/model_probe/request.py",
    "daemon/impl/request_catalog/setup/probe.py",
])
def test_temporary_model_probe_modules_are_resident_and_cannot_read_login(path):
    assert NATIVE_ORIGINS[path] == ("resident", ("new-module: temporary model credential probe",))
    assert _scan_sources({path: "value = 1\n"}, _LOGIN_PATTERN) == {}
    assert _scan_sources({path: "value = read_login_credential()\n"}, _LOGIN_PATTERN) == {path: [1]}


def test_unknown_model_probe_module_remains_fail_closed():
    path = "daemon/impl/model_probe/unreviewed.py"
    assert _scan_sources({path: "value = 1\n"}, _LOGIN_PATTERN) == {path: [1]}
