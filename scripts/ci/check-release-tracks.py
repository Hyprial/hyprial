#!/usr/bin/env python3
"""Read remote release tracks: uv run python scripts/ci/check-release-tracks.py VERSION.

Run in a source-repository checkout. This fetches objects but never writes remote
refs. --remote selects a source mirror for reads; default: origin. Public snapshot
commits differ from source commits and must be verified in their own repository.
"""
import argparse
import sys

from release_tracks import TrackError, Tracks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()
    try:
        return 0 if Tracks(args.remote).check(args.version) else 1
    except TrackError as exc:
        print(f"release-tracks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
