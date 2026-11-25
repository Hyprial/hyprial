#!/usr/bin/env bash
# Package the native shell as a macOS arm64 HyprialOS.app and .dmg, locally.
#
# Decision record (file header; no other comments by repository rule):
# - Owner decision 2026-11-17: packaging is done by this one script, run by a
#   developer on a Mac during development without waiting for CI;
#   automated packaging on the CI runner was deferred. Owner decision
#   2026-11-20: packaging is not done in CI at all. The two manual workflows
#   that called this script (shell-macos-candidate.yml, ad hoc, and
#   shell-macos-signed.yml, --developer-id) are deleted; an operator runs it
#   by hand (packaging/clients/README.md "Package by hand"), choosing the
#   source commit and setting the source remote variable.
# - Without --developer-id (the default): candidate only. The app is signed ad hoc by Tauri
#   (`signingIdentity` "-"); the script never signs with a Developer ID, never
#   notarizes, never publishes or uploads and reads no secret. Stages, outputs
#   and file names are those of the ad-hoc script before milestone 2, with two
#   deliberate changes (PR #2980 reviews): verify_bundle also runs
#   packaging/clients/signing/archives.py on the built app (read only), so an
#   archive the signing tool would refuse fails the ordinary build, and
#   BUILD-INFO.txt says `signing: ad hoc` and `distributable: no`.
# - With --developer-id (milestone 2 of Developer ID signing, owner decision
#   2026-11-19, KP-100): the same build, then packaging/clients/sign_bundle.py
#   signs, notarizes and staples a copy of the app and builds the signed dmg.
#   Every Apple tool call (codesign --sign, notarytool, stapler, spctl) is
#   inside sign_bundle.py and its signing/ modules; this script only calls
#   it, in three stages that
#   exist only in this mode. The identity is explicit: --identity <40-hex
#   SHA-1> or --discover-identity (the single valid Developer ID Application
#   identity, sign_bundle's discovery); the notary profile is explicit
#   (--notary-profile); --source-sha is required (a signed artifact names a
#   clean commit) and --skip-smoke is refused (the smokes are part of the
#   proof). Malformed values exit 2 before anything runs. Signed stages:
#   source host node toolchain sign_preflight tauri_cli wheel stage
#   verify_staged build_app verify_bundle sign verify_signed smoke_sigterm
#   smoke_kill9 collect. sign_preflight (`sign_bundle.py --check-only`: the
#   identity is valid, a bounded 120 s test signature of a scratch file
#   succeeds, the notary profile answers `history`, no submission) runs
#   before the long build so a locked keychain or a wrong identity or
#   profile fails in minutes. verify_bundle still checks the ad-hoc build;
#   sign (two notary submissions, app zip then dmg, sign_bundle's order and
#   its wait, default 30 minutes, --notary-timeout-minutes to lengthen)
#   writes under the work directory; verify_signed (`--verify-only`) re-runs
#   codesign --verify --deep --strict, both Gatekeeper assessments, stapler
#   validate of app and dmg, the checksums and the launch check on that final
#   output, then attaches the final dmg read-only, copies the app out with
#   ditto to dist/shell-macos-work/final, gives the copy a quarantine
#   attribute and detaches; both smokes launch THAT copy (signing/modes.py).
#   Mounts under the signed work directories are detached before they are
#   removed (a SIGKILLed run can leave one attached).
# - Fail closed in that mode: any failed stage fails the run (exit 1); there
#   is no path that falls back to ad hoc. Collect copies the signed dmg
#   (`HyprialOS_<version>_aarch64-signed.dmg`), the regenerated
#   runtime-manifest.json and SHA256SUMS only when every stage passed and
#   they are the verified files, and
#   BUILD-INFO.txt then carries sign_bundle's record (signing method, Team ID,
#   notary submission ids) and whether the artifact is distributable. The
#   ad-hoc dmg of build_app is never collected in this mode: an ad-hoc file
#   in the output of a signed run could be taken for its product, and a run
#   without the switch builds an ad-hoc package. On any failure the signed
#   work directory is removed (a notarized dmg whose smokes failed is not a
#   release) and NOT-SIGNED.txt names the first stage that did not succeed
#   and the notary submission ids spent; the evidence JSON stays.
# - Distributable means the wheel inside records the public source remote:
#   collect checks the built wheel with scripts/ci/wheel_source_remote.py and
#   BUILD-INFO.txt says no, or "internal verification only": owner decision
#   2026-11-19, until the repository has moved to GitHub signed packages are
#   not distributed externally (the recorded commit may not exist on the
#   public remote, and an installed `hyprial gui dev` fetches it there);
#   after the move an externally distributed package must be built from a
#   commit that already exists on GitHub. The build never contacts GitHub.
#   The identity SHA-1 is never printed (`<identity>`, as sign_bundle.py
#   records it). This script never chooses the remote: the
#   caller sets the build hook's variable (the "Package by hand" commands set
#   it to the repository's public remote constant), as for any wheel build.
# - Stages, in this order, each announced with `==> <id>`: source, host,
#   node, toolchain, tauri_cli, wheel, stage, verify_staged, build_app,
#   verify_bundle, smoke_sigterm, smoke_kill9, collect. The first failing
#   stage stops the build path (later stages are reported as not run);
#   collect always runs, so evidence survives a failed run. The two smokes
#   are independent launches: a failing smoke_sigterm does not stop
#   smoke_kill9 (each reports its own result); they are NOT RUN only when an
#   earlier build-path stage failed. The summary at the end lists every stage
#   as PASSED, FAILED, SKIPPED (--skip-smoke) or NOT RUN, and the stage line
#   of a stage that did not run says NOT RUN too ("skipped" belongs to
#   --skip-smoke alone).
#   Every stage runs in a subshell with errexit on (the runner runs a `run:`
#   as `bash -e -o pipefail`, bash 3.2 on the Mac host), so a stage cannot
#   half-succeed; a tolerated absence is tested explicitly.
# - Pins live in this header's first variable block and nowhere else: uv
#   0.11.7, cargo 1.93.1, tauri-cli 2.12.2, Node major 24 and Python 3.12.
#   Version checks compare the exact version word, so 0.11.70 does not pass
#   for 0.11.7. The Python pin is exported as UV_PYTHON before any other
#   command, so it covers every uv call the script and its helpers make
#   (build-wheel.sh, stage_runtime.py, the verifiers); the job environment of
#   the old workflow carried it and the script did not (Mac verification
#   2026-11-18: a uv defaulting to a free-threaded 3.14 failed the wheel
#   stage). The toolchain stage prints the interpreter uv actually selects
#   and refuses anything but a 3.12 with the GIL.
# - Signing is switched off at the source: at start the script unsets every
#   APPLE_* variable (Tauri signs and notarizes from APPLE_SIGNING_IDENTITY,
#   APPLE_CERTIFICATE*, APPLE_API_*, APPLE_ID, APPLE_PASSWORD, APPLE_TEAM_ID)
#   and TAURI_SIGNING_PRIVATE_KEY*, so "never notarizes" holds even when a
#   developer's shell has them set. It prints the names, never a value.
# - Exit status: 0 every required stage passed; 1 a build or verification
#   stage failed; 2 a usage error or a refused input (unknown option, a
#   --source-sha that is malformed, is not HEAD or meets a dirty tree, a
#   non-empty --out), refused before anything is built or deleted.
# - Node 24 is host-provided, never installed (run 50856, 2026-11-17: the
#   Homebrew default `node` on the runner account is a newer major that the
#   machine owner keeps). A v24 `node` already on PATH is kept; otherwise
#   only NODE24_BIN_CANDIDATES (default: the keg-only Homebrew formula
#   /opt/homebrew/opt/node@24/bin) is searched, for a directory whose `node`
#   reports v24.* and that also has `npm`; it is put in front of PATH for the
#   rest of the run. Never downloads Node, never falls back to another
#   major, fails with the hint `brew install node@24`. The Intel prefix is
#   not listed (a Rosetta Node on the asserted arm64 host would be a silent
#   mismatch). The toolchain stage re-resolves `node` and `npm` and reads
#   `npm version --json`, so an `npm` under another Node is refused.
# - tauri-cli is built with `cargo install --locked` into a directory. Without
#   --tauri-cli-dir that is a fresh mktemp directory removed at exit, never a
#   persistent tool cache that a pushed branch could have replaced (owner
#   decision on PR #2889, 2026-11-17). --tauri-cli-dir is a developer
#   convenience: it names the directory and reuses an install whose version
#   is exactly the pin.
# - The locked PBS archive is cached under $CI_CACHE_ROOT/pbs-cache when the
#   runner provides CI_CACHE_ROOT, and under dist/shell-macos-work/pbs-cache
#   otherwise (owner decision 2026-11-18: GitHub Releases downloads stalled
#   from the Mac host; a persistent, SHA-256-verified archive removes that
#   download). stage_runtime.py verifies size and SHA-256 before reuse.
# - --source-sha <40-hex>: refuse (exit 2, before any stage) unless HEAD is
#   that commit and the tree is clean (untracked files count). Without it a
#   local build from a dirty tree
#   is allowed, but the summary and BUILD-INFO.txt state the commit, whether
#   the tree was dirty and the tool versions, so a dirty build is never taken
#   for a clean one.
# - Smokes need a window server. --skip-smoke is for a machine without one
#   and is reported as SKIPPED, never as passed; the manual smoke commands
#   are in packaging/clients/README.md.
# - Collect copies only what exists, writes SHA256SUMS only for a dmg and
#   runtime-manifest.json that are present (never an empty file; run 50856
#   uploaded a lone 0-byte SHA256SUMS), and when the build did not succeed
#   writes NOT-BUILT.txt naming the first stage that did not succeed with
#   every build-path stage's outcome. A missing dmg after a successful build
#   is an error. Stale dmg and app output of an earlier run is removed before
#   the build so it cannot be collected as this run's. A signed run collects
#   only what verify_signed checked: after the copy, the dmg, the manifest
#   and the regenerated SHA256SUMS must have the SHA-256 recorded in
#   verify-signed.json (`verifiedSha256`, and `finalCopy.dmgSha256` of the
#   dmg that was attached for the smokes), else nothing signed is collected.
#   A signed run writes NOT-SIGNED.txt as soon as --out exists ("did not
#   finish"); sign and verify_signed pass --mark-unfinished, so sign_bundle
#   writes UNFINISHED.txt into the signed work directory before renaming it
#   into place and into dist/shell-macos-work/final before copying the app
#   there (review 38872: no unmarked moment). Only a passing collect removes
#   them (NOT-SIGNED.txt last); a run killed before collect leaves them.
#   Ctrl-C during a smoke (measured 2026-11-20) failed that stage and
#   collect removed the signed work; the interrupted smoke stops its app and
#   the isolated home's daemon itself (PR #3022, measured on the Mac with two
#   SIGINTs: exit 130, nothing left). Collect
#   re-checks HEAD == --source-sha and a clean tree; a change collects nothing.
# - The WebKit data of the smoke lands in the account's
#   ~/Library/{WebKit,Caches}/com.hyprial.hyprialos and
#   ~/Library/HTTPStorages/com.hyprial.hyprialos.binarycookies whatever HOME
#   is (KP-096). The script lists those entries and never deletes them.
# - The verifier and the smokes make a short parent under SHORT_TMP (default
#   /tmp; the macOS AF_UNIX limit is 103 bytes) and every stage that makes one
#   removes it when empty on every exit path, failure included.
# - The daemon of the isolated home that `hyprial gui` leaves running is
#   expected and is stopped by the verifier and the smoke themselves
#   (packaging/clients/README.md "Daemon of the isolated home"); the script
#   only runs them.
# - The collection of a signed run lives in
#   scripts/build/macos-package/signed-collect.sh, sourced next to this file
#   (review 38872: this file had reached the 1000-line limit).
# - The file can be sourced without running (the tests call single stages with
#   fake tools on PATH); init_paths recomputes every path from ROOT.
# - Works on the bash 3.2 of macOS: no associative arrays, mapfile, or
#   empty-array expansion under `set -u`.

