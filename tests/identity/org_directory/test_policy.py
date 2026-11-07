"""CasbinOrgPolicy: the frozen owner/admin/member permission table (M0 §4.2)."""

from __future__ import annotations

import pytest

from hyprial.identity.impl.org_directory.policy import (
    ACTION_INVITE,
    ACTION_LEAVE,
    ACTION_READ,
    ACTION_REMOVE,
    ACTION_WRITE,
    RESOURCE_DIRECTORY_ALL,
    RESOURCE_DIRECTORY_DEVICES,
    RESOURCE_ORG_MEMBERS,
    RESOURCE_ORG_SELF,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    CasbinOrgPolicy,
    default_role_grants_document,
    role_grants_from_document,
)


def _grants() -> tuple[tuple[str, str, str], ...]:
    return role_grants_from_document(default_role_grants_document())


def _policy() -> CasbinOrgPolicy:
    return CasbinOrgPolicy.from_roles(
        {
            "group-alpha": {
                "alice@example": ROLE_OWNER,
                "bob@example": ROLE_ADMIN,
                "carol@example": ROLE_MEMBER,
            },
            "group-beta": {
                "dave@example": ROLE_ADMIN,
            },
        },
        _grants(),
    )


# -- owner ---------------------------------------------------------------------


def test_owner_may_do_everything_in_their_org() -> None:
    policy = _policy()
    for action, resource in (
        (ACTION_INVITE, RESOURCE_ORG_MEMBERS),
        (ACTION_REMOVE, RESOURCE_ORG_MEMBERS),
        (ACTION_READ, "directory/devices/alice@example/node-1"),
        (ACTION_WRITE, RESOURCE_DIRECTORY_ALL),
        (ACTION_LEAVE, RESOURCE_ORG_SELF),
        ("delete", "org"),
        ("anything-at-all", "any:resource"),
    ):
        assert policy.allows(
            org="group-alpha", subject="alice@example", action=action, resource=resource
        ), (action, resource)


# -- admin ---------------------------------------------------------------------


def test_admin_may_invite_and_remove_members_and_write_the_directory() -> None:
    policy = _policy()
    for action, resource in (
        (ACTION_INVITE, RESOURCE_ORG_MEMBERS),
        (ACTION_REMOVE, RESOURCE_ORG_MEMBERS),
        (ACTION_WRITE, RESOURCE_DIRECTORY_ALL),
        (ACTION_WRITE, RESOURCE_DIRECTORY_DEVICES),
        (ACTION_WRITE, "directory/devices/bob@example/node-1"),
    ):
        assert policy.allows(
            org="group-alpha", subject="bob@example", action=action, resource=resource
        ), (action, resource)


def test_admin_includes_member_rights_but_may_not_delete_the_org() -> None:
    # admin ⊇ member: an admin reads the directory and may leave, but deleting
    # the org (the owner wildcard) stays owner-only.
    policy = _policy()
    for action, resource in (
        (ACTION_READ, RESOURCE_DIRECTORY_ALL),
        (ACTION_READ, "directory/devices/bob@example/node-1"),
        (ACTION_LEAVE, RESOURCE_ORG_SELF),
    ):
        assert policy.allows(
            org="group-alpha", subject="bob@example", action=action, resource=resource
        ), (action, resource)
    assert not policy.allows(org="group-alpha", subject="bob@example", action="delete", resource="org")


# -- member --------------------------------------------------------------------


def test_member_may_read_write_own_devices_and_leave() -> None:
    policy = _policy()
    for action, resource in (
        (ACTION_READ, RESOURCE_DIRECTORY_ALL),
        (ACTION_READ, "directory/devices/carol@example/node-1"),
        (ACTION_WRITE, RESOURCE_DIRECTORY_DEVICES),
        (ACTION_WRITE, "directory/devices/carol@example/node-1"),
        (ACTION_LEAVE, RESOURCE_ORG_SELF),
    ):
        assert policy.allows(
            org="group-alpha", subject="carol@example", action=action, resource=resource
        ), (action, resource)


