"""WP1 identity policy for agent secret environment fields."""

from __future__ import annotations

import pytest

from hyprial.identity.impl.agents.runtime.secrets import SecretGrant, SecretSource


def _grant(*, source: SecretSource, environment_name: str) -> SecretGrant:
    return SecretGrant(
        actor="alpha",
        entity_token="incarnation-1",
        grant_id="cloud-service",
        source=source,
        entry_id="cloud-service",
        field_name="token",
        environment_names=(environment_name,),
        revision=1,
    )


def test_wp1_non_model_name_is_allowed_while_source_holders_stay_closed() -> None:
    """An arbitrary valid field is allowed, but custody does not broaden."""

    for source in (SecretSource.USER_PROVIDER, SecretSource.AGENT_PRIVATE):
        grant = _grant(source=source, environment_name="CLOUDFLARE_API_TOKEN")
        assert grant.environment_names == ("CLOUDFLARE_API_TOKEN",)

    with pytest.raises(ValueError):
        SecretSource("adapter")


@pytest.mark.parametrize(
    "reserved_name",
    (
        "PATH",
        "HOME",
        "HYPRIAL_HOME",
        "HYPRIAL_PRIVATE_CONTROL",
        "CODEX_HOME",
        "LANG",
        "TMPDIR",
    ),
)
def test_wp1_reserved_name_rejection_carries_the_required_event(
    reserved_name: str,
) -> None:
    """Acceptance 6: reserved fields fail with a recordable event verdict."""

    with pytest.raises(ValueError) as rejected:
        _grant(source=SecretSource.AGENT_PRIVATE, environment_name=reserved_name)

    event = getattr(rejected.value, "event", None)
    assert event is not None
    assert (event.action, event.field, event.result) == (
        "rejected",
        reserved_name,
        "rejected:reserved-name",
    )
