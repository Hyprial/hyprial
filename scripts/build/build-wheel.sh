#!/usr/bin/env bash
# Build a hyprial wheel stamped with the PEP 440 local version segment
# X.Y.Z+g<short7> derived from the exact HEAD commit.
#
# The repository carries the manually declared X.Y.Z in ./VERSION, one line,
# nothing else (devN suffixes are retired; bumps are developer-manual only).
# The local segment exists ONLY on built artifacts so a published wheel names
# the exact commit it was built from; the stamp is applied to a temporary
# VERSION edit and always restored, even on failure.  The working tree must be
# clean so the hash in the version is the whole truth about the build inputs.
#
# Usage: scripts/build/build-wheel.sh [outdir]   (default: dist/)

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
OUTDIR="${1:-dist}"
VERSION_FILE="${ROOT}/VERSION"
LOCKFILE="${ROOT}/uv.lock"

cd "${ROOT}"

SHORT_HASH="$(git rev-parse --short=7 HEAD)"

if [ -n "$(git status --porcelain)" ]; then
    echo "warning: working tree is dirty; +g${SHORT_HASH} will not name all build inputs" >&2
fi
if [ -n "$(git status --porcelain -- VERSION pyproject.toml uv.lock)" ]; then
    echo "VERSION/pyproject.toml/uv.lock have uncommitted changes; refusing to stamp" >&2
    exit 1
fi

# Read the file the build itself reads, so a stamp can never be derived from a
# version the wheel will not carry.
BASE_VERSION="$(tr -d '[:space:]' <"${VERSION_FILE}")"
if [ -z "${BASE_VERSION}" ]; then
    echo "VERSION is empty or unreadable; refusing to stamp" >&2
    exit 1
fi
STAMPED="${BASE_VERSION}+g${SHORT_HASH}"

restore() {
    git checkout --quiet -- VERSION pyproject.toml uv.lock
}
trap restore EXIT

# Write the stamp where the build reads it.  `uv version` is not used any more:
# with a dynamic version it would have to know about the hatch version source,
# and this way the one file that decides the built version is the one edited.
printf '%s\n' "${STAMPED}" >"${VERSION_FILE}"
uv build --wheel --out-dir "${OUTDIR}"
echo "built ${STAMPED} -> ${OUTDIR}/"
