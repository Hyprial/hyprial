"""Local org-network state files under ``state/tailcat/``."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.org.network.state import (
    add_left_org,
    add_org_acl_grant,
    clear_left_org,
    org_acl_grants_path,
    read_left_orgs,
    read_org_acl_grants,
    remove_org_acl_grant,
)


def test_directory_bootstrap_operations_are_retired() -> None:
    from hyprial.daemon.impl.org.network import state
    assert not hasattr(state, 'add_bootstrap_peer')
    assert not hasattr(state, 'read_bootstrap_peers')


def test_a_left_org_can_be_cleared_again(tmp_path: Path) -> None:
    add_left_org(tmp_path, "group-acme")
    add_left_org(tmp_path, "group-zeta")

    assert clear_left_org(tmp_path, "group-acme") == ["group-zeta"]
    assert read_left_orgs(tmp_path) == ["group-zeta"]
    # Clearing an org that was never left is a no-op.
    assert clear_left_org(tmp_path, "other") == ["group-zeta"]


AGENT = "agent:owner@example:node-a:worker"


def test_org_acl_grants_round_trip(tmp_path: Path) -> None:
    # Grants are org state, not tailcat state: they live under state/org/.
    assert org_acl_grants_path(tmp_path) == tmp_path / "state/org/org-acl-grants.json"
    entry = add_org_acl_grant(
        tmp_path, "group-acme", AGENT, granted_at_ms=1000, until_ms=None
    )
    assert entry == {"grantedAtMs": 1000, "untilMs": None}
    assert read_org_acl_grants(tmp_path) == {"group-acme": {AGENT: entry}}

    assert remove_org_acl_grant(tmp_path, "group-acme", AGENT) is True
    assert read_org_acl_grants(tmp_path) == {}
    # Revoking what is not recorded answers False, never raises.
    assert remove_org_acl_grant(tmp_path, "group-acme", AGENT) is False


def test_an_expired_org_acl_grant_never_comes_back(tmp_path: Path) -> None:
    add_org_acl_grant(
        tmp_path, "group-acme", AGENT, granted_at_ms=1000, until_ms=2000
    )
    assert read_org_acl_grants(tmp_path, now_ms=1999) == {
        "group-acme": {AGENT: {"grantedAtMs": 1000, "untilMs": 2000}}
    }
    assert read_org_acl_grants(tmp_path, now_ms=2000) == {}
    # The next write prunes the expired entry from the file itself.
    add_org_acl_grant(
        tmp_path, "group-zeta", AGENT, granted_at_ms=3000, until_ms=None
    )
    assert "group-acme" not in read_org_acl_grants(tmp_path)


def test_a_corrupt_org_acl_grants_file_reads_as_empty(tmp_path: Path) -> None:
    path = org_acl_grants_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_org_acl_grants(tmp_path) == {}


def test_malformed_org_acl_grant_entries_are_ignored(tmp_path: Path) -> None:
    path = org_acl_grants_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"group-acme": {"agent:a": {"grantedAtMs": "soon"}, '
        '"agent:b": {"grantedAtMs": 1, "untilMs": true}}, "group-ok": '
        f'{{"{AGENT}": {{"grantedAtMs": 5, "untilMs": null}}}}}}',
        encoding="utf-8",
    )
    assert read_org_acl_grants(tmp_path) == {
        "group-ok": {AGENT: {"grantedAtMs": 5, "untilMs": None}}
    }
