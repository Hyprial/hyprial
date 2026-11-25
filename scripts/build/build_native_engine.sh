#!/usr/bin/env bash
# Build the native actor engine (crates/) and stage it as the private
# `hyprial._core.actors` extension inside the source tree so an editable
# `uv` environment imports it directly.
#
# This is the developer-loop helper for the actor-rustify line; the wheel
# path stays owned by the Hatch build hook per the build/distribution ADR.
#
# `hyprial._core` is a PEP 420 namespace package (no __init__.py): its only
# member is the compiled `hyprial._core.actors` (ENGINE_ABI_VERSION 1), which
# keeps the module-boundary gate's five-domain mapping intact without a
# Python file that belongs to no domain. Import contract: the adapter fails
# closed when the extension is absent or reports a foreign ABI version; a
# missing `hyprial._core.actors` is an error, never a fallback to the Python
# engine.
#
# Usage: scripts/build/build_native_engine.sh [--debug] [--conformance] [--free-threaded]
#
# --free-threaded builds a version-specific module for the uv-managed
# free-threaded CPython 3.14t (PyO3 builds no abi3 module for free-threaded
# interpreters) into crates/target-ft and stages it under
# crates/target-ft/stage; that directory is the HYPRIAL_CONFORMANCE_FT_PATH of
# the conformance case FREE-03. The editable-tree module is left untouched.
#
# --conformance adds the test-only `conformance-handlers` feature (spec
# clarification CL-2): module init registers the conformance suite's native
# handler factories through register_native_factory. Never used for wheels.
#
# After staging, the script records src/hyprial/_core/actors.stamp.json (the
# digest of every crates/ input, the module's SHA-256, features and profile)
# through src/hyprial/shell/impl/packaging/native_engine.py. An editable build
# (uv sync) verifies the staged module against that stamp and its
# ENGINE_ABI_VERSION, and fails on a stale or foreign module. Wheels never
# use the staged module: the Hatch hook compiles their own.
#
# The artefact path is taken from cargo's own --message-format=json output
# (native_engine.py library), never from a guessed target directory, so
# CARGO_TARGET_DIR, CARGO_BUILD_TARGET_DIR and build.target-dir in
# .cargo/config.toml all work and a stale crates/target library is never staged.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT}"

# Fixed toolchain per docs/design/actor-rustify-build-distribution-adr-2026-10-24.md
RUST_VERSION="1.93.1"
if command -v rustup >/dev/null 2>&1; then
    if [[ "$(rustup run "${RUST_VERSION}" rustc --version 2>/dev/null || true)" != *"${RUST_VERSION}"* ]]; then
        echo "error: Rust ${RUST_VERSION} is not available: install it with \`rustup toolchain install ${RUST_VERSION}\`, or skip the native build by leaving HYPRIAL_BUILD_NATIVE unset when running uv sync" >&2
        exit 1
    fi
    RUNNER=(rustup run "${RUST_VERSION}")
else
    RUNNER=()
    if ! rustc --version 2>/dev/null | grep -q "${RUST_VERSION}"; then
        echo "error: Rust ${RUST_VERSION} is not available: install it with \`rustup toolchain install ${RUST_VERSION}\`, or skip the native build by leaving HYPRIAL_BUILD_NATIVE unset when running uv sync" >&2
        exit 1
    fi
fi

PROFILE=release
CARGO_PROFILE=--release
FEATURES=extension-module
FREE_THREADED=0
for arg in "$@"; do
    case "${arg}" in
        --debug) PROFILE=debug; CARGO_PROFILE="" ;;
        --free-threaded) FREE_THREADED=1 ;;
        --conformance) FEATURES="extension-module,conformance-handlers" ;;
        *) echo "error: unknown argument ${arg}" >&2; exit 2 ;;
    esac
done

if [[ "${FREE_THREADED}" == "1" ]]; then
    FT_PYTHON="$(uv python find 3.14t)"
    FT_SUFFIX="$("${FT_PYTHON}" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
    PYO3_PYTHON="${FT_PYTHON}" CARGO_TARGET_DIR=crates/target-ft \
        "${RUNNER[@]+"${RUNNER[@]}"}" cargo build ${CARGO_PROFILE} --locked --manifest-path crates/Cargo.toml \
        --features "${FEATURES}" -p hyprial-py
    STAGE="crates/target-ft/stage"
    mkdir -p "${STAGE}/hyprial/_core"
    : > "${STAGE}/hyprial/__init__.py"
    : > "${STAGE}/hyprial/_core/__init__.py"
    FT_EXT="crates/target-ft/${PROFILE}/libactors.so"
    [[ "$(uname)" == "Darwin" ]] && FT_EXT="crates/target-ft/${PROFILE}/libactors.dylib"
    cp "${FT_EXT}" "${STAGE}/hyprial/_core/actors${FT_SUFFIX}"
    echo "staged ${STAGE}/hyprial/_core/actors${FT_SUFFIX}"
    echo "HYPRIAL_CONFORMANCE_FT_PYTHON=${FT_PYTHON}"
    echo "HYPRIAL_CONFORMANCE_FT_PATH=$(pwd)/${STAGE}"
    exit 0
fi

# PYO3_PYTHON: the uv-managed build interpreter, per the build/distribution
# ADR (hook passes the interpreter explicitly; do not rely on system python).
if [[ -z "${PYO3_PYTHON:-}" ]]; then
    if [[ -x ".venv/bin/python" ]]; then
        export PYO3_PYTHON="$(readlink -f .venv/bin/python)"
    else
        echo "error: .venv/bin/python not found (run: uv sync --extra test)" >&2
        exit 1
    fi
fi

# RUNNER is legitimately empty when rustup is not on PATH (the E2E script
# installs cargo into a private toolchain dir instead). Expand it optionally:
# bash < 4.4 treats "${RUNNER[@]}" on an empty array as unbound under set -u,
# which broke the manylinux2014_aarch64 gate (run 57004, job 149555).
MESSAGES="$(mktemp)"
trap 'rm -f "${MESSAGES}"' EXIT
"${RUNNER[@]+"${RUNNER[@]}"}" cargo build ${CARGO_PROFILE} --locked --manifest-path crates/Cargo.toml --features "${FEATURES}" -p hyprial-py \
    --message-format=json-render-diagnostics > "${MESSAGES}"

EXT="$(uv run --no-project --python 3.12 python src/hyprial/shell/impl/packaging/native_engine.py library "${MESSAGES}")"
DEST="src/hyprial/_core/actors.abi3.so"

mkdir -p src/hyprial/_core
cp "${EXT}" "${DEST}"
echo "staged ${DEST} (from ${EXT})"
uv run --no-project --python 3.12 python src/hyprial/shell/impl/packaging/native_engine.py \
    stamp "$(pwd)" "${DEST}" "${FEATURES}" "${PROFILE}"
