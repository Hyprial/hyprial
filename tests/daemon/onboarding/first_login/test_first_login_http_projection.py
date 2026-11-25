"""``waitSeconds`` is honoured on the daemon's own wire only, never over HTTP.

Decision: daemon-first-z5-first-login-tests-20261118.  The GUI's forwarding host
holds its own lock while it forwards a call, so a parked ``auth.operation.get``
sent by a browser would block every GUI call.  ``waitSeconds`` is therefore
``x-hyprial-http-forbidden``: the HTTP projection of the params has no such
property and refuses it, while the IPC schema keeps accepting it.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    AUTH_OPERATION_GET_SPEC,
)
from hyprial.kernel.impl.contracts.daemon.http_params import project_http_params_schema

PARAMS = {"operationId": "op-0123456789abcdef"}


def test_the_ipc_schema_accepts_wait_seconds():
    Draft202012Validator(AUTH_OPERATION_GET_SPEC.params_schema).validate(
        {**PARAMS, "waitSeconds": 30}
    )


def test_the_http_projection_has_no_wait_seconds_and_refuses_it():
    projected = project_http_params_schema(AUTH_OPERATION_GET_SPEC.params_schema)

    assert "waitSeconds" not in projected["properties"]
    validator = Draft202012Validator(projected)
    validator.validate(PARAMS)
    with pytest.raises(Exception):
        validator.validate({**PARAMS, "waitSeconds": 30})
