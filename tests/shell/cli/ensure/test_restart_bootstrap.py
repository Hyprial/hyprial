"""``daemon restart`` reads a bootstrap daemon's status from ``ping`` (KP-084).

Decision: daemon-first-z4-restart-bootstrap-20261117.
"""

from __future__ import annotations

import pytest

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.daemon import restart

from .conftest import refusal

BOOTSTRAP_PING = {"running": True, "phase": "bootstrap", "pid": 4242, "epoch": "e1", "version": "1.0"}
SERVING_PING = {"running": True, "phase": "serving", "pid": 4242}


@pytest.mark.parametrize("code", ["ACCOUNT_REQUIRED", ipc_errors.METHOD_NOT_FOUND, ipc_errors.CALLER_NOT_AUTHORIZED])
def test_a_bootstrap_daemon_yields_a_status_the_restart_gate_can_read(ensure_env, code):
    ensure_env.script = [refusal(code), BOOTSTRAP_PING]

    status = restart._restart_status()

    assert status is not None
    assert status["daemon"]["phase"] == "bootstrap" and status["daemon"]["pid"] == 4242
    assert status["agents"] == [] and status["connectors"] == []
    assert restart._interrupted_turns(status) == []
    assert restart._daemon_restart_identity(status)["pid"] == 4242


def test_a_serving_daemon_that_refuses_ps_is_not_taken_for_bootstrap(ensure_env):
    ensure_env.script = [refusal(ipc_errors.METHOD_NOT_FOUND, "no such method"), SERVING_PING]

    with pytest.raises(ensure_env.services.CliError) as raised:
        restart._restart_status()

    assert raised.value.code == ipc_errors.METHOD_NOT_FOUND


def test_the_identity_names_the_bootstrap_phase_only_for_a_bootstrap_daemon():
    assert restart._daemon_restart_identity({"daemon": BOOTSTRAP_PING})["phase"] == "bootstrap"
    assert "phase" not in restart._daemon_restart_identity({"daemon": SERVING_PING})
