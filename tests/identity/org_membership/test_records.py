"""Org-membership records: names, paths, shapes and the authorization rules."""

from __future__ import annotations

import pytest

from hyprial.identity import OrgMembership

owner = "alice@example.test"
AGENT_ID = "a." + "c" * 26


def test_user_key_normalizes_owner_identity() -> None:
    assert OrgMembership.user_key(owner) == "alice-example.test"
    assert OrgMembership.user_key(f"user:{owner}") == "alice-example.test"


def test_kind_accepts_only_work_orgs() -> None:
    assert OrgMembership.org_kind("group-acme") == "work"
    for rejected in ("acme", "personal-alice-example.test"):
        with pytest.raises(OrgMembership.OrgMembershipError):
            OrgMembership.org_kind(rejected)


def test_work_org_accepts_outsider_and_allows_member_agents() -> None:
    assert OrgMembership.org_accepts_outsider("group-acme", "user:other@example") is True


