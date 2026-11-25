#!/usr/bin/env bash
# Produce linux/amd64 manylinux_2_17 build evidence for the native actor
# engine extension `hyprial._core.actors` (crate crates/hyprial-py, PyO3
# abi3-py312), as required by the linux/amd64 hard cell of
# docs/design/actor-rustify-build-distribution-adr-2026-10-24.md section 2.1.
#
# Usage:
#   scripts/build/manylinux_native_evidence.sh [--ref REF] [--out DIR]
#       [--image IMAGE] [--rust VERSION] [--ca-bundle PATH]
#       [--cache-volume NAME] [--no-cache] [--skip-behavior]
#
# Procedure and decisions:
# - Sources come from `git archive <ref> crates`, never the working tree, so
#   concurrent uncommitted edits cannot leak into the evidence; the resolved
#   commit SHA is printed with the evidence.
# - The build runs inside quay.io/pypa/manylinux2014_x86_64 (glibc 2.17, the
#   manylinux_2_17 baseline). Building on a newer-glibc host and relabelling
#   is explicitly forbidden by the ADR. The resolved image digest is printed.
# - Rust is installed in the container with rustup (minimal profile) at the
#   ADR-fixed toolchain 1.93.1; the rustup-init binary is checked against the
#   SHA-256 published next to it. RUSTUP_HOME/CARGO_HOME live in a named
#   docker volume so reruns skip the toolchain/registry download; --no-cache
#   uses a throwaway volume instead.
# - PYO3_PYTHON is pinned to /opt/python/cp312-cp312/bin/python (the abi3
#   floor), never left to PyO3 auto-discovery.
# - The product build MUST pass `--features extension-module`; without it the
#   cdylib links libpython (DT_NEEDED libpython3.x.so, or a static libpython
#   copy when the interpreter has no shared library, as in manylinux images).
#   A negative-control build without the feature runs in a separate target
#   directory and its NEEDED list and defined Py* symbol count are printed so
#   the evidence shows the feature is load-bearing.
# - Checks: `readelf -d` NEEDED list must contain no libpython; the highest
#   GLIBC_ symbol version from `objdump -T` must be <= 2.17; a throwaway
#   wheel wrapping the .so as hyprial/_core/actors.abi3.so (tag
#   cp312-abi3-linux_x86_64) must be reported by `auditwheel show` as
#   consistent with manylinux_2_17_x86_64 and must survive
#   `auditwheel repair --plat manylinux_2_17_x86_64`.
# - Import checks stage the .so as <stage>/hyprial/_core/actors.abi3.so with
#   empty hyprial/__init__.py and hyprial/_core/__init__.py, then import it
#   under every standard-GIL CPython >= 3.12 the image ships (cp312, cp313,
#   cp314 are required). Free-threaded (t) interpreters are run and reported
#   as informational only: the ADR does not support them and requires an
#   explicit error there.
# - The public-call check runs crates/hyprial-py/tests/engine_behavior.py from
#   the same archived ref against the staged module on cp312, cp313 and
#   cp314 (`--skip-behavior` omits it and reports SKIPPED, never PASS).
# - Proxy egress: HTTPS_PROXY/https_proxy are forwarded and the container
#   uses --network host so a loopback proxy is reachable; --ca-bundle
#   (default: $SSL_CERT_FILE when it names a file) is mounted read-only,
#   added to the container trust store and exported via SSL_CERT_FILE,
#   CURL_CA_BUNDLE, CARGO_HTTP_CAINFO and PIP_CERT. TLS verification is
#   never disabled.
# - Exit status is 0 only when every required check is PASS; the evidence
#   text is also written to <out>/evidence.txt.
set -euo pipefail

REF="HEAD"
OUT=""
IMAGE="quay.io/pypa/manylinux2014_x86_64"
RUST_VERSION="1.93.1"
CA_BUNDLE="${SSL_CERT_FILE:-}"
CACHE_VOLUME="hyprial-manylinux-evidence-cache"
NO_CACHE=0
SKIP_BEHAVIOR=0