set -euo pipefail

UV_VERSION="0.11.7"
CARGO_VERSION="1.93.1"
TAURI_CLI_VERSION="2.12.2"
NODE_MAJOR="24"
PYTHON_VERSION="3.12"
SHORT_TMP="${SHORT_TMP:-/tmp}"
NODE24_BIN_CANDIDATES="${NODE24_BIN_CANDIDATES:-/opt/homebrew/opt/node@24/bin}"

export UV_PYTHON="${PYTHON_VERSION}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

clear_signing_variables() {
    local name cleared=""
    for name in $(compgen -v APPLE_) $(compgen -v TAURI_SIGNING_PRIVATE_KEY); do
        cleared="${cleared} ${name}"
        unset "${name}"
    done
    SIGNING_CLEARED="${cleared# }"
}

SIGNING_CLEARED=""
clear_signing_variables

init_paths() {
    DEFAULT_OUT="${ROOT}/dist/shell-macos"
    WORK="${ROOT}/dist/shell-macos-work"
    WHEEL_DIR="${WORK}/wheel"
    BUNDLE="${ROOT}/clients/native/src-tauri/target/release/bundle"
    APP="${BUNDLE}/macos/HyprialOS.app"
    SIGNED="${WORK}/signed"
    FINAL="${WORK}/final"
}

