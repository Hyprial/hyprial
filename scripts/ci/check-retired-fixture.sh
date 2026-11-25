#!/bin/sh
# Classify retired GUI fixture roots for a workflow.
#
# Usage: check-retired-fixture.sh <root> [<root> ...]
#
# - none of the roots exist: the fixture is retired, so emit present=false and
#   succeed; the caller skips its fixture-only steps.
# - at least one root exists but a root or its package-lock.json is missing:
#   this is a partial/accidental deletion, not retirement.  Name every missing
#   path and exit 1 so the workflow turns red.
# - every root exists with its lockfile: emit present=true and succeed.
#
# GITHUB_OUTPUT is honored when set; otherwise `present=...` goes to stdout.
set -eu

if [ "$#" -eq 0 ]; then
    echo "check-retired-fixture: at least one root is required" >&2
    exit 2
fi

present_roots=0
missing=""
for root in "$@"; do
    if [ -d "$root" ]; then
        present_roots=$((present_roots + 1))
        if [ ! -f "$root/package-lock.json" ]; then
            missing="${missing}${root}/package-lock.json (root exists)"$'\n'
        fi
    elif [ -f "$root/package-lock.json" ]; then
        missing="${missing}${root} (lockfile exists without its root)"$'\n'
    elif [ ! -e "$root" ]; then
        missing="${missing}${root} (root absent)"$'\n'
    fi
done

if [ "$present_roots" -eq 0 ]; then
    echo "retired GUI fixture roots absent: skipping fixture steps" >&2
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        printf 'present=false\n' >> "$GITHUB_OUTPUT"
    else
        printf 'present=false\n'
    fi
    exit 0
fi

if [ -n "$missing" ]; then
    printf 'retired GUI fixture is incomplete; missing:\n%s' "$missing" >&2
    exit 1
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf 'present=true\n' >> "$GITHUB_OUTPUT"
else
    printf 'present=true\n'
fi