usage() {
    sed -n '7,10p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REF="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --rust) RUST_VERSION="$2"; shift 2 ;;
        --ca-bundle) CA_BUNDLE="$2"; shift 2 ;;
        --cache-volume) CACHE_VOLUME="$2"; shift 2 ;;
        --no-cache) NO_CACHE=1; shift ;;
        --skip-behavior) SKIP_BEHAVIOR=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument $1" >&2; usage >&2; exit 2 ;;
    esac
done

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
SHA="$(git -C "${REPO}" rev-parse --verify "${REF}^{commit}")"

if [[ -z "${OUT}" ]]; then
    OUT="$(mktemp -d "${TMPDIR:-/tmp}/manylinux-native-evidence.XXXXXX")"
fi
mkdir -p "${OUT}"
OUT="$(cd "${OUT}" && pwd)"
rm -rf "${OUT}/src" "${OUT}/out" "${OUT}/target-ext" "${OUT}/target-noext"
mkdir -p "${OUT}/src" "${OUT}/out"
git -C "${REPO}" archive "${SHA}" crates | tar -x -C "${OUT}/src"

if ! docker info >/dev/null 2>&1; then
    echo "BLOCKED: docker daemon is not reachable (docker info failed)" >&2
    docker info 2>&1 | tail -5 >&2 || true
    exit 3
fi

T0=${SECONDS}
docker pull "${IMAGE}" >"${OUT}/pull.log" 2>&1 || { cat "${OUT}/pull.log" >&2; echo "BLOCKED: docker pull ${IMAGE} failed" >&2; exit 3; }
PULL_SECONDS=$((SECONDS - T0))
DIGEST="$(docker image inspect "${IMAGE}" --format '{{join .RepoDigests " "}}')"

if [[ "${NO_CACHE}" == "1" ]]; then
    CACHE_VOLUME="hyprial-manylinux-evidence-$$-${RANDOM}"
fi
docker volume create "${CACHE_VOLUME}" >/dev/null

DOCKER_ARGS=(run --rm --network host
    -v "${OUT}:/work"
    -v "${CACHE_VOLUME}:/cache"
    -e "RUST_VERSION=${RUST_VERSION}"
    -e "SKIP_BEHAVIOR=${SKIP_BEHAVIOR}")
for var in HTTPS_PROXY https_proxy; do
    if [[ -n "${!var:-}" ]]; then
        DOCKER_ARGS+=(-e "${var}=${!var}")
    fi
done
if [[ -n "${CA_BUNDLE}" && -f "${CA_BUNDLE}" ]]; then
    DOCKER_ARGS+=(-v "$(readlink -f "${CA_BUNDLE}"):/evidence-ca.crt:ro")
fi

cat >"${OUT}/inner.sh" <<'INNER'
set -euo pipefail

if [[ -f /evidence-ca.crt ]]; then
    cp /evidence-ca.crt /etc/pki/ca-trust/source/anchors/evidence-ca.crt
    update-ca-trust extract
    export SSL_CERT_FILE=/evidence-ca.crt CURL_CA_BUNDLE=/evidence-ca.crt
    export CARGO_HTTP_CAINFO=/evidence-ca.crt PIP_CERT=/evidence-ca.crt
    export REQUESTS_CA_BUNDLE=/evidence-ca.crt
fi

export RUSTUP_HOME=/cache/rustup CARGO_HOME=/cache/cargo
export PATH="${CARGO_HOME}/bin:${PATH}"
export PYO3_PYTHON=/opt/python/cp312-cp312/bin/python
export CARGO_TERM_COLOR=never

REQUIRED_FAIL=0
RESULTS=()
verdict() {
    RESULTS+=("$1 $2")
    if [[ "$1" != "PASS" && "$3" == "required" ]]; then
        REQUIRED_FAIL=1
    fi
}

section() {
    printf '\n===== %s =====\n' "$1"
}

