"""The scripted answers are the daemon's published shapes; the literals are the daemon's constants.

Decision: daemon-first-z5-cli-contract-20261119.  ``cli/commands/login/first_login.py``
imports nothing from the daemon, kernel or identity domains (a new CLI backend
import is refused by the import ratchet), so the strings it compares with are
pinned here, to the declared schemas and error constants.
"""

from __future__ import annotations

import re
from pathlib import Path

import hyprial
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.onboarding import schemas
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.login import first_login

from .support import DEVICE_NOT_READY, DEVICE_READY, answer, begin_result

SRC = Path(hyprial.__file__).resolve().parent
FIRST_LOGIN = SRC / "shell" / "impl" / "cli" / "commands" / "login" / "first_login.py"
RELOGIN = SRC / "shell" / "impl" / "cli" / "commands" / "login" / "relogin.py"


def test_the_scripted_answers_validate_against_the_declared_schemas():
    Draft202012Validator(schemas.LOGIN_BEGIN_RESULT).validate(begin_result())
    for scripted in (
        answer(),
        answer(device=DEVICE_NOT_READY),
        answer(state="denied", stage="stopped", device=None, owner=None),
        answer(stage="stopped", failure={"code": "DAEMON_ERROR", "message": "stopped"}),
    ):
        Draft202012Validator(schemas.AUTH_GET_RESULT).validate(scripted)
    assert DEVICE_READY["retry"] is None and DEVICE_NOT_READY["retry"] == schemas.COMPLETION_RETRY


def test_the_published_wait_and_begin_parameters_exist_in_the_daemon_schemas():
    properties = schemas.LOGIN_BEGIN_PARAMS["properties"]
    assert properties["until"]["enum"] == ["authenticated", "serving"]
    wait = schemas.AUTH_GET_PARAMS["properties"]["waitSeconds"]
    assert wait["maximum"] == first_login.WAIT_SECONDS == 900
    assert first_login.REQUEST_SECONDS > first_login.WAIT_SECONDS


def test_the_states_stages_and_cancel_reason_are_the_daemons():
    assert set(first_login._FAILED_STATES) <= set(schemas.AUTH_STATES)
    assert set(first_login._TERMINAL_STAGES) <= set(schemas.COMPLETION_STAGES)
    assert "identity-committed" in schemas.CANCEL_REASONS
    assert first_login.SUBMIT_OUTCOME_UNKNOWN == ipc_errors.SUBMIT_OUTCOME_UNKNOWN


def test_the_refusal_codes_are_the_ones_the_identity_flow_raises():
    codes = {code for code, _message in first_login._FAILED_STATES.values()}
    runtime = (SRC / "daemon" / "impl" / "onboarding" / "runtime.py").read_text(encoding="utf-8")
    identity_lifecycle = (
        SRC / "daemon" / "impl" / "onboarding" / "lifecycle" / "identity.py"
    ).read_text(encoding="utf-8")
    surface = runtime + "\n" + identity_lifecycle
    for code in codes - {"CANCELLED"}:
        assert f'"{code}"' in surface
    assert "CANCELLED" in codes


def test_the_module_imports_no_backend_domain():
    for path in (FIRST_LOGIN, RELOGIN):
        source = path.read_text(encoding="utf-8")
        assert re.findall(r"^\s*(?:from|import)\s+hyprial\.(?:kernel|identity|daemon)\b", source, re.M) == []
