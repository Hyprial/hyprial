# Decisions:
# - Phase D, package T2: the journal-only helpers (client-id registration and
#   the update-blob recovery spies) are removed with the tests that used them;
#   the remaining helpers are runtime-agnostic loopback plumbing.
"""Loopback production-entry coverage for the P1 mesh rework."""

from __future__ import annotations

import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.daemon.impl.transport import (
    KeySpace,
    LivelinessDirectory)
from hyprial.kernel.impl.primitives.uri import TARGET_KIND_HOST


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (OrgFsError, AssertionError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_true(value):
    assert value
    return value


def _assert_equal(actual, expected):
    assert actual == expected
    return actual


def _bind_with_daemon_presence(runtime, transport):
    """Use the same host-class + shared liveliness verdict as DaemonApplication."""

    presence = LivelinessDirectory(transport)
    runtime.bind_transport(
        transport,
        supplier_online=lambda supplier: (
            classify_target_identity(supplier) == TARGET_KIND_HOST
            and presence.actor_online(supplier)
        ),
        holder_candidates=lambda: tuple(
            actor
            for actor in presence.online_actors()
            if classify_target_identity(actor) == TARGET_KIND_HOST
        ),
    )
    token = transport.declare_liveliness(KeySpace().actor_liveliness(runtime.node_id))
    return presence, token