T=${SECONDS}
section "toolchain install"
if [[ -x "${CARGO_HOME}/bin/rustup" ]]; then
    echo "rustup already present in cache volume"
else
    mkdir -p /tmp/rustup-dl
    base=https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu
    curl --proto '=https' --tlsv1.2 -sSf "${base}/rustup-init" -o /tmp/rustup-dl/rustup-init
    curl --proto '=https' --tlsv1.2 -sSf "${base}/rustup-init.sha256" -o /tmp/rustup-dl/rustup-init.sha256
    want="$(awk '{print $1}' /tmp/rustup-dl/rustup-init.sha256)"
    got="$(sha256sum /tmp/rustup-dl/rustup-init | awk '{print $1}')"
    if [[ "${want}" != "${got}" ]]; then
        echo "rustup-init sha256 mismatch: want ${want} got ${got}" >&2
        exit 4
    fi
    echo "rustup-init sha256 ${got} (matches published checksum)"
    chmod +x /tmp/rustup-dl/rustup-init
    /tmp/rustup-dl/rustup-init -y --no-modify-path --profile minimal --default-toolchain "${RUST_VERSION}" >/tmp/rustup-dl/install.log 2>&1 || { cat /tmp/rustup-dl/install.log; exit 4; }
fi
rustup toolchain install "${RUST_VERSION}" --profile minimal >/dev/null 2>&1
rustup default "${RUST_VERSION}" >/dev/null
echo "rustc: $(rustc --version --verbose | tr '\n' ' ')"
echo "cargo: $(cargo --version)"
echo "cc: $(cc --version | head -1)"
echo "ldd: $(ldd --version | head -1)"
echo "PYO3_PYTHON: ${PYO3_PYTHON} ($(${PYO3_PYTHON} -c 'import sys; print(sys.version.split()[0])'))"
echo "auditwheel: $(auditwheel --version)"
TOOLCHAIN_SECONDS=$((SECONDS - T))

cd /work/src
T=${SECONDS}
section "product build (--features extension-module)"
set -x
CARGO_TARGET_DIR=/work/target-ext cargo build --release --locked --manifest-path crates/Cargo.toml -p hyprial-py --features extension-module >/work/out/build-ext.log 2>&1 && EXT_RC=0 || EXT_RC=$?
set +x
tail -3 /work/out/build-ext.log
BUILD_EXT_SECONDS=$((SECONDS - T))
EXT=/work/target-ext/release/libactors.so
if [[ "${EXT_RC}" == "0" && -f "${EXT}" ]]; then
    verdict PASS "product build (exit 0, ${BUILD_EXT_SECONDS}s)" required
    cp "${EXT}" /work/out/libactors.so
else
    verdict FAIL "product build (exit ${EXT_RC})" required
    cat /work/out/build-ext.log
fi

T=${SECONDS}
section "negative control build (no extension-module)"
set -x
CARGO_TARGET_DIR=/work/target-noext cargo build --release --locked --manifest-path crates/Cargo.toml -p hyprial-py >/work/out/build-noext.log 2>&1 && NOEXT_RC=0 || NOEXT_RC=$?
set +x
tail -3 /work/out/build-noext.log
BUILD_NOEXT_SECONDS=$((SECONDS - T))
NOEXT=/work/target-noext/release/libactors.so