init_paths
WHEEL_GLOB="*-macosx_13_0_arm64.whl"
source "$(dirname "${BASH_SOURCE[0]}")/macos-package/signed-collect.sh"

OUT=""
DEVELOPER_ID=0
IDENTITY_ARG=""
NOTARY_PROFILE=""
NOTARY_MINUTES=""
TAURI_CLI_DIR=""
SKIP_SMOKE=0
SOURCE_SHA=""
OWN_TAURI_DIR=""
STATUS=""
FAILED=0
STOPPED=0

usage() {
    cat <<'EOF'
Usage: scripts/build/package-macos-shell.sh [options]

Package the native shell as HyprialOS.app and .dmg on a macOS arm64 Mac:
host check, Node 24 selection, pinned toolchain check, tauri-cli install,
wheel, runtime staging and verification, cargo tauri build, signature and
bundle check, the two launch smokes, collection of the results.
Without --developer-id: an ad-hoc signed candidate only, no Developer ID
signing, no notarization, no publishing, no upload, no secret.

Options:
  --out <dir>            output directory for the .app/.dmg checksums and
                         evidence (default: dist/shell-macos under the
                         repository, git-ignored and emptied on each run; any
                         other directory must not exist or must be empty)
  --tauri-cli-dir <dir>  install tauri-cli into <dir> and reuse an existing
                         install of exactly the pinned version (default: a
                         fresh temporary directory removed at exit)
  --skip-smoke           skip the two launch smokes (a machine without a
                         window server); reported as SKIPPED, never as passed
  --source-sha <40-hex>  refuse unless HEAD is this commit and the tree is clean
  -h, --help             show this help

Developer ID signing (off unless --developer-id is given):
  --developer-id         sign, notarize and staple with packaging/clients/
                         sign_bundle.py after the build; needs --source-sha,
                         one of --identity / --discover-identity and
                         --notary-profile; refuses --skip-smoke. The signed
                         dmg is named HyprialOS_<version>_aarch64-signed.dmg
                         and is collected only when every stage passed
  --identity <40-hex>    SHA-1 of the Developer ID Application identity
  --discover-identity    use the single valid Developer ID Application identity
  --notary-profile <p>   name of the stored notary credentials profile
  --notary-timeout-minutes <n>
                         notary wait per submission (default: sign_bundle's, 30)

Environment:
  NODE24_BIN_CANDIDATES  directories searched for a Node 24 with npm when the
                         node on PATH is not v24 (default:
                         /opt/homebrew/opt/node@24/bin)

Exit status:
  0  every required stage passed
  1  a build or verification stage failed (the summary names it)
  2  a usage error or a refused input: unknown option, --source-sha that is
     malformed, is not HEAD or meets a dirty tree, or a non-empty --out, or a
     signing option that is malformed, incomplete or given without
     --developer-id; nothing was built or deleted

The script pins Python 3.12 itself (UV_PYTHON) and unsets every APPLE_* and
TAURI_SIGNING_* variable before it starts: without --developer-id it never
signs with a Developer ID and never notarizes.
EOF
}

die_usage() {
    echo "$1" >&2
    usage >&2
    exit 2
}

refuse() {
    echo "refused: $1" >&2
    exit 2
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --out) [ "$#" -ge 2 ] || die_usage "--out needs a directory"; OUT="$2"; shift 2 ;;
            --tauri-cli-dir) [ "$#" -ge 2 ] || die_usage "--tauri-cli-dir needs a directory"; TAURI_CLI_DIR="$2"; shift 2 ;;
            --source-sha) [ "$#" -ge 2 ] || die_usage "--source-sha needs a commit"; SOURCE_SHA="$2"; shift 2 ;;
            --skip-smoke) SKIP_SMOKE=1; shift ;;
            --developer-id) DEVELOPER_ID=1; shift ;;
            --identity) [ "$#" -ge 2 ] || die_usage "--identity needs a SHA-1"; [ -z "${IDENTITY_ARG}" ] || die_usage "give one of --identity and --discover-identity"; check_identity "$2"; IDENTITY_ARG="--identity=$2"; shift 2 ;;
            --discover-identity) [ -z "${IDENTITY_ARG}" ] || die_usage "give one of --identity and --discover-identity"; IDENTITY_ARG="--discover-identity"; shift ;;
            --notary-profile) [ "$#" -ge 2 ] || die_usage "--notary-profile needs a name"; NOTARY_PROFILE="$2"; shift 2 ;;
            --notary-timeout-minutes) [ "$#" -ge 2 ] || die_usage "--notary-timeout-minutes needs a number"; NOTARY_MINUTES="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) die_usage "unknown option: $1" ;;
        esac
    done
    check_signing_options
}