def test_member_may_not_invite_remove_or_write_the_whole_directory() -> None:
    policy = _policy()
    for action, resource in (
        (ACTION_INVITE, RESOURCE_ORG_MEMBERS),
        (ACTION_REMOVE, RESOURCE_ORG_MEMBERS),
        (ACTION_WRITE, RESOURCE_DIRECTORY_ALL),
        (ACTION_WRITE, "directory/announcements.json"),
        ("delete", "org"),
    ):
        assert not policy.allows(
            org="group-alpha", subject="carol@example", action=action, resource=resource
        ), (action, resource)


# -- domain isolation and strangers ---------------------------------------------


def test_roles_are_per_org() -> None:
    policy = _policy()
    # dave is admin of beta, a stranger in alpha.
    assert policy.allows(
        org="group-beta", subject="dave@example", action=ACTION_INVITE, resource=RESOURCE_ORG_MEMBERS
    )
    assert not policy.allows(
        org="group-alpha", subject="dave@example", action=ACTION_INVITE, resource=RESOURCE_ORG_MEMBERS
    )
    # alice owns alpha but has no role in beta.
    assert not policy.allows(
        org="group-beta", subject="alice@example", action=ACTION_READ, resource=RESOURCE_DIRECTORY_ALL
    )


def test_unknown_users_are_denied_everything() -> None:
    policy = _policy()
    for action, resource in (
        (ACTION_READ, RESOURCE_DIRECTORY_ALL),
        (ACTION_INVITE, RESOURCE_ORG_MEMBERS),
        (ACTION_LEAVE, RESOURCE_ORG_SELF),
    ):
        assert not policy.allows(
            org="group-alpha", subject="mallory@example", action=action, resource=resource
        )
        assert not policy.allows(
            org="group-gamma", subject="alice@example", action=action, resource=resource
        )


def test_role_of_reports_the_grant() -> None:
    policy = _policy()
    assert policy.role_of("group-alpha", "alice@example") == ROLE_OWNER
    assert policy.role_of("group-alpha", "mallory@example") is None
    assert policy.role_of("group-gamma", "alice@example") is None


# -- update_roles ---------------------------------------------------------------


def test_update_roles_replaces_one_org_without_touching_the_others() -> None:
    policy = _policy()
    policy.update_roles("group-alpha", {"carol@example": ROLE_ADMIN})
    assert policy.allows(
        org="group-alpha", subject="carol@example", action=ACTION_INVITE, resource=RESOURCE_ORG_MEMBERS
    )
    assert not policy.allows(
        org="group-alpha", subject="alice@example", action=ACTION_INVITE, resource=RESOURCE_ORG_MEMBERS
    )
    # beta is untouched.
    assert policy.allows(
        org="group-beta", subject="dave@example", action=ACTION_INVITE, resource=RESOURCE_ORG_MEMBERS
    )


def test_an_empty_policy_denies_everything() -> None:
    policy = CasbinOrgPolicy({}, _grants())
    assert not policy.allows(
        org="group-alpha", subject="alice@example", action=ACTION_READ, resource=RESOURCE_DIRECTORY_ALL
    )


def test_invalid_roles_and_names_are_rejected() -> None:
    with pytest.raises(ValueError):
        CasbinOrgPolicy({"group-alpha": {"alice@example": "superuser"}}, _grants())
    with pytest.raises(ValueError):
        CasbinOrgPolicy({"Not A Org": {"alice@example": ROLE_OWNER}}, _grants())
    policy = CasbinOrgPolicy({}, _grants())
    with pytest.raises(ValueError):
        policy.update_roles("group-alpha", {"a,b@example": ROLE_MEMBER})


def test_default_role_grants_document_round_trips_the_frozen_table() -> None:
    document = default_role_grants_document()
    assert document["version"] == 1
    assert role_grants_from_document(document) == _grants()
    assert (ROLE_OWNER, "*", "*") in _grants()


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": 2, "grants": []},
        {"version": 1, "grants": [{"role": "superuser", "resource": "*", "action": "*"}]},
        {"version": 1, "grants": [{"role": ROLE_OWNER, "resource": "", "action": "*"}]},
    ],
)
def test_role_grants_document_rejects_unusable_policy(document: object) -> None:
    with pytest.raises(ValueError):
        role_grants_from_document(document)