if [[ -f /work/out/libactors.so ]]; then
    SO=/work/out/libactors.so
    section "product .so identity"
    ls -l "${SO}"
    sha256sum "${SO}"
    file "${SO}" 2>/dev/null || true

    section "readelf -d (product)"
    readelf -d "${SO}"
    NEEDED="$(readelf -d "${SO}" | awk '/\(NEEDED\)/{gsub(/[\[\]]/,"",$5); print $5}' | tr '\n' ' ')"
    echo "NEEDED: ${NEEDED}"
    if grep -q 'libpython' <<<"${NEEDED}"; then
        verdict FAIL "no libpython in DT_NEEDED (NEEDED: ${NEEDED})" required
    else
        verdict PASS "no libpython in DT_NEEDED (NEEDED: ${NEEDED})" required
    fi
    if readelf -d "${SO}" | grep -qE '\((RPATH|RUNPATH)\)'; then
        verdict FAIL "no RPATH/RUNPATH" required
    else
        verdict PASS "no RPATH/RUNPATH" required
    fi

    section "symbol versions (objdump -T / readelf -V)"
    objdump -T "${SO}" | grep -oE '\b[A-Z]+_[0-9]+(\.[0-9]+)*\b' | sort -u -V | tr '\n' ' '
    echo
    readelf -V "${SO}" | sed -n '/.gnu.version_r/,$p'
    MAX_GLIBC="$(objdump -T "${SO}" | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)*' | sed 's/GLIBC_//' | sort -u -V | tail -1)"
    echo "max GLIBC_ version: ${MAX_GLIBC}"
    if [[ "$(printf '%s\n%s\n' "${MAX_GLIBC}" 2.17 | sort -V | tail -1)" == "2.17" ]]; then
        verdict PASS "max GLIBC_ ${MAX_GLIBC} <= 2.17" required
    else
        verdict FAIL "max GLIBC_ ${MAX_GLIBC} > 2.17" required
    fi
    OTHER_VERS="$(objdump -T "${SO}" | grep -oE '\b(GLIBCXX|CXXABI|GCC)_[0-9]+(\.[0-9]+)*\b' | sort -u -V | tr '\n' ' ')"
    echo "non-glibc versioned deps: ${OTHER_VERS:-none}"
    PY_UNDEF="$(nm -D --undefined-only "${SO}" | grep -cE ' U _?Py' || true)"
    PY_DEF="$(nm -D --defined-only "${SO}" | grep -vE ' PyInit_' | grep -cE ' [TD] _?Py[A-Z_]' || true)"
    echo "undefined Py* symbols (resolved from host interpreter at load): ${PY_UNDEF}"
    echo "defined Py* C-API symbols (embedded libpython): ${PY_DEF}"
    echo "exported init symbol: $(nm -D --defined-only "${SO}" | grep -E 'PyInit_' || echo none)"

    section "auditwheel (throwaway wheel)"
    rm -rf /work/out/wheel-build /work/out/wheelhouse
    /opt/python/cp312-cp312/bin/python -I - "${SO}" /work/out/wheel-build <<'PYWHEEL'
import base64
import hashlib
import os
import sys
import zipfile

so, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
name, version, tag = "hyprial", "0.0.0", "cp312-abi3-linux_x86_64"
dist = f"{name}-{version}.dist-info"
files = {
    "hyprial/__init__.py": b"",
    "hyprial/_core/__init__.py": b"",
    "hyprial/_core/actors.abi3.so": open(so, "rb").read(),
    f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
    f"{dist}/WHEEL": f"Wheel-Version: 1.0\nGenerator: manylinux_native_evidence\nRoot-Is-Purelib: false\nTag: {tag}\n".encode(),
}
record = []
for path, data in files.items():
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    record.append(f"{path},sha256={digest},{len(data)}")
record.append(f"{dist}/RECORD,,")
files[f"{dist}/RECORD"] = ("\n".join(record) + "\n").encode()
wheel = os.path.join(out, f"{name}-{version}-{tag}.whl")
with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
    for path, data in files.items():
        info = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
        info.external_attr = (0o755 if path.endswith(".so") else 0o644) << 16
        zf.writestr(info, data, zipfile.ZIP_DEFLATED)
