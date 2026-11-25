"""The pure predicate: is an address a user agent (of owner X), from its text alone.

Each machine's user agent is an independent agent with a registry-minted id,
so the text alone recognises only the name form.  An id-form address of any
machine's user agent is an ordinary agent address here; this machine's own
instance is recognised by its registry marker instead (``is_user_agent``).
"""

from __future__ import annotations

import hashlib

import pytest

from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, is_user_agent_address
from hyprial.identity import agent_id_for_entity_token

OWNER = "owner@example"
FORMER_DERIVED_ID = agent_id_for_entity_token(
    hashlib.sha256(b"hyprial-user-agent-token-v1\0" + OWNER.encode()).digest()[:16].hex()
)


@pytest.mark.parametrize(
    "address",
    [
        f"agent:{OWNER}:node-a:{USER_AGENT_NAME}",
        f"agent:{OWNER}:a-machine-never-seen:{USER_AGENT_NAME}",
    ],
)
def test_the_name_form_on_any_machine_is_recognised(address: str) -> None:
    assert is_user_agent_address(address)
    assert is_user_agent_address(address, OWNER)
    assert is_user_agent_address(address, "user:" + OWNER)
    assert not is_user_agent_address(address, "someone-else")


@pytest.mark.parametrize(
    "address",
    [
        f"agent:{OWNER}:node-b:{FORMER_DERIVED_ID}",
        f"agent:{OWNER}:node-b:a.aaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_pending_merge_an_id_form_address_of_another_machines_user_agent_is_ordinary(
    address: str,
) -> None:
    """Pinned for the later merge step: the text of an id-form address names no user agent.

    That holds for a random id and for the id an older build derived from the
    owner alike; another machine's instance is recognised by its name form only.
    """

    assert not is_user_agent_address(address)
    assert not is_user_agent_address(address, OWNER)


@pytest.mark.parametrize(
    "address",
    [
        f"agent:{OWNER}:node-a:user",
        f"agent:{OWNER}:node-a:hyprial-user-2",
        f"agent:{OWNER}:node-a:{USER_AGENT_NAME.upper()}",
        f"user:{OWNER}",
        f"agent:{OWNER}:{USER_AGENT_NAME}",
        USER_AGENT_NAME,
        "",
    ],
)
def test_look_alikes_are_refused(address: str) -> None:
    assert not is_user_agent_address(address, OWNER)


def test_another_owners_user_agent_is_recognised_without_naming_the_owner() -> None:
    theirs = f"agent:someone-else:node-x:{USER_AGENT_NAME}"

    assert is_user_agent_address(theirs)
    assert is_user_agent_address(theirs, "someone-else")
    assert not is_user_agent_address(theirs, OWNER)


def test_non_strings_are_refused() -> None:
    assert not is_user_agent_address(None)
    assert not is_user_agent_address(42)
