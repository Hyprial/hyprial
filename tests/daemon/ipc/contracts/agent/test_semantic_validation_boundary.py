"""A real legacy handler rejects schema-valid params after validation.

Decision record (M-03 rework, 2026-10-31)
- M-01/M-02 deliberately leave duration coercion, provider requiredness and
  combined-input order to the existing handlers.  Inputs such as
  ``inactiveSince=0`` or a malformed duration can pass the declared params
  schema and still produce ``INVALID_ARGUMENT``.  The example framework must
  not assume that schema validity means "no parameter error", and it must not
  strengthen the declaration to make today's accepted input invalid.
- This test uses the real ``link.list`` declaration and a real
  ``DaemonApplication`` handler.  A schema-valid but malformed ``from`` value
  reaches the handler and is rejected as ``INVALID_ARGUMENT``.  The same
  example set also carries a true schema-invalid negative, so the pre-dispatch
  control is preserved.
- The new ``semantic-param-error`` kind records this boundary explicitly.  The
  kind is the declaration; the error code is not used to infer the stage.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.request_catalog.link import LINK_LIST_SPEC
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
    validate_method_example_set,
)
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

ALICE = "agent:op:node-1:alice"
SEMANTIC = "not:an:agent"


class _UnusedLinkService:
    def list_links(self, actor: str) -> dict[str, Any]:
        raise AssertionError("the semantic rejection happens before the service call")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _example_set() -> MethodExampleSet:
    return MethodExampleSet(
        method=LINK_LIST_SPEC.name,
        cases=(
            MethodExample(
                name="listed",
                kind="success",
                description="the real result shape with no links",
                params={"from": ALICE},
                result={"ok": True, "actor": ALICE, "links": []},
            ),
            MethodExample(
                name="no-links",
                kind="empty",
                description="a canonical actor with no links",
                params={"from": ALICE},
                result={"ok": True, "actor": ALICE, "links": []},
                reason="an actor with no active links is a legal empty result",
            ),
            MethodExample(
                name="schema-invalid",
                kind="invalid-params",
                description="the true pre-dispatch negative control",
                params={"from": 5},
                error=MethodExampleError("INVALID_ARGUMENT", "from must be a string", {}),
            ),
            MethodExample(
                name="semantic-invalid",
                kind="semantic-param-error",
                description="schema-valid, but the handler rejects the actor form",
                params={"from": SEMANTIC},
                error=MethodExampleError(
                    "INVALID_ARGUMENT", "actor is not a bare name or canonical URI", {}
                ),
            ),
            MethodExample(
                name="not-ready",
                kind="domain-error",
                description="a schema-valid request with no wired link service",
                params={"from": ALICE},
                error=MethodExampleError("DAEMON_NOT_READY", "link service is not wired"),
            ),
        ),
    )


def test_the_real_declaration_expresses_both_validation_boundaries() -> None:
    validated = validate_method_example_set(LINK_LIST_SPEC, _example_set())
    assert [case.kind for case in validated] == [
        "success",
        "empty",
        "invalid-params",
        "semantic-param-error",
        "domain-error",
    ]


def test_the_true_schema_negative_still_fails_before_dispatch() -> None:
    validator = Draft202012Validator(_plain(LINK_LIST_SPEC.params_schema))
    assert not validator.is_valid({"from": 5})
    assert validator.is_valid({"from": SEMANTIC})


def test_the_real_handler_rejects_a_schema_valid_value(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "daemon-state",
            socket_path=tmp_path / "daemon.sock",
            node_id="node-1",
            owner="op",
            **_deep_module_daemon_dependencies(),
        )
    )
    app._link_service = _UnusedLinkService()  # type: ignore[attr-defined]
    params = {"from": SEMANTIC}
    assert Draft202012Validator(_plain(LINK_LIST_SPEC.params_schema)).is_valid(params)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, LINK_LIST_SPEC.name, params)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT


def test_the_real_agent_list_handler_rejects_schema_valid_durations(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "daemon-state",
            socket_path=tmp_path / "daemon.sock",
            node_id="node-1",
            owner="op",
            **_deep_module_daemon_dependencies(),
        )
    )
    for params in ({"inactiveSince": 0}, {"inactiveSince": "bogus"}):
        with pytest.raises(DaemonRequestError) as caught:
            app._handle_agent("agent.list", params)
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._handle_agent("agent.list", {"inactiveSince": 1}) == {
        "ok": True,
        "agents": [],
        "agentsWithoutRoutine": [],
    }
