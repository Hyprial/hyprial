"""The address-family table holds the deliverable families only (I4b).

Decision notes: ``route:<adapter>:<route>`` is retired as an address family;
the table lists agent and user, a ``route:`` string is simply not a
deliverable address, and none of the route parsing names remain on the
kernel surface.
"""

from __future__ import annotations

import pytest

import hyprial.kernel as kernel
from hyprial.kernel.impl.primitives import uri as uri_module

RETIRED_NAMES = ("ROUTE_URI_PREFIX", "parse_route_uri", "TARGET_KIND_CHANNEL_ROUTE")


def test_table_is_the_single_family_surface() -> None:
    families = uri_module.ADDRESS_FAMILIES
    assert tuple(families) == ("agent", "user")
    assert all(family.name == key for key, family in families.items())
    assert all(family.escalation_supported for family in families.values())


@pytest.mark.parametrize("name", RETIRED_NAMES)
def test_the_route_parsing_names_are_gone_from_the_kernel(name: str) -> None:
    assert not hasattr(kernel, name)
    assert not hasattr(uri_module, name)


def test_channel_route_is_not_a_target_kind() -> None:
    assert "channel_route" not in uri_module.TARGET_KINDS
    assert "channel_route" not in {
        value for value in vars(uri_module).values() if isinstance(value, str)
    }


@pytest.mark.parametrize(
    "value", ["route:a:b", "route:hyprial-introducer-feishu:customer-dm", "route:only-adapter"]
)
def test_a_route_string_is_the_generic_undeliverable_address(value: str) -> None:
    message = uri_module.delivery_address_error(value)

    assert message is not None
    assert "is not a deliverable address" in message
    assert "hyprial adapter route add" not in message
    assert "outbound chat sends" not in message


def test_agent_and_user_addresses_are_still_deliverable() -> None:
    assert uri_module.delivery_address_error("agent:alice:mac:worker") is None
    assert uri_module.delivery_address_error("user:alice") is None
