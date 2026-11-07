"""Binding and old-root classification controls for the native inventory."""

from __future__ import annotations

import ast

import pytest

from .test_native_resident_tripwires import (
    NATIVE_ORIGINS,
    _DIR,
    _GETTER,
    _JOIN_PATTERN,
    _scan_sources,
)


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
