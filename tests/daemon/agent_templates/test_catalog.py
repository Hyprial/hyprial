"""The packaged-template catalog: data lookup, provenance fact, cell params.

Relocated 2026-11-20 (S2b, remove-squire-setup-surface) from the deleted
``tests/daemon/squire/template`` file: the generic catalog behavior stays
pinned against the remaining packaged template, while the squire template
and its management path are gone.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.agent_templates import (
    AgentTemplateError,
    load_template,
    template_cell_params,
    template_name_of,
    template_names,
)
from hyprial.identity.impl.agents import Agent


def test_the_packaged_templates_are_data_with_their_role_in_cell_params() -> None:
    template = load_template("user")

    assert template_names() == ("user",)
    assert template.capabilities == {"template": "user"}
    assert template.cell_params == {"agent.role": "user", "agent.template": "user"}
    with pytest.raises(AgentTemplateError):
        load_template("no-such-template")
    with pytest.raises(AgentTemplateError):
        load_template("../user")


def test_a_recorded_template_fact_names_the_template_and_a_plain_agent_has_none() -> None:
    made = Agent(
        uri="agent:alice:mac:helper", actor="helper", owner="alice", machine="mac",
        capabilities={"template": "user"},
    )
    plain = Agent(
        uri="agent:alice:mac:worker", actor="worker", owner="alice", machine="mac",
    )

    assert template_name_of(made) == "user"
    assert template_cell_params(made)["agent.role"] == "user"
    assert template_name_of(plain) is None
    assert template_cell_params(plain) == {}


def test_a_fact_naming_no_packaged_template_seeds_nothing() -> None:
    """An agent stamped by a template that is no longer packaged still loads."""

    retired = Agent(
        uri="agent:alice:mac:helper", actor="helper", owner="alice", machine="mac",
        capabilities={"template": "retired"},
    )

    assert template_name_of(retired) == "retired"
    assert template_cell_params(retired) == {}
