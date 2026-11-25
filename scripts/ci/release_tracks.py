#!/usr/bin/env python3
"""Source-repository track moves; Allen's 2026-10-26 release rule.

Only github-publish's formal-release step calls --release. nightly-promote calls
--nightly with an optional SHA already accepted by its unchanged marker gates.
Both paths share one nightly writer and lease policy. No local tags are changed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from github_publish_policy import netretry, push


class TrackError(RuntimeError):
    pass


class Tracks:
    def __init__(self, remote: str = "origin") -> None:
        self.remote = remote
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        self.token = self.env.get("GITHUB_TOKEN", "")
        self.backoff = netretry.backoff_schedule(self.env, TrackError)
        self.cwd = Path.cwd()
        if self.git("rev-parse", "--is-shallow-repository") == "true":
            self.network("fetch", "--unshallow", "--no-tags", remote)

    def git(self, *args: str) -> str:
        result = netretry._run(["git", *args], env=self.env)
        if result.returncode:
            raise TrackError(netretry._git_failure_detail(result.stderr, self.token))
        return result.stdout.strip()

    def network(self, *args: str) -> str:
        result = netretry._run_git_network(
            ["git", *args], env=self.env, label="release tracks read",
            token=self.token, sleep=time.sleep, backoff=self.backoff,
        )
        if result.returncode:
            raise TrackError(netretry._git_failure_detail(result.stderr, self.token))
        return result.stdout.strip()

    def tag(self, name: str) -> tuple[str, str]:
        """Return (raw object for lease, peeled commit), including annotated tags."""
        ref = f"refs/tags/{name}"
        rows = self.network("ls-remote", self.remote, ref, ref + "^{}")
        refs = dict(line.split()[::-1] for line in rows.splitlines())
        obj = refs.get(ref, "")
        if not obj:
            return "", ""
        self.network("fetch", "--no-tags", self.remote, obj)
        return obj, self.git("rev-parse", f"{obj}^{{commit}}")

    def ancestor(self, older: str, newer: str) -> bool:
        result = netretry._run(
            ["git", "merge-base", "--is-ancestor", older, newer], env=self.env,
        )
        if result.returncode not in (0, 1):
            raise TrackError(netretry._git_failure_detail(result.stderr, self.token))
        return result.returncode == 0

    def move(self, tag: str, intended: str, previous: str) -> None:
        # The existing retry bound is 3 attempts, 10s/30s. Each retry reads back
        # first; lease rejection never becomes a blind force push.
        if self.remote != "origin":
            raise TrackError("track writes require the source origin remote")
        push.push_tag_with_retry(
            mirror=self.cwd, target_remote=self.remote, tag=tag,
            intended=intended, previous=previous, movable=True, env=self.env,
            token=self.token, sleep=time.sleep, backoff=self.backoff,
            attempts=netretry.NETWORK_RETRY_ATTEMPTS, run=netretry._run,
            run_network=netretry._run_git_network,
            transient=netretry.is_transient_git_network_error,
            backoff_for=netretry._backoff_for,
            failure_detail=netretry._git_failure_detail, error_type=TrackError,
        )

    def release(self, tag: str, expected: str | None) -> None:
        _, target = self.tag(release_tag(tag))
        if not target:
            raise TrackError(f"release tag {tag} is missing")
        if expected and target != expected:
            raise TrackError(f"release tag {tag} is not the event SHA {expected}")
        previous, stable = self.tag("stable")
        if stable and not self.ancestor(stable, target):
            raise TrackError(f"refusing backwards stable move: {stable} -> {target}")
        if stable != target:
            self.move("stable", target, previous)
        self.nightly()

    def nightly(self, candidate: str = "") -> None:
        if candidate:
            if not re.fullmatch(r"[0-9a-f]{40}", candidate):
                raise TrackError("nightly candidate must be a full commit SHA")
            self.network("fetch", "--no-tags", self.remote, candidate)
        moved_to = ""
        # Two callers can race. Recompute from fresh refs, never retry a stale
        # target with a new lease. Three reconciliation passes bound contention;
        # each push still uses the existing transient-network retry policy.
        for _ in range(3):
            _, stable = self.tag("stable")
            previous, nightly = self.tag("nightly")
            target = nightly
            if stable and (not nightly or not self.ancestor(stable, nightly)):
                target = stable
            if candidate:
                if not target or self.ancestor(target, candidate):
                    target = candidate
                else:
                    print(f"NO-PROMOTE: candidate {candidate} is behind/diverges from {target}")
            if target and target != nightly:
                try:
                    self.move("nightly", target, previous)
                except TrackError:
                    current, _ = self.tag("nightly")
                    if current == previous:
                        raise  # not a concurrent ref change; retain the error
                    print("nightly changed concurrently; recomputing the stable floor")
                    continue
                moved_to = target
            # Stable may have advanced while the candidate was being pushed.
            _, stable_now = self.tag("stable")
            _, nightly_now = self.tag("nightly")
            if stable_now and (not nightly_now or not self.ancestor(stable_now, nightly_now)):
                continue
            if moved_to:
                output = os.environ.get("GITHUB_OUTPUT")
                if output:
                    with open(output, "a", encoding="utf-8") as stream:
                        stream.write(f"promoted={moved_to}\n")
            return
        raise TrackError("nightly reconciliation did not converge after 3 passes; rerun")

    def check(self, version: str) -> bool:
        tag = release_tag(version)
        _, target = self.tag(tag)
        points = {name: self.tag(name)[1] for name in ("stable", "nightly", "internal")}
        print(f"{tag}={target or 'missing'}")
        for name, sha in points.items():
            print(f"{name}={sha or 'missing'}")
        stable_ok = bool(target and points["stable"] == target)
        nightly_ok = bool(target and points["nightly"] and self.ancestor(target, points["nightly"]))
        print(f"stable_reached={str(stable_ok).lower()}")
        print(f"nightly_reached={str(nightly_ok).lower()}")
        return stable_ok and nightly_ok


def release_tag(version: str) -> str:
    tag = version if version.startswith("v") else "v" + version
    if not re.fullmatch(r"v[0-9][0-9A-Za-z.+-]*", tag):
        raise TrackError("expected a version or v<version> release tag")
    return tag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--release")
    mode.add_argument("--nightly", nargs="?", const="")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    try:
        tracks = Tracks()
        if args.release:
            tracks.release(args.release, args.expected_sha)
        else:
            tracks.nightly(args.nightly)
    except TrackError as exc:
        print(f"release-tracks: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
