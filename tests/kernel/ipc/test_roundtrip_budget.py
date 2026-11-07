"""The IPC patience is overridable so a delay distribution can be measured.

With a fixed deadline, every observation slower than it is indistinguishable
from a hang and the successes are right-censored.  ``HYPRIAL_IPC_ROUNDTRIP_SECONDS``
exists so a measurement can see past the default; it is not a tuning knob.
"""

from __future__ import annotations

import pytest

import hyprial.kernel.impl.ipc.client as client_module


def test_default_is_the_published_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", raising=False)
    assert (
        client_module._daemon_ipc_roundtrip_seconds()
        == client_module._DAEMON_IPC_ROUNDTRIP_SECONDS
    )


def test_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", "45")
    assert client_module._daemon_ipc_roundtrip_seconds() == 45.0


@pytest.mark.parametrize(
    "raw",
    [
        "0",          # a zero-timeout client reports every call as a timeout
        "-1",
        "abc",
        "",
        "nan",
        "inf",
        str(client_module._DAEMON_IPC_ROUNDTRIP_MAX_SECONDS + 1),
    ],
)
def test_bad_values_fall_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", raw)
    assert (
        client_module._daemon_ipc_roundtrip_seconds()
        == client_module._DAEMON_IPC_ROUNDTRIP_SECONDS
    )


def test_resolved_per_call_not_per_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default argument must not freeze the value at import time."""

    monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", "21")
    assert client_module._daemon_ipc_roundtrip_seconds() == 21.0
    monkeypatch.setenv("HYPRIAL_IPC_ROUNDTRIP_SECONDS", "22")
    assert client_module._daemon_ipc_roundtrip_seconds() == 22.0