print(wheel)
PYWHEEL
    WHL="$(ls /work/out/wheel-build/*.whl)"
    auditwheel show "${WHL}" | tee /work/out/auditwheel-show.txt
    if tr '\n' ' ' </work/out/auditwheel-show.txt | grep -qE 'consistent with the following platform tag:[[:space:]]+"manylinux_2_17_x86_64"'; then
        verdict PASS "auditwheel show: consistent with manylinux_2_17_x86_64" required
    else
        verdict FAIL "auditwheel show: not consistent with manylinux_2_17_x86_64" required
    fi
    if auditwheel repair --plat manylinux_2_17_x86_64 -w /work/out/wheelhouse "${WHL}" >/work/out/auditwheel-repair.txt 2>&1; then
        REPAIRED="$(ls /work/out/wheelhouse/*.whl)"
        tail -2 /work/out/auditwheel-repair.txt
        echo "repaired wheel: $(basename "${REPAIRED}")"
        verdict PASS "auditwheel repair --plat manylinux_2_17_x86_64 -> $(basename "${REPAIRED}")" required
    else
        cat /work/out/auditwheel-repair.txt
        verdict FAIL "auditwheel repair --plat manylinux_2_17_x86_64" required
    fi

    section "import checks"
    rm -rf /work/out/stage
    mkdir -p /work/out/stage/hyprial/_core
    : >/work/out/stage/hyprial/__init__.py
    : >/work/out/stage/hyprial/_core/__init__.py
    cp "${SO}" /work/out/stage/hyprial/_core/actors.abi3.so
    for tagdir in /opt/python/cp3*; do
        tag="$(basename "${tagdir}")"
        minor="$(sed -E 's/^cp3([0-9]+)-.*/\1/' <<<"${tag}")"
        if (( minor < 12 )); then
            continue
        fi
        level=informational
        case "${tag}" in
            cp312-cp312|cp313-cp313|cp314-cp314) level=required ;;
        esac
        py="${tagdir}/bin/python"
        set +e
        outp="$(cd / && timeout 60 "${py}" -I -c "import sys; sys.path.insert(0, '/work/out/stage'); import sysconfig; import hyprial._core.actors as m; print(m.ENGINE_ABI_VERSION, sys.version.split()[0], 'gil_disabled=' + str(sysconfig.get_config_var('Py_GIL_DISABLED')), m.__file__)" 2>&1)"
        rc=$?
        set -e
        echo "[${tag}] exit=${rc} ${outp}" | head -5
        if [[ "${rc}" == "0" ]]; then
            verdict PASS "import ${tag}: $(tail -1 <<<"${outp}")" "${level}"
        else
            verdict FAIL "import ${tag} (${level}): $(tail -1 <<<"${outp}")" "${level}"
        fi
    done

    section "public-call behaviour suite (crates/hyprial-py/tests/engine_behavior.py)"
    BEHAVIOR_SECONDS=0
    if [[ "${SKIP_BEHAVIOR}" == "1" ]]; then
        verdict SKIPPED "engine_behavior.py (--skip-behavior)" informational
    else
        T=${SECONDS}
        for tag in cp312-cp312 cp313-cp313 cp314-cp314; do
            py="/opt/python/${tag}/bin/python"
            [[ -x "${py}" ]] || { verdict FAIL "engine_behavior ${tag}: interpreter missing" required; continue; }
            set +e
            (cd / && PYTHONPATH=/work/out/stage timeout 600 "${py}" /work/src/crates/hyprial-py/tests/engine_behavior.py) >"/work/out/behavior-${tag}.log" 2>&1
            rc=$?
            set -e
            summary="$(grep -E 'groups passed' "/work/out/behavior-${tag}.log" | tail -1)"
            echo "[${tag}] exit=${rc} ${summary}"
            grep -E '^FAIL' "/work/out/behavior-${tag}.log" || true
            if [[ "${rc}" == "0" ]]; then
                verdict PASS "engine_behavior ${tag}: ${summary}" required
            else
                verdict FAIL "engine_behavior ${tag}: exit ${rc} ${summary}" required
            fi
        done
        BEHAVIOR_SECONDS=$((SECONDS - T))
    fi
fi

