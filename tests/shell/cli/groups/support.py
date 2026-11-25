"""Shared fakes for the ``hyprial message group`` CLI tests.

The daemon request port is the scripted fake of the thread CLI tests
(``tests.shell.cli.threads.support.record``).  The answers are the request
catalog's own examples for the group methods, and every recorded call is
validated against the method's declared params schema, so these tests pin
the public shapes the daemon declares rather than shapes invented here.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
from typer.testing import CliRunner

from hyprial.daemon.impl.request_catalog.conversations import CONVERSATIONS_LIST_SPEC
from hyprial.daemon.impl.request_catalog.conversations.group_examples import (
    GROUP_EXAMPLES,
    GROUP_HEAD,
    GROUP_ID,
    REVIEWER_ID,
    WORKER_ID,
)
from hyprial.daemon.impl.request_catalog.conversations.groups import GROUP_BINDINGS
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from tests.shell.cli.threads.support import OPERATOR, record

SPECS = {spec.name: spec for spec, _handler in GROUP_BINDINGS}
SPECS[CONVERSATIONS_LIST_SPEC.name] = CONVERSATIONS_LIST_SPEC


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=dict))


def example(method: str, name: str) -> dict[str, Any]:
    """The result of the named success example of *method*, as plain JSON."""

    for example_set in GROUP_EXAMPLES:
        if example_set.method != method:
            continue
        for case in example_set.cases:
            if case.name == name and case.result is not None:
                return _plain(case.result)
    raise KeyError((method, name))


def declared_codes(method: str) -> list[str]:
    """Every error code *method* declares, once each, in declaration order."""

    return list(dict.fromkeys(error.code for error in SPECS[method].errors))


def assert_declared_params(calls: list[tuple[str, dict[str, Any]]]) -> None:
    """Each recorded call is valid against its method's params schema."""

    for method, params in calls:
        jsonschema.validate(params, _plain(SPECS[method].params_schema))


def invoke(*args: str) -> Any:
    return CliRunner().invoke(_cli_root_app, ["message", "group", *args])


__all__ = [
    "GROUP_HEAD",
    "GROUP_ID",
    "OPERATOR",
    "REVIEWER_ID",
    "SPECS",
    "WORKER_ID",
    "assert_declared_params",
    "declared_codes",
    "example",
    "invoke",
    "record",
]
