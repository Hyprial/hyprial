"""Bootstrap workflow vocabulary and stable YAML serialization (§4.1)."""

from __future__ import annotations

import yaml

import pytest

from hyprial.identity.impl.org_directory.bootstrap import (
    BOOTSTRAP_STEP_KINDS,
    BootstrapStep,
    bootstrap_yaml,
    default_bootstrap,
    parse_bootstrap_yaml,
)


def test_the_vocabulary_is_the_four_documented_kinds_in_order() -> None:
    assert BOOTSTRAP_STEP_KINDS == (
        "connect-inviter",
        "join-org",
        "register-device",
        "connect-members",
    )


def test_default_bootstrap_is_the_four_steps_in_order() -> None:
    assert [step.kind for step in default_bootstrap()] == list(BOOTSTRAP_STEP_KINDS)


def test_a_step_outside_the_vocabulary_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown bootstrap step kind"):
        BootstrapStep("format-disk")  # type: ignore[arg-type]


def test_yaml_round_trips_the_default_workflow() -> None:
    text = bootstrap_yaml(default_bootstrap())
    assert parse_bootstrap_yaml(text) == default_bootstrap()


def test_yaml_serialization_is_stable() -> None:
    first = bootstrap_yaml(default_bootstrap())
    second = bootstrap_yaml(default_bootstrap())
    assert first == second


def test_yaml_document_is_exactly_version_one_and_steps() -> None:
    document = yaml.safe_load(bootstrap_yaml(default_bootstrap()))
    assert set(document) == {"version", "steps"}
    assert document["version"] == 1
    assert document["steps"] == [{"kind": kind} for kind in BOOTSTRAP_STEP_KINDS]


def test_reordering_steps_survives_a_round_trip() -> None:
    steps = (
        BootstrapStep("join-org"),
        BootstrapStep("connect-inviter"),
    )
    assert parse_bootstrap_yaml(bootstrap_yaml(steps)) == steps


def test_empty_workflow_round_trips() -> None:
    assert parse_bootstrap_yaml(bootstrap_yaml(())) == ()


# -- what the parser refuses ------------------------------------------------------


def _valid_text() -> str:
    return bootstrap_yaml(default_bootstrap())


def test_parse_rejects_a_different_version() -> None:
    text = _valid_text().replace("version: 1", "version: 2")
    with pytest.raises(ValueError, match="version must be 1"):
        parse_bootstrap_yaml(text)


def test_parse_rejects_a_boolean_version() -> None:
    # ``True == 1`` in Python; the parser must not be fooled.
    with pytest.raises(ValueError, match="version must be 1"):
        parse_bootstrap_yaml("steps: []\nversion: true\n")


def test_parse_rejects_unknown_top_level_keys() -> None:
    with pytest.raises(ValueError, match="exactly 'version' and 'steps'"):
        parse_bootstrap_yaml("extra: 1\nsteps: []\nversion: 1\n")


def test_parse_rejects_missing_steps() -> None:
    with pytest.raises(ValueError, match="exactly 'version' and 'steps'"):
        parse_bootstrap_yaml("version: 1\n")


def test_parse_rejects_steps_that_are_not_a_list() -> None:
    with pytest.raises(ValueError, match="'steps' must be a list"):
        parse_bootstrap_yaml("steps: connect-inviter\nversion: 1\n")


def test_parse_rejects_a_step_with_extra_keys() -> None:
    with pytest.raises(ValueError, match="exactly 'kind'"):
        parse_bootstrap_yaml(
            "steps:\n- kind: join-org\n  target: everywhere\nversion: 1\n"
        )


def test_parse_rejects_a_step_outside_the_vocabulary() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        parse_bootstrap_yaml(
            "steps:\n- kind: sync-directory\nversion: 1\n"
        )


def test_parse_rejects_a_non_mapping_document() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_bootstrap_yaml("- connect-inviter\n")


def test_parse_rejects_text_that_is_not_yaml() -> None:
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_bootstrap_yaml("::: not yaml [::")


def test_parse_rejects_duplicate_keys() -> None:
    # A duplicated ``version:`` would let one workflow carry two truths.
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_bootstrap_yaml("steps: []\nversion: 1\nversion: 2\n")
