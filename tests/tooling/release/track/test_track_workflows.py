"""Concurrency contracts of ``scripts/ci/release_tracks.py`` under real Git.

The mover is driven directly against isolated, local-only remotes: a stale
lease cannot overwrite a concurrent release, a nightly move that races a
competitor rechecks the floor and keeps the newest commit, and a release
cannot roll back a newer nightly.  The workflow step bodies that once wrapped
the mover were retired with their workflows and are not reproduced here.
"""
from __future__ import annotations

import pytest

from .test_release_tracks import SCRIPTS, move, remote
from .test_release_tracks import repo as repo


def test_stale_lease_cannot_overwrite_a_concurrent_release(repo, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from release_tracks import TrackError, Tracks

    # Tracks uses cwd for the push, so construct and read in the fixture repo.
    monkeypatch.chdir(repo[0])
    tracks = Tracks()
    previous, _ = tracks.tag("stable")
    move(repo, "stable", repo[2][2])
    with pytest.raises(TrackError, match="push of refs/tags/stable failed"):
        tracks.move("stable", repo[2][1], previous)
    assert remote(repo, "stable") == repo[2][2]


@pytest.mark.parametrize("stable,competitor", [(2, 1), (1, 2)])
def test_concurrent_nightly_move_rechecks_floor_and_preserves_newest(repo, monkeypatch, stable, competitor):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.chdir(repo[0])
    from release_tracks import Tracks

    move(repo, "stable", repo[2][stable])
    tracks = Tracks()
    original = tracks.move
    raced = False

    def race(tag, intended, previous):
        nonlocal raced
        if not raced:
            raced = True
            move(repo, "nightly", repo[2][competitor])
        original(tag, intended, previous)

    monkeypatch.setattr(tracks, "move", race)
    tracks.nightly()
    assert raced
    assert remote(repo, "nightly") == repo[2][2]


def test_release_racing_newer_nightly_cannot_roll_it_back(repo, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.chdir(repo[0])
    from release_tracks import TrackError, Tracks

    tracks = Tracks()
    previous, _ = tracks.tag("nightly")
    move(repo, "nightly", repo[2][2])
    with pytest.raises(TrackError, match="push of refs/tags/nightly failed"):
        tracks.move("nightly", repo[2][1], previous)
    assert remote(repo, "nightly") == repo[2][2]