check_identity() {
    case "$1" in
        *[!0-9A-Fa-f]*|"") die_usage "--identity must be the 40-hex SHA-1 of a Developer ID Application identity" ;;
    esac
    [ "${#1}" -eq 40 ] || die_usage "--identity must be the 40-hex SHA-1 of a Developer ID Application identity"
}

check_signing_options() {
    if [ "${DEVELOPER_ID}" -eq 0 ]; then
        if [ -n "${IDENTITY_ARG}${NOTARY_PROFILE}${NOTARY_MINUTES}" ]; then
            die_usage "--identity, --discover-identity, --notary-profile and --notary-timeout-minutes need --developer-id"
        fi
        return 0
    fi
    [ -n "${IDENTITY_ARG}" ] || die_usage "--developer-id needs --identity <40-hex> or --discover-identity"
    [ -n "${SOURCE_SHA}" ] || die_usage "--developer-id needs --source-sha: a signed artifact names a clean commit"
    [ "${SKIP_SMOKE}" -eq 0 ] || die_usage "--developer-id refuses --skip-smoke: both smokes run on the signed app"
    case "${NOTARY_PROFILE}" in
        ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) die_usage "--developer-id needs --notary-profile <name> (letters, digits, '.', '_' or '-')" ;;
    esac
    [ "${#NOTARY_PROFILE}" -le 64 ] || die_usage "--notary-profile is longer than 64 characters"
    if [ -n "${NOTARY_MINUTES}" ]; then
        case "${NOTARY_MINUTES}" in
            *[!0-9]*|0*) die_usage "--notary-timeout-minutes must be 1..1440" ;;
        esac
        [ "${#NOTARY_MINUTES}" -le 4 ] && [ "${NOTARY_MINUTES}" -le 1440 ] || die_usage "--notary-timeout-minutes must be 1..1440"
    fi
}

absolute() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$(pwd)" "$1" ;;
    esac
}

cleanup() {
    if [ -n "${OWN_TAURI_DIR}" ]; then
        rm -rf "${OWN_TAURI_DIR}"
    fi
}

set_status() {
    STATUS="${STATUS}$1=$2
"
}

status_of() {
    local line
    while IFS= read -r line; do
        case "${line}" in
            "$1="*) printf '%s\n' "${line#*=}"; return 0 ;;
        esac
    done <<EOF
${STATUS}
EOF
    printf 'not-run\n'
}

