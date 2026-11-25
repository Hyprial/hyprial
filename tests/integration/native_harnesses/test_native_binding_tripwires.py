"""Binding and old-root classification controls for the native inventory."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .resident_scan import (
    _DIR,
    _GETTER,
    _JOIN_PATTERN,
    _LOGIN_PATTERN,
    native_scan_sources as _scan_sources,
)
from .test_native_resident_tripwires import NATIVE_ORIGINS


@pytest.mark.parametrize(
    "path",
    [
        "daemon/impl/configuration/network_profile.py",
        "daemon/impl/forwarding_config.py",
        "daemon/impl/network/tailcat/binary.py",
        "daemon/impl/network/tailcat/keys.py",
    ],
)
def test_original_root_network_providers_are_not_reclassified_as_old_resident(path):
    role, origins = NATIVE_ORIGINS[path]
    assert role == "other-original"
    assert all(not origin.startswith("src/hyprial/daemon/") for origin in origins)


def test_public_literal_map_is_binding_but_mutated_getter_is_implementation():
    prefix = "_FACADE_EXPORTS = {'run_join': ('hyprial.daemon.impl.network.tsnet_join', 'run_join')}\n__all__ = ['run_join']\n"
    face = prefix + ast.unparse(_GETTER) + "\n" + ast.unparse(_DIR)
    path = "daemon/interface.py"
    assert _scan_sources({path: face}, _JOIN_PATTERN) == {}
    assert _scan_sources({path: face + "\nrun_join()\n"}, _JOIN_PATTERN) == {path: [1]}
    disguised = face.replace("provider = import_module(module_path)", "provider = run_join()")
    assert _scan_sources({path: disguised}, _JOIN_PATTERN) == {path: [1]}
    assert _scan_sources({path: face.replace("__all__ = ['run_join']", "__all__ = []")}, _JOIN_PATTERN) == {path: [1]}


def test_known_binding_namespace_cannot_grow_runtime_auth_code():
    path = "daemon/impl/network/__init__.py"
    assert _scan_sources({path: "from .tsnet_join import run_join\n"}, _JOIN_PATTERN) == {}
    assert _scan_sources({path: "from .tsnet_join import run_join\nrun_join()\n"}, _JOIN_PATTERN) == {path: [1]}
    assert _scan_sources({path: "from .tsnet_join import *\n"}, _JOIN_PATTERN) == {path: [1]}


@pytest.mark.parametrize("pattern,payload", [
    (_LOGIN_PATTERN, "import hyprial.login"), (_LOGIN_PATTERN, "from hyprial import login"),
    (_LOGIN_PATTERN, "from hyprial.shell.impl import login as auth"),
    (_LOGIN_PATTERN, "from hyprial.shell.impl.login.oidc import _origin as alias"),
    (_LOGIN_PATTERN, "from hyprial.shell.impl.login.flow import LoginError as failure"),
    (_LOGIN_PATTERN, "def resident():\n    return open('login.json').read()"),
    (_LOGIN_PATTERN, "def resident():\n    return SECRETS_DIRNAME"),
    (_LOGIN_PATTERN, "def resident():\n    return refresh_access_token()"),
    (_LOGIN_PATTERN, "def resident():\n    return run_login()"),
    (_JOIN_PATTERN, "import hyprial.daemon.impl.network.tsnet_join"),
    (_JOIN_PATTERN, "import hyprial.daemon.impl.network.tsnet_sidecar"),
    (_JOIN_PATTERN, "def resident():\n    return open('node.json').read()"),
    (_JOIN_PATTERN, "def resident():\n    return TSNET_STATE_DIRNAME"),
    (_JOIN_PATTERN, "def resident():\n    return install_sidecar()"),
])
def test_all_original_forbidden_families_and_native_aliases_are_rejected(pattern, payload):
    path = "daemon/impl/ipc/router.py"
    assert path in _scan_sources({path: payload}, pattern)


@pytest.mark.parametrize("pattern,source", [
    (_LOGIN_PATTERN, "CREDENTIAL = 'login.json'\ndef resident(home):\n    return (home / CREDENTIAL).read_text()"),
    (_JOIN_PATTERN, "DISPLAY = 'node.json'\ndef resident(home):\n    return (home / DISPLAY).read_text()"),
    (_LOGIN_PATTERN, "def resident(home):\n    return open('login.' + 'json').read()"),
    (_JOIN_PATTERN, "def resident(home):\n    return open('node.' + 'json').read()"),
    (_LOGIN_PATTERN, "def resident(auth):\n    return getattr(auth, 'run_' + 'login')()"),
    (_JOIN_PATTERN, "def resident(auth):\n    return getattr(auth, 'install_' + 'sidecar')()"),
])
def test_literal_alias_and_concatenation_cannot_disguise_implementation(pattern, source):
    assert _scan_sources({"daemon/impl/ipc/router.py": source}, pattern)


@pytest.mark.parametrize("pattern,name", [(_LOGIN_PATTERN, "SECRETS_DIRNAME"), (_JOIN_PATTERN, "TSNET_STATE_DIRNAME")])
def test_a_literal_binding_cannot_hide_a_different_nonliteral_store(pattern, name):
    source = f"{name} = 'metadata'\ndef resident():\n    {name} = opaque_path()\n"
    assert _scan_sources({"daemon/impl/ipc/router.py": source}, pattern) == {"daemon/impl/ipc/router.py": [3]}


@pytest.mark.parametrize("pattern", [_LOGIN_PATTERN, _JOIN_PATTERN])
def test_nonsecret_unused_literal_metadata_is_not_implementation(pattern):
    source = '"""References login.json and node.json, not executable code."""\nSECRETS_DIRNAME = "secrets"\nNOTE = "node.json"\nUNUSED = None\n'
    assert _scan_sources({"daemon/impl/ipc/router.py": source}, pattern) == {}


@pytest.mark.parametrize("path", ["daemon/impl/ipc/new_reader.py", "daemon/impl/network/new_join.py", "daemon/impl/orgfs/new_auth.py",
                                  "kernel/impl/facts/new_reader.py", "kernel/impl/processes/new_join.py"])
def test_new_unclassified_files_fail_closed_even_in_other_original_clusters(path):
    assert _scan_sources({path: "VALUE = 1\n"}, _LOGIN_PATTERN) == {path: [1]}


_CLUSTERS = {}
for _path, (_role, _) in sorted(NATIVE_ORIGINS.items()):
    if _role == "resident":
        _CLUSTERS.setdefault(str(Path(_path).parent), _path)


@pytest.mark.parametrize("path", list(_CLUSTERS.values()), ids=list(_CLUSTERS))
@pytest.mark.parametrize("pattern,payload", [(_LOGIN_PATTERN, "read_login_credential()"), (_JOIN_PATTERN, "run_join()")], ids=["auth", "sidecar"])
def test_each_native_resident_cluster_rejects_forbidden_implementation(path, pattern, payload):
    mutated = "def resident_entry():\n    return " + payload + "\n"
    assert _scan_sources({path: mutated}, pattern) == {path: [2]}
