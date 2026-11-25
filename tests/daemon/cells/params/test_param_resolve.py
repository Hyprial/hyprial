"""The pure resolver: combine modes, clamping, subjects and defaults."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.cells.params import (
    DELIVERY_BODY_RETENTION_MS,
    EMPTY_SNAPSHOT,
    ParamSource,
    ParamSpec,
    ParamSpecError,
    declare_param,
    resolve_param,
    resolve_param_detail,
)

from tests.daemon.cells.params.param_support import (
    AGENT_ID,
    AGENT_URI,
    OTHER_AGENT_ID,
    OTHER_AGENT_URI,
    agent,
    snapshot,
    source_snapshot,
)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
KEY = "delivery.bodyRetentionMs"
FLAG = declare_param(
    ParamSpec("tests.wakeHuman", "bool", combine="any_false", default=True)
)
MODE = declare_param(
    ParamSpec("tests.mode", "string", combine="unique", default="auto")
)
POLICY = declare_param(ParamSpec("tests.policy", "object", combine="collect"))
LIMIT = declare_param(ParamSpec("tests.limit", "int", combine="min", default=10))
OBJECT = declare_param(ParamSpec("tests.obj", "object", default={"a": [1]}))


def _multi_agent_snapshot(*param_maps: dict[str, object], subject: str = AGENT_URI):
    sources = tuple(
        ParamSource(f"c.agent-{index}", "agent", 1, frozenset({subject}), params)
        for index, params in enumerate(param_maps)
    )
    group = frozenset({subject, *(source.cell_id for source in sources)})
    return source_snapshot(*sources, aliases={subject: group})


def test_min_takes_the_smallest_trusted_value_for_the_subject() -> None:
    view = _multi_agent_snapshot(
        {KEY: 3 * DAY_MS},
        {KEY: 2 * DAY_MS},
        {KEY: 5 * DAY_MS},
    )
    detail = resolve_param_detail(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI)
    assert detail.value == 2 * DAY_MS
    assert not detail.defaulted
    assert len(detail.sources) == 3
    assert resolve_param(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 2 * DAY_MS


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (1, HOUR_MS),
        (HOUR_MS - 1, HOUR_MS),
        (HOUR_MS, HOUR_MS),
        (400 * DAY_MS, 365 * DAY_MS),
        (365 * DAY_MS, 365 * DAY_MS),
        (0, HOUR_MS),
        (-5, HOUR_MS),
    ],
)
def test_retention_is_clamped_to_one_hour_through_365_days(
    configured: int, expected: int
) -> None:
    view = snapshot(agent({KEY: configured}))
    detail = resolve_param_detail(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI)
    assert detail.value == expected
    assert detail.clamped == (configured != expected)


def test_retention_defaults_to_seven_days_without_sources() -> None:
    detail = resolve_param_detail(
        snapshot(agent()), DELIVERY_BODY_RETENTION_MS, AGENT_URI
    )
    assert detail.value == 604_800_000
    assert detail.defaulted
    assert detail.sources == ()


@pytest.mark.parametrize("bad", ["3600000", 3.6e6, True, None, [1], {"ms": 1}])
def test_wrongly_typed_values_are_ignored_not_coerced(bad: object) -> None:
    view = _multi_agent_snapshot({KEY: bad}, {KEY: 2 * DAY_MS})
    detail = resolve_param_detail(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI)
    assert detail.value == 2 * DAY_MS
    assert [reason for _, reason in detail.ignored] == ["type"]


def test_any_false_is_false_when_one_source_says_false() -> None:
    both = _multi_agent_snapshot({FLAG.key: True}, {FLAG.key: False})
    assert resolve_param(both, FLAG, AGENT_URI) is False
    only_true = snapshot(agent({FLAG.key: True}))
    assert resolve_param(only_true, FLAG, AGENT_URI) is True
    assert resolve_param(snapshot(), FLAG, AGENT_URI) is True


def test_unique_needs_every_source_to_agree_else_falls_back_with_conflict() -> None:
    agree = _multi_agent_snapshot({MODE.key: "strict"}, {MODE.key: "strict"})
    assert resolve_param(agree, MODE, AGENT_URI) == "strict"
    disagree = _multi_agent_snapshot({MODE.key: "strict"}, {MODE.key: "loose"})
    detail = resolve_param_detail(disagree, MODE, AGENT_URI)
    assert detail.value == "auto"
    assert detail.conflict
    assert detail.defaulted


def test_collect_returns_every_value_in_deterministic_source_order() -> None:
    view = _multi_agent_snapshot(
        {POLICY.key: {"window": 8}},
        {POLICY.key: {"pause": True}},
        {POLICY.key: {"window": 4}},
    )
    values = resolve_param(view, POLICY, AGENT_URI)
    assert values == ({"window": 8}, {"pause": True}, {"window": 4})
    assert all(type(value) is dict for value in values)
    assert resolve_param(snapshot(), POLICY, AGENT_URI) == ()


def test_a_subject_sees_only_its_own_agent_cell() -> None:
    view = snapshot(
        agent({LIMIT.key: 5}),
        agent({LIMIT.key: 1}, agent_id=OTHER_AGENT_ID, uri=OTHER_AGENT_URI),
    )
    assert resolve_param(view, LIMIT, AGENT_URI) == 5
    assert resolve_param(view, LIMIT, OTHER_AGENT_URI) == 1


def test_a_subject_may_be_named_by_uri_agent_id_or_agent_cell_id() -> None:
    cell = agent({LIMIT.key: 3})
    view = snapshot(cell)
    for name in (AGENT_URI, AGENT_ID, cell.cell_id):
        assert resolve_param(view, LIMIT, name) == 3, name
    assert resolve_param(view, LIMIT, "agent:alice:mac:nobody") == 10


def test_no_subject_means_every_trusted_source() -> None:
    view = snapshot(
        agent({LIMIT.key: 5}),
        agent({LIMIT.key: 2}, agent_id=OTHER_AGENT_ID, uri=OTHER_AGENT_URI),
    )
    assert resolve_param(view, LIMIT) == 2


def test_none_and_empty_snapshots_resolve_to_the_default() -> None:
    for unavailable in (None, EMPTY_SNAPSHOT):
        detail = resolve_param_detail(
            unavailable, DELIVERY_BODY_RETENTION_MS, AGENT_URI
        )
        assert detail.value == 604_800_000
        assert detail.defaulted
    assert resolve_param(None, POLICY, AGENT_URI) == ()


def test_object_defaults_are_copies_callers_cannot_mutate_the_spec() -> None:
    value = resolve_param(None, OBJECT)
    value["a"].append(2)
    assert resolve_param(None, OBJECT) == {"a": [1]}


def test_an_undeclared_spec_cannot_be_resolved() -> None:
    stray = ParamSpec("tests.neverDeclared", "int", combine="min", default=3)
    with pytest.raises(ParamSpecError, match="not declared"):
        resolve_param(snapshot(agent({stray.key: 1})), stray, AGENT_URI)
    with pytest.raises(ParamSpecError, match="not declared"):
        resolve_param(None, stray)


def test_a_spec_that_differs_from_the_declared_one_cannot_be_resolved() -> None:
    rival = ParamSpec(LIMIT.key, "int", combine="unique", default=10)
    with pytest.raises(ParamSpecError, match="differs from its declaration"):
        resolve_param_detail(snapshot(agent({LIMIT.key: 1})), rival, AGENT_URI)
    with pytest.raises(ParamSpecError, match="differs from its declaration"):
        resolve_param(None, rival)
