"""AT07: landing a received home on the target identity (red-first mutants)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Sequence

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.landing.owner import (
    LandingActorMismatch,
    LandingFailed,
    LandingNameTaken,
    land_agent_home)
from hyprial.daemon.impl.transfer.landing.receive import ReceiveDestinationNotEmpty
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL

CREATED_AT = "2026-09-30T22:40:00+08:00"


class FakeAuthority:
    """Stands in for the daemon: agent rows, incarnations and grant rows."""

    def __init__(
        self,
        *,
        exists: bool = False,
        fail_adopt: bool = False,
        fail_grant: str | None = None,
        fail_drop: bool = False,
        fail_revoke: bool = False,
    ) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.grant_rows: dict[str, list[str]] = {}
        self.calls: list[str] = []
        self._exists = exists
        self._fail_adopt = fail_adopt
        self._fail_grant = fail_grant
        self._fail_drop = fail_drop
        self._fail_revoke = fail_revoke
        self._minted = 0

    def agent_exists(self, actor: str) -> bool:
        self.calls.append(f"exists:{actor}")
        return self._exists or actor in self.rows

    def adopt_agent(
        self,
        actor: str,
        *,
        owner: str,
        home: Path,
        source_machine: str,
    ) -> None:
        self.calls.append(f"adopt:{actor}")
        if self._fail_adopt:
            raise RuntimeError("registry write failed")
        # The target mints its OWN incarnation; the bundle never carries one.
        self._minted += 1
        self.rows[actor] = {
            "owner": owner,
            "home": str(home),
            "source_machine": source_machine,
            "entityToken": f"target-minted-{self._minted}",
        }

    def drop_agent(self, actor: str) -> None:
        self.calls.append(f"drop:{actor}")
        if self._fail_drop:
            raise RuntimeError("registry drop failed")
        self.rows.pop(actor, None)

    def materialise_grant(self, actor: str, grant: str) -> str | None:
        self.calls.append(f"grant:{grant}")
        if self._fail_grant is not None and grant == self._fail_grant:
            raise RuntimeError(f"grant {grant!r} was refused")
        grant_id = f"grant-id:{grant}"
        self.grant_rows.setdefault(actor, []).append(grant_id)
        return grant_id

    def revoke_grants(self, actor: str, grant_ids: Sequence[str]) -> None:
        self.calls.append("revoke:" + ",".join(grant_ids))
        if self._fail_revoke:
            raise RuntimeError("revoke failed")
        live = set(grant_ids)
        self.grant_rows[actor] = [
            item for item in self.grant_rows.get(actor, []) if item not in live
        ]


def _source_home(root: Path) -> Path:
    home = root / "src" / "agents" / "codex-sw"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "session-1.json").write_text('{"title": "demo"}\n')
    (home / "notes.md").write_text("payload\n")
    (home / "notes.md").chmod(0o640)
    return home


def _bundle(
    root: Path,
    *,
    name: str = "bundle",
    actor: str = "codex-sw",
    grants: Sequence[str] = ("shared-path:/srv/shared",),
    identity: dict[str, object] | None = None,
) -> Path:
    bundle_dir = root / name
    export_bundle(
        _source_home(root),
        bundle_dir,
        owner="owner@example.com",
        actor=actor,
        source_machine="devusr-dev",
        created_at=CREATED_AT,
        identity=identity if identity is not None else {"entityToken": "source-token"},
        grants=grants,
    )
    return bundle_dir


def test_landing_adopts_a_fresh_incarnation_and_materialises_grants(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority()
    destination = tmp_path / "dest" / "agents" / "codex-sw"

    result = land_agent_home(
        bundle_dir,
        destination,
        authority=authority,
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.actor == "codex-sw"
    assert result.owner == "owner@example.com"
    assert result.identity_rebound is True
    assert result.grants == ("grant-id:shared-path:/srv/shared",)
    assert (destination / "notes.md").read_text() == "payload\n"
    assert stat.S_IMODE((destination / "notes.md").stat().st_mode) == 0o640
    row = authority.rows["codex-sw"]
    assert row["owner"] == "owner@example.com"
    assert row["home"] == str(destination)
    assert row["source_machine"] == "devusr-dev"
    # The target's own incarnation, not the source's: the bundle said
    # "source-token" and the row must not.
    assert row["entityToken"].startswith("target-minted-")
    payload = result.as_dict()
    assert payload["identityRebound"] is True
    assert "source-token" not in json.dumps(payload)
    assert "entityToken" not in payload


def test_landing_refuses_a_name_the_target_already_speaks_for(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority(exists=True)
    destination = tmp_path / "dest"

    with pytest.raises(LandingNameTaken) as caught:
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert caught.value.code == "landing_name_taken"
    assert authority.calls == ["exists:codex-sw"]
    assert not destination.exists()


def test_landing_refuses_a_bundle_for_another_actor(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority()

    with pytest.raises(LandingActorMismatch):
        land_agent_home(
            bundle_dir,
            tmp_path / "dest",
            authority=authority,
            expect_actor="bob",
            signature=UNSIGNED_REHEARSAL,
        )

    assert not (tmp_path / "dest").exists()


def test_landing_refuses_a_destination_that_holds_something(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority()
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "keep.md").write_text("not yours\n")

    with pytest.raises(ReceiveDestinationNotEmpty):
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert (destination / "keep.md").read_text() == "not yours\n"
    assert authority.rows == {}


def test_a_failed_adoption_leaves_no_home_and_no_identity(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority(fail_adopt=True)
    destination = tmp_path / "dest" / "agents" / "codex-sw"

    with pytest.raises(LandingFailed) as caught:
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert "registry write failed" in str(caught.value)
    assert not destination.exists()
    assert authority.rows == {}
    assert authority.grant_rows == {}


def test_a_failed_grant_revokes_the_ones_already_written(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path, grants=("first", "second", "third"))
    authority = FakeAuthority(fail_grant="second")
    destination = tmp_path / "dest"

    with pytest.raises(LandingFailed) as caught:
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert "second" in str(caught.value)
    assert authority.calls == [
        "exists:codex-sw",
        "adopt:codex-sw",
        "grant:first",
        "grant:second",
        "revoke:grant-id:first",
        "drop:codex-sw",
    ]
    assert authority.rows == {}
    assert authority.grant_rows["codex-sw"] == []
    assert not destination.exists()


def test_undo_problems_travel_back_with_the_failure(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority(fail_adopt=True, fail_drop=True)

    with pytest.raises(LandingFailed) as caught:
        land_agent_home(
            bundle_dir,
            tmp_path / "dest",
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    # A row that could not be dropped is reported.  ``adopt`` failed, so the
    # drop is a no-op here: what matters is that the message stays honest.
    assert "landing 'codex-sw' failed and was undone" in str(caught.value)


def test_a_revoke_that_fails_is_reported_not_hidden(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path, grants=("first", "second"))
    authority = FakeAuthority(fail_grant="second", fail_revoke=True)

    with pytest.raises(LandingFailed) as caught:
        land_agent_home(
            bundle_dir,
            tmp_path / "dest",
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert "undo problems: revoke grants: revoke failed" in str(caught.value)


def test_landing_leaves_an_existing_empty_destination_empty(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority(fail_adopt=True)
    destination = tmp_path / "dest"
    destination.mkdir()

    with pytest.raises(LandingFailed):
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_dry_run_touches_neither_disk_nor_registry(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    authority = FakeAuthority()
    destination = tmp_path / "dest"

    result = land_agent_home(
        bundle_dir,
        destination,
        authority=authority,
        dry_run=True,
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.dry_run is True
    assert result.identity_rebound is False
    assert result.files == 2
    assert authority.calls == ["exists:codex-sw"]
    assert authority.rows == {}
    assert not destination.exists()


def test_landing_refuses_a_bundle_that_no_longer_validates(tmp_path: Path) -> None:
    from hyprial.daemon.impl.transfer.archive.bundle import BundleError

    bundle_dir = _bundle(tmp_path)
    (bundle_dir / "payload" / "notes.md").write_text("tampered\n")
    authority = FakeAuthority()
    destination = tmp_path / "dest"

    with pytest.raises(BundleError):
        land_agent_home(
            bundle_dir,
            destination,
            authority=authority,
            signature=UNSIGNED_REHEARSAL,
        )

    assert not destination.exists()
    assert authority.rows == {}