run_stage() {
    local id="$1" fn="$2" scope="${3:-stops}" rc
    echo "==> ${id}"
    set +e
    ( set -euo pipefail; "${fn}" )
    rc=$?
    set -e
    if [ "${rc}" -eq 0 ]; then
        set_status "${id}" success
    else
        set_status "${id}" failure
        FAILED=1
        if [ "${scope}" = stops ]; then
            STOPPED=1
        fi
        echo "stage ${id} failed (exit ${rc})" >&2
    fi
}

skip_stage() {
    echo "==> $1 ($2)"
    set_status "$1" "$3"
}

short_parent() {
    TEMP_PARENT="$(mktemp -d "${SHORT_TMP}/$1.XXXXXX")"
    trap remove_short_parent EXIT
}

remove_short_parent() {
    if [ -n "${TEMP_PARENT:-}" ]; then
        rmdir "${TEMP_PARENT}" 2>/dev/null || true
    fi
}

stage_source() {
    local head dirty
    head="$(git -C "${ROOT}" rev-parse HEAD)"
    dirty=no
    if [ -n "$(git -C "${ROOT}" status --porcelain)" ]; then
        dirty=yes
    fi
    printf 'commit=%s\ndirty=%s\n' "${head}" "${dirty}" > "${WORK}/source-info.txt"
    echo "commit ${head}, tree dirty: ${dirty}"
    if [ "${dirty}" = yes ] && [ -z "${SOURCE_SHA}" ]; then
        echo "warning: the tree is dirty; this build is not reproducible from the commit alone"
    fi
}

check_source_sha() {
    local head
    [ -n "${SOURCE_SHA}" ] || return 0
    case "${SOURCE_SHA}" in
        *[!0-9a-f]*) refuse "invalid source sha: ${SOURCE_SHA}" ;;
    esac
    [ "${#SOURCE_SHA}" -eq 40 ] || refuse "invalid source sha length: ${SOURCE_SHA}"
    head="$(git -C "${ROOT}" rev-parse HEAD)"
    [ "${head}" = "${SOURCE_SHA}" ] || refuse "HEAD ${head} is not the requested source ${SOURCE_SHA}"
    if [ -n "$(git -C "${ROOT}" status --porcelain)" ]; then
        git -C "${ROOT}" status --short >&2
        refuse "the tree is not clean; --source-sha needs a clean tree"
    fi
}

stage_host() {
    local host
    host="$(uname -s)/$(uname -m)"
    case "${host}" in
        Darwin/arm64) echo "host ${host}" ;;
        *) echo "this script packages macOS arm64 only, host is ${host}"; exit 1 ;;
    esac
    sw_vers
}

stage_node() {
    local current="none" dir version
    rm -f "${WORK}/node-bin"
    if command -v node >/dev/null; then
        current="$(node --version)"
    fi
    case "${current}" in
        v${NODE_MAJOR}.*) echo "node on PATH is $(command -v node) ${current}"; exit 0 ;;
    esac
    for dir in ${NODE24_BIN_CANDIDATES}; do
        if [ -x "${dir}/node" ] && [ -x "${dir}/npm" ]; then
            version="$("${dir}/node" --version)"
            case "${version}" in
                v${NODE_MAJOR}.*)
                    echo "${dir}" > "${WORK}/node-bin"
                    echo "node on PATH is ${current}; the following stages use ${dir}/node ${version}"
                    exit 0
                    ;;
            esac
        fi
    done
    echo "the wheel's GUI build needs Node ${NODE_MAJOR}: node on PATH is ${current} and no node and npm reporting v${NODE_MAJOR} in ${NODE24_BIN_CANDIDATES}"
    echo "install it on the host with: brew install node@${NODE_MAJOR}"
    exit 1
}

version_word_is() {
    case "$1" in
        "$2 $3"|"$2 $3 "*) return 0 ;;
    esac
    return 1
}

stage_toolchain() {
    local tool uv_out cargo_out npm_node python_out
    for tool in git uv cargo node npm codesign hdiutil; do
        command -v "${tool}" || { echo "missing ${tool}"; exit 1; }
    done
    uv_out="$(uv --version)"
    version_word_is "${uv_out}" uv "${UV_VERSION}" || { echo "unexpected uv: ${uv_out} (pinned ${UV_VERSION})"; exit 1; }
    cargo_out="$(cargo --version)"
    version_word_is "${cargo_out}" cargo "${CARGO_VERSION}" || { echo "unexpected cargo: ${cargo_out} (pinned ${CARGO_VERSION})"; exit 1; }
    case "$(node --version)" in
        v${NODE_MAJOR}.*) echo "$(command -v node) $(node --version)" ;;
        *) echo "the wheel's GUI build needs Node ${NODE_MAJOR}, found $(node --version)"; exit 1 ;;
    esac
    npm_node="$(npm version --json | grep '"node":' | tr -d ' ",')"
    case "${npm_node}" in
        node:${NODE_MAJOR}.*) echo "$(command -v npm) $(npm --version) ${npm_node}" ;;
        *) echo "npm runs under ${npm_node}; the wheel's GUI build needs Node ${NODE_MAJOR} ($(command -v npm))"; exit 1 ;;
    esac
    python_out="$(uv run --no-project python -c 'import sys, sysconfig; print("%s %s gil-disabled=%s" % (sys.executable, ".".join(str(part) for part in sys.version_info[:3]), sysconfig.get_config_var("Py_GIL_DISABLED") or 0))')"
    echo "python (UV_PYTHON=${UV_PYTHON:-unset}) ${python_out}"
    case "${python_out}" in
        *" ${PYTHON_VERSION}."*" gil-disabled=0") ;;
        *) echo "uv selects an unsupported interpreter: ${python_out} (pinned ${PYTHON_VERSION}, with the GIL)"; exit 1 ;;
    esac
    {
        echo "uv=${uv_out}"
        echo "python=${python_out}"
        echo "cargo=${cargo_out}"
        echo "node=$(node --version)"
        echo "npm=$(npm --version)"
        echo "tauri-cli=pinned ${TAURI_CLI_VERSION}"
    } > "${WORK}/tool-versions.txt"
}