section "negative control (no extension-module)"
echo "exit=${NOEXT_RC} (${BUILD_NOEXT_SECONDS}s)"
if [[ "${NOEXT_RC}" == "0" && -f "${NOEXT}" ]]; then
    ls -l "${NOEXT}"
    readelf -d "${NOEXT}" | grep -E '\((NEEDED|RPATH|RUNPATH)\)'
    NC_NEEDED="$(readelf -d "${NOEXT}" | awk '/\(NEEDED\)/{gsub(/[\[\]]/,"",$5); print $5}' | tr '\n' ' ')"
    NC_DEF="$(nm -D --defined-only "${NOEXT}" | grep -vE ' PyInit_' | grep -cE ' [TD] _?Py[A-Z_]' || true)"
    NC_UNDEF="$(nm -D --undefined-only "${NOEXT}" | grep -cE ' U _?Py' || true)"
    echo "NEEDED: ${NC_NEEDED}"
    echo "defined Py* C-API symbols (embedded libpython): ${NC_DEF}"
    echo "undefined Py* symbols: ${NC_UNDEF}"
    grep -E 'rustc-link-lib|python3' /work/target-noext/release/build/pyo3-ffi-*/output 2>/dev/null | sort -u || true
    if grep -q libpython <<<"${NC_NEEDED}"; then
        verdict PASS "negative control: without extension-module the .so has DT_NEEDED ${NC_NEEDED}" required
    elif (( NC_DEF > 0 )); then
        verdict PASS "negative control: without extension-module the .so statically embeds libpython (${NC_DEF} defined Py* symbols, $(stat -c %s "${NOEXT}") bytes vs product $(stat -c %s /work/out/libactors.so 2>/dev/null || echo '?') bytes)" required
    else
        verdict FAIL "negative control: no observable difference without extension-module" required
    fi
    set +e
    nc_imp="$(cd / && mkdir -p /work/out/stage-noext/hyprial/_core && : >/work/out/stage-noext/hyprial/__init__.py && : >/work/out/stage-noext/hyprial/_core/__init__.py && cp "${NOEXT}" /work/out/stage-noext/hyprial/_core/actors.abi3.so && timeout 60 /opt/python/cp314-cp314/bin/python -I -c "import sys; sys.path.insert(0, '/work/out/stage-noext'); import hyprial._core.actors as m; print(m.ENGINE_ABI_VERSION)" 2>&1)"
    nc_rc=$?
    set -e
    echo "negative-control import on cp314: exit=${nc_rc} $(tail -1 <<<"${nc_imp}")"
else
    grep -E 'error|undefined reference|cannot find' /work/out/build-noext.log | head -20
    verdict PASS "negative control: without extension-module the build fails to link (exit ${NOEXT_RC})" required
fi

section "durations (s)"
echo "toolchain=${TOOLCHAIN_SECONDS} build_ext=${BUILD_EXT_SECONDS} build_noext=${BUILD_NOEXT_SECONDS} behavior=${BEHAVIOR_SECONDS:-0}"

section "verdicts"
for line in "${RESULTS[@]}"; do
    echo "${line}"
done
if [[ "${REQUIRED_FAIL}" == "0" ]]; then
    echo "OVERALL PASS"
else
    echo "OVERALL FAIL"
fi
exit "${REQUIRED_FAIL}"
INNER
chmod +x "${OUT}/inner.sh"

{
    echo "manylinux native evidence"
    echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "ref: ${REF} -> ${SHA}"
    echo "image: ${IMAGE}"
    echo "image_digest: ${DIGEST}"
    echo "image_pull_seconds: ${PULL_SECONDS}"
    echo "rust_toolchain: ${RUST_VERSION}"
    echo "cache_volume: ${CACHE_VOLUME}"
    echo "out: ${OUT}"
} | tee "${OUT}/evidence.txt"

T0=${SECONDS}
set +e
docker "${DOCKER_ARGS[@]}" "${IMAGE}" bash /work/inner.sh 2>&1 | tee -a "${OUT}/evidence.txt"
RC=${PIPESTATUS[0]}
set -e
echo "container_seconds: $((SECONDS - T0))" | tee -a "${OUT}/evidence.txt"

if [[ "${NO_CACHE}" == "1" ]]; then
    docker volume rm "${CACHE_VOLUME}" >/dev/null 2>&1 || true
fi
exit "${RC}"
