"""ParamSpec validation, the declared-spec registry and the retention spec."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.cells.params import (
    COMBINE_MODES,
    DELIVERY_BODY_RETENTION_MS,
    PARAM_REGISTRY,
    ParamRegistry,
    ParamSpec,
    ParamSpecError,
    declare_param,
)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def test_the_four_combine_modes_are_the_closed_set() -> None:
    assert COMBINE_MODES == frozenset({"unique", "min", "any_false", "collect"})


def test_the_retention_spec_is_declared_with_min_and_the_q9_clamp() -> None:
    spec = DELIVERY_BODY_RETENTION_MS
    assert spec.key == "delivery.bodyRetentionMs"
    assert spec.type == "int"
    assert spec.combine == "min"
    assert spec.default == 7 * DAY_MS == 604_800_000
    assert spec.minimum == HOUR_MS
    assert spec.maximum == 365 * DAY_MS
    assert PARAM_REGISTRY.get("delivery.bodyRetentionMs") is spec


def test_the_collect_combine_shape_is_constructible() -> None:
    spec = ParamSpec("custom.policy", "object", combine="collect")
    assert spec.default == ()
    assert spec.combine == "collect"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": "", "type": "int"},
        {"key": "bad key", "type": "int"},
        {"key": "a.b", "type": "float"},
        {"key": "a.b", "type": "int", "combine": "max"},
        {"key": "a.b", "type": "string", "combine": "min"},
        {"key": "a.b", "type": "int", "combine": "any_false"},
        {"key": "a.b", "type": "bool", "minimum": 1},
        {"key": "a.b", "type": "int", "combine": "min", "minimum": 5, "maximum": 4},
        {"key": "a.b", "type": "int", "combine": "unique", "default": 5, "maximum": 10},
        {"key": "a.b", "type": "int", "combine": "collect", "minimum": 1},
        {"key": "a.b", "type": "int", "combine": "min", "default": "7"},
        {"key": "a.b", "type": "int", "combine": "min", "default": True},
        {"key": "a.b", "type": "int", "combine": "min", "default": 1, "minimum": 2},
        {"key": "a.b", "type": "object", "combine": "collect", "default": ({},)},
    ],
)
def test_inconsistent_specs_are_refused(kwargs: dict) -> None:
    with pytest.raises(ParamSpecError):
        ParamSpec(**kwargs)


def test_the_registry_is_idempotent_for_equal_specs_and_refuses_redefinition() -> None:
    registry = ParamRegistry()
    first = registry.declare(
        ParamSpec("x.flag", "bool", combine="any_false", default=True)
    )
    assert (
        registry.declare(ParamSpec("x.flag", "bool", combine="any_false", default=True))
        is first
    )
    with pytest.raises(ParamSpecError):
        registry.declare(ParamSpec("x.flag", "bool", combine="unique", default=True))
    assert "x.flag" in registry
    assert registry.specs() == (first,)
    assert registry.get("x.other") is None


def test_declare_param_registers_in_the_process_registry() -> None:
    spec = declare_param(ParamSpec("tests.paramSpecProbe", "string", default="a"))
    assert PARAM_REGISTRY.get("tests.paramSpecProbe") is spec
    assert spec in PARAM_REGISTRY.specs()


def test_specs_with_object_defaults_are_hashable() -> None:
    spec = ParamSpec("x.obj", "object", default={"a": 1})
    assert hash(spec) == hash(ParamSpec("x.obj", "object", default={"a": 1}))