stage_tauri_cli() {
    local installed=""
    if [ -x "${TAURI_CLI_DIR}/bin/cargo-tauri" ]; then
        installed="$("${TAURI_CLI_DIR}/bin/cargo-tauri" --version 2>/dev/null || true)"
    fi
    if version_word_is "${installed}" tauri-cli "${TAURI_CLI_VERSION}"; then
        echo "reusing tauri-cli ${TAURI_CLI_VERSION} in ${TAURI_CLI_DIR}"
    else
        cargo install tauri-cli --version "${TAURI_CLI_VERSION}" --locked --root "${TAURI_CLI_DIR}"
    fi
    installed="$("${TAURI_CLI_DIR}/bin/cargo-tauri" --version)"
    version_word_is "${installed}" tauri-cli "${TAURI_CLI_VERSION}" || { echo "unexpected tauri-cli: ${installed}"; exit 1; }
    echo "${installed}"
}

stage_wheel() {
    cd "${ROOT}"
    rm -rf "${WHEEL_DIR}"
    bash scripts/build/build-wheel.sh "${WHEEL_DIR}"
    test "$(find "${WHEEL_DIR}" -maxdepth 1 -name "${WHEEL_GLOB}" | wc -l | tr -d ' ')" -eq 1
}

stage_stage() {
    local wheel cache_root
    cd "${ROOT}"
    wheel="$(find "${WHEEL_DIR}" -maxdepth 1 -name "${WHEEL_GLOB}" -print -quit)"
    cache_root="${CI_CACHE_ROOT:-${WORK}}"
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/stage_runtime.py \
        --target aarch64-apple-darwin --wheel "${wheel}" \
        --cache-dir "${cache_root}/pbs-cache"
}

stage_verify_staged() {
    cd "${ROOT}"
    short_parent hsv
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/verify_runtime.py \
        --runtime clients/native/src-tauri/runtime --root-parent "${TEMP_PARENT}" \
        --evidence "${OUT}/verify-staged.json"
}

stage_build_app() {
    rm -rf "${BUNDLE}/dmg" "${BUNDLE}/macos"
    cd "${ROOT}/clients/native/src-tauri"
    CI=true cargo tauri build --bundles app,dmg --config ../../../packaging/clients/tauri.bundle.json -- --locked
}

stage_verify_bundle() {
    cd "${ROOT}"
    codesign --verify --deep --strict --verbose=2 "${APP}"
    codesign -dv "${APP}" 2>&1 | grep -E '^(Identifier|Signature)='
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/signing/archives.py --app "${APP}"
    short_parent hsv
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/verify_runtime.py \
        --runtime "${APP}/Contents/Resources/runtime" --root-parent "${TEMP_PARENT}" \
        --evidence "${OUT}/verify-bundle.json"
}

stage_smoke_sigterm() {
    cd "${ROOT}"
    short_parent hsm
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/smoke_bundle.py \
        --app "${APP}" \
        --root-parent "${TEMP_PARENT}" --stop sigterm --second-start \
        --evidence "${OUT}/smoke-sigterm.json"
}

stage_smoke_kill9() {
    cd "${ROOT}"
    short_parent hsm
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/smoke_bundle.py \
        --app "${APP}" \
        --root-parent "${TEMP_PARENT}" --stop kill9 --app-home \
        --evidence "${OUT}/smoke-kill9.json"
}

APP_DATA_ENTRIES="WebKit/com.hyprial.hyprialos
Caches/com.hyprial.hyprialos
HTTPStorages/com.hyprial.hyprialos.binarycookies
Logs/com.hyprial.hyprialos
Application Support/com.hyprial.hyprialos"

list_app_data() {
    local entry
    while IFS= read -r entry; do
        ls -ld "${HOME}/Library/${entry}" 2>/dev/null || true
    done <<EOF
${APP_DATA_ENTRIES}
EOF
}

write_not_built() {
    local blocker="" id outcome
    for id in ${BUILD_PATH}; do
        outcome="$(status_of "${id}")"
        if [ -z "${blocker}" ] && [ "${outcome}" != success ]; then
            blocker="${id} (${outcome})"
        fi
    done
    {
        echo "no app or dmg was built; the first stage that did not succeed: ${blocker}"
        for id in ${BUILD_PATH}; do
            echo "${id}=$(status_of "${id}")"
        done
    } > "${OUT}/NOT-BUILT.txt"
}

write_build_info() {
    local commit="unknown" dirty="unknown"
    if [ -f "${WORK}/source-info.txt" ]; then
        commit="$(sed -n 's/^commit=//p' "${WORK}/source-info.txt")"
        dirty="$(sed -n 's/^dirty=//p' "${WORK}/source-info.txt")"
    fi
    {
        echo "commit: ${commit}"
        echo "tree dirty: ${dirty}"
        echo "requested source sha: ${SOURCE_SHA:-none}"
        echo "host: $(uname -s)/$(uname -m)"
        echo "tool versions:"
        if [ -f "${WORK}/tool-versions.txt" ]; then
            sed 's/^/  /' "${WORK}/tool-versions.txt"
        else
            echo "  not checked (the toolchain stage did not run)"
        fi
    } > "${OUT}/BUILD-INFO.txt"
}

stage_collect() {
    local dmgs=0 dmg file build
    cd "${ROOT}"
    mkdir -p "${OUT}"
    if [ "${DEVELOPER_ID}" -eq 1 ]; then
        collect_signed
        exit 0
    fi
    build="$(status_of build_app)"
    for dmg in "${BUNDLE}"/dmg/*.dmg; do
        if [ -f "${dmg}" ]; then
            cp "${dmg}" "${OUT}/"
            dmgs=$((dmgs + 1))
        fi
    done
    if [ "${build}" = success ] && [ "${dmgs}" -eq 0 ]; then
        echo "build succeeded but no dmg"
        exit 1
    fi
    if [ "$(status_of stage)" = success ] && [ -f clients/native/src-tauri/runtime/runtime-manifest.json ]; then
        cp clients/native/src-tauri/runtime/runtime-manifest.json "${OUT}/"
    fi
    if [ "${build}" != success ]; then
        write_not_built
    fi
    write_build_info
    printf '%s\n' 'signing: ad hoc (Tauri, signingIdentity "-"); no Developer ID, not notarized' 'distributable: no (an ad-hoc candidate for internal testing)' >> "${OUT}/BUILD-INFO.txt"
    rm -f "${OUT}/SHA256SUMS"
    for file in "${OUT}"/*.dmg "${OUT}/runtime-manifest.json"; do
        if [ -f "${file}" ]; then
            (cd "${OUT}" && shasum -a 256 "${file##*/}") >> "${OUT}/SHA256SUMS"
        fi
    done
    if [ -f "${OUT}/SHA256SUMS" ]; then
        cat "${OUT}/SHA256SUMS"
    fi
    ls -l "${OUT}"
}

stage_sign_preflight() {
    cd "${ROOT}"
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/sign_bundle.py --check-only \
        "${IDENTITY_ARG}" --notary-profile "${NOTARY_PROFILE}" --evidence "${OUT}/sign-preflight.json"
}

detach_mounts_under() {
    local real device kind mount
    real="$(cd "$1" 2>/dev/null && pwd -P)" || return 0
    hdiutil info | while IFS="$(printf '\t')" read -r device kind mount; do
        mount="${mount:-${kind}}"
        case "${device}|${mount}" in
            /dev/*"|$1/"*|/dev/*"|${real}/"*)
                echo "detaching ${mount} (${kind})"
                hdiutil detach "${mount}" >/dev/null 2>&1 || hdiutil detach -force "${mount}" || exit 1
                ;;
        esac
    done
}

remove_signed_work() {
    local dir
    for dir in "${SIGNED}" "${SIGNED}.unfinished" "${FINAL}"; do
        detach_mounts_under "${dir}" || { echo "a dmg stays attached under ${dir}; it is left in place"; return 1; }
        rm -rf "${dir}"
    done
}

stage_sign() {
    cd "${ROOT}"
    remove_signed_work
    short_parent hss
    set -- --app "${APP}" "${IDENTITY_ARG}" --notary-profile "${NOTARY_PROFILE}" \
        --out "${SIGNED}" --evidence "${OUT}/sign-bundle.json" --root-parent "${TEMP_PARENT}"
    if [ -n "${NOTARY_MINUTES}" ]; then
        set -- "$@" --notary-timeout-minutes "${NOTARY_MINUTES}"
    fi
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/sign_bundle.py "$@" --mark-unfinished
    test -f "${SIGNED}/UNFINISHED.txt"
}

stage_verify_signed() {
    cd "${ROOT}"
    detach_mounts_under "${FINAL}"
    rm -rf "${FINAL}"
    short_parent hss
    uv run --no-project --python "${PYTHON_VERSION}" python packaging/clients/sign_bundle.py --verify-only \
        "${IDENTITY_ARG}" --out "${SIGNED}" --evidence "${OUT}/verify-signed.json" --root-parent "${TEMP_PARENT}" \
        --extract-to "${FINAL}" --mark-unfinished
    test -f "${FINAL}/UNFINISHED.txt"
}

BUILD_PATH="source host node toolchain tauri_cli wheel stage verify_staged build_app"
ALL_STAGES="source host node toolchain tauri_cli wheel stage verify_staged build_app verify_bundle smoke_sigterm smoke_kill9 collect"
SIGNED_BUILD_PATH="source host node toolchain sign_preflight tauri_cli wheel stage verify_staged build_app"
SIGNED_STAGES="source host node toolchain sign_preflight tauri_cli wheel stage verify_staged build_app verify_bundle sign verify_signed smoke_sigterm smoke_kill9 collect"

prepare_signed_mode() {
    unset HYPRIAL_SIGN_BUNDLE_TOOL_PATH
    BUILD_PATH="${SIGNED_BUILD_PATH}"
    ALL_STAGES="${SIGNED_STAGES}"
    case "${IDENTITY_ARG}" in
        --identity=*) echo "Developer ID signing requested (--developer-id): identity <identity> (given, not printed), notary profile ${NOTARY_PROFILE}" ;;
        *) echo "Developer ID signing requested (--developer-id): the single valid identity is discovered, notary profile ${NOTARY_PROFILE}" ;;
    esac
}

signed_stage() {
    if [ "${DEVELOPER_ID}" -eq 1 ]; then
        build_stage "$1"
    fi
}

build_stage() {
    local id="$1"
    if [ "${STOPPED}" -eq 1 ]; then
        skip_stage "${id}" "NOT RUN: an earlier stage failed" not-run
        return 0
    fi
    run_stage "${id}" "stage_${id}"
}

smoke_stage() {
    local id="$1"
    if [ "${SKIP_SMOKE}" -eq 1 ]; then
        skip_stage "${id}" "SKIPPED: --skip-smoke" skipped
        return 0
    fi
    if [ "${STOPPED}" -eq 1 ]; then
        skip_stage "${id}" "NOT RUN: an earlier stage failed" not-run
        return 0
    fi
    run_stage "${id}" "stage_${id}" independent
}

print_summary() {
    local id outcome
    echo
    echo "==> summary"
    if [ -f "${WORK}/source-info.txt" ]; then
        echo "commit $(sed -n 's/^commit=//p' "${WORK}/source-info.txt"), tree dirty: $(sed -n 's/^dirty=//p' "${WORK}/source-info.txt")"
    fi
    for id in ${ALL_STAGES}; do
        outcome="$(status_of "${id}")"
        case "${outcome}" in
            success) outcome=PASSED ;;
            failure) outcome=FAILED ;;
            skipped) outcome="SKIPPED (not a pass)" ;;
            not-run) outcome="NOT RUN" ;;
        esac
        printf '  %-15s %s\n' "${id}" "${outcome}"
    done
    if [ "${FAILED}" -eq 0 ]; then
        echo "result: every required stage passed"
    else
        echo "result: FAILED"
    fi
}

main() {
    parse_args "$@"
    cd "${ROOT}"
    check_source_sha

    if [ -z "${OUT}" ]; then
        OUT="${DEFAULT_OUT}"
        rm -rf "${OUT}"
    else
        OUT="$(absolute "${OUT}")"
        if [ -d "${OUT}" ] && [ -n "$(ls -A "${OUT}")" ]; then
            refuse "--out ${OUT} is not empty; give an empty or new directory"
        fi
    fi
    trap cleanup EXIT
    if [ "${DEVELOPER_ID}" -eq 1 ]; then
        prepare_signed_mode
    fi
    if [ -n "${SIGNING_CLEARED}" ]; then
        echo "signing variables cleared from the environment: ${SIGNING_CLEARED}"
    fi
    if [ -z "${TAURI_CLI_DIR}" ]; then
        OWN_TAURI_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tauri-cli.XXXXXX")"
        TAURI_CLI_DIR="${OWN_TAURI_DIR}"
    else
        TAURI_CLI_DIR="$(absolute "${TAURI_CLI_DIR}")"
    fi
    mkdir -p "${WORK}"
    rm -f "${WORK}/source-info.txt" "${WORK}/tool-versions.txt" "${WORK}/node-bin"

    build_stage source
    if [ "${STOPPED}" -eq 0 ]; then
        mkdir -p "${OUT}"
        if [ "${DEVELOPER_ID}" -eq 1 ]; then
            write_not_signed "unknown: the run did not finish (it was killed or is still running)"
        fi
    fi
    build_stage host
    build_stage node
    if [ -f "${WORK}/node-bin" ]; then
        PATH="$(cat "${WORK}/node-bin"):${PATH}"
        export PATH
    fi
    build_stage toolchain
    signed_stage sign_preflight
    build_stage tauri_cli
    PATH="${TAURI_CLI_DIR}/bin:${PATH}"
    export PATH
    build_stage wheel
    build_stage stage
    build_stage verify_staged
    build_stage build_app
    build_stage verify_bundle
    signed_stage sign
    signed_stage verify_signed
    if [ "${DEVELOPER_ID}" -eq 1 ]; then
        APP="${FINAL}/app/HyprialOS.app"
    fi
    smoke_stage smoke_sigterm
    smoke_stage smoke_kill9

    list_app_data
    echo "==> collect"
    set +e
    ( set -euo pipefail; stage_collect )
    rc=$?
    set -e
    if [ "${rc}" -eq 0 ]; then
        set_status collect success
    else
        set_status collect failure
        FAILED=1
    fi

    print_summary
    if [ -d "${OUT}" ]; then
        print_summary >> "${OUT}/BUILD-INFO.txt"
    fi
    if [ "${FAILED}" -ne 0 ]; then
        exit 1
    fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
