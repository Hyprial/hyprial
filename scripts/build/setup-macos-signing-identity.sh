#!/usr/bin/env bash
# Install an Apple "Developer ID Application" signing identity and a
# notarytool credential profile on the packaging Mac, by hand, over SSH,
# without Xcode (Command Line Tools only).
#
# Decision record (file header; no other comments by repository rule):
# - Owner request 2026-11-18: the machine owner holds the Apple Developer
#   account and installs the certificate himself. This script only prepares
#   the identity; signing and notarizing the app stay deferred
#   (packaging/clients/README.md "Later: Developer ID and notarization
#   (deferred)", KP-100). No workflow runs this script and none may
#   (tests/tooling/release/shell/signing/test_identity_setup_policy.py).
# - Security consequence, accepted by the owner: on this Mac the CI runner
#   runs under the same macOS account. Once the identity is in the login
#   keychain, any workflow on any pushed branch of any repository that can
#   use the runner can sign with it while that keychain is unlocked (KP-100).
#   The cheaper mitigation is a separate keychain file (--keychain <path>),
#   kept off the account's keychain search list and unlocked only when
#   signing. codesign does not find an identity whose keychain is not on the
#   search list (measured on macOS 26.5.2 for PR #2907: `codesign --keychain <file>` alone
#   answers "no identity found"), so signing from such a keychain needs it on
#   the search list of the signing process, for example through a HOME whose
#   Library/Keychains/login.keychain-db links to it, as `check` does.
# - Subcommands, in the order the owner runs them: csr, import, notary;
#   check is read-only. The guided entry scripts/build/import-app-dev-id.sh
#   (owner request 2026-11-18: one command for the whole procedure) runs
#   them and adds no logic of its own; for it there are two helpers: `status`
#   (read-only key=value progress: files in the private directory, the
#   certificate's parse/match/type/expiry/SHA-1 and the Team ID its OU
#   suggests, the identity and intermediate in the keychain) and
#   `intermediate` (download or read the G2 intermediate, check the pin,
#   write it as DER), which `import` shares. Exit status: 0 ok; 1 a step failed; 2 a usage error
#   or a refused input (unknown option, bad value, an unsafe directory, an
#   existing key, a certificate of another key or type, an expired
#   certificate, an intermediate that does not match the pin, not macOS).
#   Every refusal of `import` happens before the keychain is touched.
# - Tools are /usr/bin ones: PATH is reset to /usr/bin:/bin:/usr/sbin:/sbin,
#   so a Homebrew OpenSSL 3 never makes the key or reads the certificate
#   (/usr/bin/openssl is LibreSSL 3.3.6 on the target Mac). The test-only
#   variable HYPRIAL_SIGNING_SETUP_TOOL_PATH replaces that PATH (fake tools
#   in tests/tooling/release/shell/signing); the script announces it on
#   stderr whenever it is set.
# - Secrets: the keychain password is read once with `read -s` from stdin
#   (on a terminal, echo is turned off with `stty -echo` before the prompt
#   is printed and restored afterwards and on every exit: measured
#   2026-11-18 on a pty, input sent right after the prompt was echoed both
#   with `printf` + `read -s` and with `read -s -p`, which print the prompt
#   before they turn echo off; with `stty -echo` first nothing was echoed),
#   held in a shell variable, and given to `security -i` on its stdin by the
#   printf builtin through a pipe (one security process per command, because
#   `security -i` reports only the last command's status). It is never in
#   an argv, never echoed, never written to disk (no here-documents or
#   here-strings: bash 3.2 backs them with temp files), and is scrubbed from
#   any security output that is shown. Inside `security -i` a value is
#   double-quoted with backslash and double quote escaped by a backslash
#   (measured with a password containing ", \, ', space, $ and !). The
#   notarytool app-specific password is never handled here: notarytool asks
#   for it itself, and `notary` refuses every option it does not know.
# - Measured on macOS 26.5.2 with temp keychains under an isolated HOME:
#   * a PEM key (LibreSSL `genrsa`) imported with `-t priv -f openssl` and
#     the DER certificate imported with `-t cert -f x509` pair into one
#     identity, in either order. No p12 is built, so no p12 password exists.
#   * a second import of the same key or certificate exits 1 with "already
#     exists". `import` therefore looks the identity up by the
#     certificate's SHA-1 first, tolerates "already exists", and the final
#     exactly-once check decides.
#   * the imported key's label is "Imported Private Key" whatever the file
#     is called; `set-key-partition-list` is narrowed to
#     `-s -t private -l "Imported Private Key"` (bare `-s` rewrites the
#     partition list of every signing key in the keychain). Keys imported
#     earlier by `security import` carry the same label and get the same
#     list (apple-tool:,apple:); the script prints how many keys matched.
#   * `set-key-partition-list` without -k prompts "(deprecated) password to
#     unlock" on a terminal and reads the password there without echo; with
#     an already unlocked keychain it exits 0 even with a wrong -k password,
#     and on keychains made by `security create-keychain` codesign signed
#     with no partition list at all, so its effect on the real login
#     keychain is UNKNOWN until the owner runs `check`.
#   * a locked keychain still lists identities but codesign fails with
#     errSecInternalComponent; `check` reports that as a FAIL with the
#     unlock hint.
#   * `find-identity -v` lists only identities whose chain validates; a
#     self-signed placeholder is never valid, so `import --allow-other-type`
#     reports it as not valid instead of failing.
# - Developer ID G2 intermediate pin (DEVELOPER_ID_G2_SHA256): obtained
#   2026-11-18 by downloading the URL below over HTTPS (a direct request was
#   answered 403 with an HTML page; through the configured proxy it returned
#   the 1090-byte DER certificate). Apple's page
#   https://www.apple.com/certificateauthority/ publishes no fingerprint; it
#   links that file as "Developer ID - G2 (Expiring 09/17/2031 00:00:00
#   UTC)", which matches the certificate's notAfter. The certificate's
#   signature verifies with /usr/bin/openssl against the "Apple Root CA"
#   certificate shipped in macOS (/System/Library/Keychains/
#   SystemRootCertificates.keychain, SHA-256 B0B1730E...1001F024). SHA-1
#   5B45F61068B29FCC8FFFF1A7E99B78DA9E9C4635, subject "Developer ID
#   Certification Authority", OU G2. A downloaded or --intermediate file is
#   used only when its SHA-256 equals the pin. Manual re-check:
#   curl -fsSo g2.cer <URL> && /usr/bin/openssl x509 -inform DER -in g2.cer
#   -noout -fingerprint -sha256
# - SSH: the login keychain is locked in an SSH session until it is
#   unlocked in that session (`import` unlocks it with the password it
#   reads; `notary` and `check` do not unlock and say how to). A CI runner
#   started in the GUI session sees a different keychain state; whether a
#   job there can use the identity is UNKNOWN until tried.
# - Never changes the keychain search list or the default keychain, never
#   reads or writes any keychain but the selected one (default
#   $HOME/Library/Keychains/login.keychain-db), imports the key for
#   /usr/bin/codesign only (-T, never the allow-all flag).
# - The private directory (csr --dir) is created 0700 and refused when it is
#   a symlink, open to group or others, not owned by the caller, or inside a
#   git work tree (a .git entry in it or any parent, checked on the physical
#   path of the nearest existing ancestor). umask 077 is set before any
#   file is made; devid.key is RSA 2048 and is never overwritten.
# - Works with the bash 3.2 of macOS: no associative arrays, mapfile, or
#   empty-array expansion under `set -u`.

set -euo pipefail
umask 077

DEVELOPER_ID_G2_SHA256="F16CD3C54C7F83CEA4BF1A3E6A0819C8AAA8E4A1528FD144715F350643D2DF3A"
DEVELOPER_ID_G2_URL="https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer"
DEVELOPER_ID_G2_NAME="Developer ID Certification Authority"
KEY_LABEL="Imported Private Key"
PARTITIONS="apple-tool:,apple:"
CERTIFICATE_PAGE="https://developer.apple.com/account/resources/certificates/add"
PROG="setup-macos-signing-identity"
EMAIL_PATTERN='^[^@[:space:]/]+@[^@[:space:]/]+[.][^@[:space:]/]+$'

if [ -n "${HYPRIAL_SIGNING_SETUP_TOOL_PATH:-}" ]; then
    PATH="${HYPRIAL_SIGNING_SETUP_TOOL_PATH}"
    printf '%s: test tool path in use (HYPRIAL_SIGNING_SETUP_TOOL_PATH)\n' "${PROG}" >&2
else
    PATH="/usr/bin:/bin:/usr/sbin:/sbin"
fi
export PATH

WORK=""
SECRET=""
QUOTED=""
STTY_SAVED=""

cleanup() {
    SECRET=""
    QUOTED=""
    if [ -n "${STTY_SAVED}" ]; then
        stty "${STTY_SAVED}" 2>/dev/null || true
    fi
    if [ -n "${WORK}" ] && [ -d "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

say() { printf '%s\n' "$*"; }
err() { printf '%s: %s\n' "${PROG}" "$*" >&2; }
refuse() { err "$*"; exit 2; }
fail() { err "$*"; exit 1; }

usage_main() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh <subcommand> [options]" \
        "" \
        "Installs an Apple \"Developer ID Application\" signing identity and a notarytool" \
        "credential profile on this Mac. Run the subcommands in this order:" \
        "" \
        "  csr     --dir <private dir> [--email <e-mail>] [--common-name <name>]" \
        "          make devid.key (RSA 2048) and devid.csr for developer.apple.com" \
        "  import  --dir <private dir> --cer <file> [--keychain <path>] [--intermediate <file>]" \
        "          [--allow-other-type]" \
        "          install key + certificate as one identity for /usr/bin/codesign" \
        "  notary  --profile <name> --apple-id <id> --team-id <id> [--keychain <path>]" \
        "          store notarytool credentials (notarytool asks for the password)" \
        "  check   [--keychain <path>] [--profile <name>] [--identity <sha1>]" \
        "          read-only report; signs a throw-away file to prove non-interactive use" \
        "" \
        "Guided: scripts/build/import-app-dev-id.sh walks through all of it interactively," \
        "using two helpers:" \
        "  status        --dir <private dir> [--keychain <path>] [--cer <file>]  read-only progress" \
        "  intermediate  --out <file> [--from <file>]  fetch and pin-check the G2 intermediate" \
        "" \
        "Each subcommand takes --help. The default keychain is the login keychain" \
        "(\$HOME/Library/Keychains/login.keychain-db)." \
        "" \
        "SSH: the login keychain is locked in an SSH session and must be unlocked in" \
        "every session (import unlocks it with the password it asks for; before notary or" \
        "check run: security unlock-keychain ~/Library/Keychains/login.keychain-db)." \
        "A CI runner started in the GUI session has a different keychain state; whether" \
        "it can use the identity is UNKNOWN until tried." \
        "" \
        "Security: the CI runner on this Mac runs under the same macOS account, so once" \
        "the identity is in the login keychain any workflow that reaches the runner can" \
        "sign with it (KP-100). A separate keychain (--keychain), kept off the search" \
        "list and unlocked only for signing, narrows that." \
        "" \
        "Exit status: 0 ok, 1 a step failed, 2 usage error or refused input."
}

usage_csr() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh csr --dir <private dir> [--email <e-mail>] [--common-name <name>]" \
        "" \
        "Creates <private dir> (mode 0700) if missing and writes devid.key (RSA 2048," \
        "mode 0600) and devid.csr with /usr/bin/openssl. Refuses a directory that is a" \
        "symlink, open to group or others, or inside a git work tree, and never" \
        "overwrites an existing key. Missing --email / --common-name are asked for." \
        "Keep the directory outside any repository and delete or move it after import."
}

usage_import() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh import --dir <private dir> --cer <file> [--keychain <path>]" \
        "                                       [--intermediate <file>] [--allow-other-type]" \
        "" \
        "Checks that the certificate belongs to devid.key, is a \"Developer ID Application\"" \
        "certificate (unless --allow-other-type) and has not expired; fetches Apple's" \
        "Developer ID G2 intermediate (or takes --intermediate <file>) and checks it" \
        "against the SHA-256 pinned in this script. Then asks once for the keychain" \
        "password (hidden), unlocks the keychain, imports key and certificate for" \
        "/usr/bin/codesign only, sets the key partition list so codesign needs no GUI" \
        "prompt, imports the intermediate and verifies that exactly one identity for" \
        "the certificate exists. Running it again changes nothing that is already there." \
        "Default keychain: \$HOME/Library/Keychains/login.keychain-db (locked in an SSH" \
        "session until unlocked; this subcommand unlocks it)."
}

usage_notary() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh notary --profile <name> --apple-id <id> --team-id <id> [--keychain <path>]" \
        "" \
        "Runs xcrun notarytool store-credentials. notarytool asks for an app-specific" \
        "password (created at account.apple.com, formerly appleid.apple.com, Sign-In and Security) on the terminal;" \
        "this script never takes it as an option. In an SSH session unlock the keychain" \
        "first: security unlock-keychain ~/Library/Keychains/login.keychain-db"
}

usage_status() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh status --dir <private dir> [--keychain <path>] [--cer <file>]" \
        "" \
        "Read-only progress as key=value lines, for scripts/build/import-app-dev-id.sh: which" \
        "files exist in <private dir>, whether the certificate (default" \
        "<private dir>/developerID_application.cer) parses, matches devid.key, is a Developer" \
        "ID Application certificate, has expired, its SHA-1 and the Team ID its OU suggests," \
        "and whether the keychain holds that identity and the G2 intermediate. Never unlocks."
}

usage_intermediate() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh intermediate --out <file> [--from <file>]" \
        "" \
        "Downloads Apple's Developer ID G2 intermediate (or reads --from <file>, DER or PEM)," \
        "checks it against the SHA-256 pinned in this script and writes it as DER to <file>." \
        "Exit 1 when the download fails, 2 when the file is not that certificate."
}

usage_check() {
    printf '%s\n' \
        "usage: setup-macos-signing-identity.sh check [--keychain <path>] [--profile <name>] [--identity <sha1>]" \
        "" \
        "Read-only. Reports the Developer ID Application identities (SHA-1 only), whether" \
        "the G2 intermediate is in the keychain, and whether the identity signs without" \
        "a prompt (a copy of /usr/bin/true in a temp dir, --timestamp=none, then" \
        "codesign --verify). --identity picks the identity to try (needed when there is" \
        "not exactly one Developer ID Application identity). With --profile it also asks" \
        "notarytool to use the stored credentials (contacts Apple). Does not unlock: in" \
        "an SSH session run security unlock-keychain <keychain> first."
}

wants_help() {
    local arg
    for arg in "$@"; do
        case "${arg}" in -h|--help) return 0 ;; esac
    done
    return 1
}

require_macos() {
    local system
    system="$(uname -s 2>/dev/null || true)"
    if [ "${system}" != "Darwin" ]; then
        refuse "macOS only (this host reports '${system:-unknown}')"
    fi
}

need_value() {
    [ "$1" -ge 2 ] || refuse "$2 needs a value"
}

absolute() {
    local path="$1"
    while [ "${path}" != "/" ] && [ "${path%/}" != "${path}" ]; do
        path="${path%/}"
    done
    case "${path}" in
        /*) printf '%s\n' "${path}" ;;
        *) printf '%s/%s\n' "${PWD}" "${path}" ;;
    esac
}

resolve_file() {
    local path="$1" target hops=0 directory
    while [ -L "${path}" ] && [ "${hops}" -lt 40 ]; do
        target="$(readlink "${path}")"
        case "${target}" in
            /*) path="${target}" ;;
            *) path="$(dirname "${path}")/${target}" ;;
        esac
        hops=$((hops + 1))
    done
    directory="$(cd "$(dirname "${path}")" 2>/dev/null && pwd -P)" || directory="$(dirname "${path}")"
    printf '%s/%s\n' "${directory}" "$(basename "${path}")"
}

make_work() {
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/setup-signing.XXXXXX")" || fail "cannot create a temp directory"
}

private_mode_ok() {
    local listing
    listing="$(ls -ld "$1")"
    [ "${listing:4:6}" = "------" ]
}

check_private_dir() {
    local directory="$1" create="$2" existing physical walk
    if [ -L "${directory}" ]; then
        refuse "${directory} is a symlink; give the real directory"
    fi
    existing="${directory}"
    while [ ! -e "${existing}" ] && [ "${existing}" != "/" ]; do
        existing="$(dirname "${existing}")"
    done
    physical="$(cd "${existing}" && pwd -P)" || refuse "cannot read ${existing}"
    walk="${physical}"
    while :; do
        if [ -e "${walk}/.git" ]; then
            refuse "${directory} is inside a git work tree (${walk}); keep the private key outside any repository"
        fi
        [ "${walk}" = "/" ] && break
        walk="$(dirname "${walk}")"
    done
    if [ ! -e "${directory}" ]; then
        [ "${create}" = "yes" ] || refuse "${directory} does not exist"
        [ -d "$(dirname "${directory}")" ] || refuse "the parent of ${directory} does not exist"
        mkdir -m 700 "${directory}" || fail "cannot create ${directory}"
    fi
    [ -d "${directory}" ] || refuse "${directory} is not a directory"
    [ -O "${directory}" ] || refuse "${directory} is not owned by the current user"
    private_mode_ok "${directory}" || refuse "${directory} is accessible by group or others; chmod 700 it"
}

quote_for_security() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    QUOTED="\"${value}\""
}

scrub() {
    local text="$1" escaped
    if [ -n "${SECRET}" ]; then
        escaped="${SECRET//\\/\\\\}"
        escaped="${escaped//\"/\\\"}"
        text="${text//"${escaped}"/<hidden>}"
        text="${text//"${SECRET}"/<hidden>}"
    fi
    printf '%s\n' "${text}"
}

security_stdin() {
    local line="$1" output status=0
    output="$(printf '%s\n' "${line}" | security -i 2>&1)" || status=$?
    SECURITY_OUTPUT="$(scrub "${output}")"
    return "${status}"
}

certificate_format() {
    if openssl x509 -inform DER -in "$1" -noout >/dev/null 2>&1; then
        printf 'DER\n'
    elif openssl x509 -inform PEM -in "$1" -noout >/dev/null 2>&1; then
        printf 'PEM\n'
    else
        return 1
    fi
}

to_der() {
    local source="$1" target="$2" format
    format="$(certificate_format "${source}")" || refuse "${source} is not an X.509 certificate"
    openssl x509 -inform "${format}" -in "${source}" -outform DER -out "${target}" >/dev/null 2>&1 \
        || refuse "cannot read ${source}"
}

fingerprint() {
    local value
    value="$(openssl x509 -inform DER -in "$1" -noout -fingerprint "-$2")" || fail "cannot fingerprint $1"
    value="${value#*=}"
    value="${value//:/}"
    printf '%s\n' "${value}" | tr '[:lower:]' '[:upper:]'
}

identity_section() {
    security find-identity -p codesigning "$1" | awk '/Valid identities only/{exit} {print}'
}

identity_count() {
    local count
    count="$(identity_section "$1" | grep -c "^ *[0-9][0-9]*) $2 " || true)"
    printf '%s\n' "${count:-0}"
}

identity_is_valid() {
    security find-identity -v -p codesigning "$1" | grep -q "^ *[0-9][0-9]*) $2 "
}

intermediate_present() {
    security find-certificate -a -Z -c "${DEVELOPER_ID_G2_NAME}" "$1" 2>/dev/null \
        | grep -q "SHA-256 hash: ${DEVELOPER_ID_G2_SHA256}"
}

security_import() {
    local output status=0
    output="$(security import "$@" 2>&1)" || status=$?
    if [ "${status}" -ne 0 ]; then
        case "${output}" in
            *"already exists"*) return 0 ;;
        esac
        err "${output}"
        return "${status}"
    fi
}

obtain_intermediate() {
    local source="$1" target="$2" g2_sha256
    if [ -n "${source}" ]; then
        cp "${source}" "${WORK}/g2.source"
    else
        say "downloading ${DEVELOPER_ID_G2_URL}"
        curl -fsS --proto '=https' --max-time 60 -o "${WORK}/g2.source" "${DEVELOPER_ID_G2_URL}" \
            || fail "could not download the G2 intermediate; download it elsewhere and pass --intermediate <file>"
    fi
    to_der "${WORK}/g2.source" "${target}"
    g2_sha256="$(fingerprint "${target}" sha256)"
    [ "${g2_sha256}" = "${DEVELOPER_ID_G2_SHA256}" ] \
        || refuse "the intermediate (SHA-256 ${g2_sha256}) does not match the pinned SHA-256 ${DEVELOPER_ID_G2_SHA256}"
}

default_keychain() {
    printf '%s/Library/Keychains/login.keychain-db\n' "${HOME}"
}

cmd_csr() {
    local directory="" email="" name=""
    if wants_help "$@"; then usage_csr; exit 0; fi
    require_macos
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir) need_value "$#" "$1"; directory="$2"; shift 2 ;;
            --email) need_value "$#" "$1"; email="$2"; shift 2 ;;
            --common-name) need_value "$#" "$1"; name="$2"; shift 2 ;;
            *) refuse "unknown option for csr: $1" ;;
        esac
    done
    [ -n "${directory}" ] || refuse "csr needs --dir <private dir>"
    directory="$(absolute "${directory}")"
    check_private_dir "${directory}" yes
    if [ -e "${directory}/devid.key" ] || [ -e "${directory}/devid.csr" ]; then
        refuse "${directory}/devid.key or devid.csr already exists; refusing to overwrite a private key"
    fi
    if [ -z "${email}" ]; then
        printf 'E-mail address for the CSR: ' >&2
        IFS= read -r email || true
    fi
    if [ -z "${name}" ]; then
        printf 'Common name for the CSR (your name): ' >&2
        IFS= read -r name || true
    fi
    case "${email}" in
        *\\*) refuse "the e-mail address contains a backslash" ;;
    esac
    if ! [[ "${email}" =~ ${EMAIL_PATTERN} ]]; then
        refuse "the CSR needs an e-mail address (got '${email}')"
    fi
    case "${name}" in
        ""|*/*|*\\*|*=*|*+*) refuse "the common name must be non-empty and contain no / \\ = +" ;;
    esac
    if [[ "${name}" =~ [[:cntrl:]] ]]; then
        refuse "the common name contains a control character"
    fi
    openssl genrsa -out "${directory}/devid.key" 2048 >/dev/null 2>&1 || fail "openssl genrsa failed"
    chmod 600 "${directory}/devid.key"
    openssl req -new -utf8 -key "${directory}/devid.key" -subj "/emailAddress=${email}/CN=${name}" \
        -out "${directory}/devid.csr" >/dev/null 2>&1 || fail "openssl req failed"
    say "wrote ${directory}/devid.key (RSA 2048, mode 0600) and ${directory}/devid.csr"
    say ""
    say "Next, in a browser on any computer (Account Holder of the Apple Developer team):"
    say "  1. open ${CERTIFICATE_PAGE}"
    say "  2. choose \"Developer ID Application\" (Profile Type: G2 Sub-CA) and continue"
    say "  3. upload ${directory}/devid.csr"
    say "  4. download the certificate and copy it to this Mac as"
    say "     ${directory}/developerID_application.cer"
    say "Then run:"
    say "  bash scripts/build/setup-macos-signing-identity.sh import --dir ${directory} \\"
    say "    --cer ${directory}/developerID_application.cer"
}

cmd_import() {
    local directory="" cer="" keychain="" intermediate="" allow_other="no"
    local subject key_public cert_public sha1 count matched
    if wants_help "$@"; then usage_import; exit 0; fi
    require_macos
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir) need_value "$#" "$1"; directory="$2"; shift 2 ;;
            --cer) need_value "$#" "$1"; cer="$2"; shift 2 ;;
            --keychain) need_value "$#" "$1"; keychain="$2"; shift 2 ;;
            --intermediate) need_value "$#" "$1"; intermediate="$2"; shift 2 ;;
            --allow-other-type) allow_other="yes"; shift ;;
            *) refuse "unknown option for import: $1" ;;
        esac
    done
    [ -n "${directory}" ] || refuse "import needs --dir <private dir>"
    [ -n "${cer}" ] || refuse "import needs --cer <certificate file>"
    directory="$(absolute "${directory}")"
    check_private_dir "${directory}" no
    [ -f "${directory}/devid.key" ] || refuse "${directory}/devid.key not found; run csr first"
    private_mode_ok "${directory}/devid.key" || refuse "${directory}/devid.key is accessible by group or others"
    [ -f "${cer}" ] || refuse "certificate ${cer} not found"
    keychain="$(absolute "${keychain:-$(default_keychain)}")"
    [ -f "${keychain}" ] || refuse "keychain ${keychain} not found"
    if [ -n "${intermediate}" ]; then
        [ -f "${intermediate}" ] || refuse "intermediate ${intermediate} not found"
    fi

    make_work
    to_der "${cer}" "${WORK}/certificate.der"
    openssl x509 -inform DER -in "${WORK}/certificate.der" -noout -checkend 0 >/dev/null 2>&1 \
        || refuse "the certificate has expired"
    cert_public="$(openssl x509 -inform DER -in "${WORK}/certificate.der" -noout -pubkey 2>/dev/null)" \
        || refuse "cannot read the certificate's public key"
    key_public="$(openssl pkey -in "${directory}/devid.key" -pubout 2>/dev/null)" \
        || refuse "cannot read ${directory}/devid.key"
    [ "${cert_public}" = "${key_public}" ] || refuse "the certificate does not match the private key in ${directory}"
    subject="$(openssl x509 -inform DER -in "${WORK}/certificate.der" -noout -subject)"
    case "${subject}" in
        *"CN=Developer ID Application: "*|*"CN = Developer ID Application: "*) ;;
        *)
            if [ "${allow_other}" != "yes" ]; then
                refuse "not a Developer ID Application certificate (choose that type at ${CERTIFICATE_PAGE})"
            fi
            say "note: not a Developer ID Application certificate; continuing (--allow-other-type)"
            ;;
    esac
    sha1="$(fingerprint "${WORK}/certificate.der" sha1)"

    obtain_intermediate "${intermediate}" "${WORK}/g2.der"

    if [ -t 0 ]; then
        STTY_SAVED="$(stty -g)"
        stty -echo
    fi
    printf 'Password of keychain %s (input hidden): ' "${keychain}" >&2
    IFS= read -r -s SECRET || true
    if [ -n "${STTY_SAVED}" ]; then
        stty "${STTY_SAVED}"
        STTY_SAVED=""
    fi
    printf '\n' >&2
    quote_for_security "${keychain}"
    local keychain_q="${QUOTED}"
    quote_for_security "${SECRET}"
    security_stdin "unlock-keychain -p ${QUOTED} ${keychain_q}" \
        || fail "could not unlock ${keychain} (wrong password?): ${SECURITY_OUTPUT}"

    count="$(identity_count "${keychain}" "${sha1}")"
    if [ "${count}" -ge 1 ]; then
        say "identity ${sha1} already installed in ${keychain}; not importing key or certificate again"
    else
        security_import "${directory}/devid.key" -k "${keychain}" -t priv -f openssl -T /usr/bin/codesign \
            || fail "importing the private key failed"
        security_import "${WORK}/certificate.der" -k "${keychain}" -t cert -f x509 \
            || fail "importing the certificate failed"
        say "imported the private key (usable by /usr/bin/codesign only) and the certificate"
    fi

    security_stdin "set-key-partition-list -S ${PARTITIONS} -s -t private -l \"${KEY_LABEL}\" -k ${QUOTED} ${keychain_q}" \
        || fail "set-key-partition-list failed: ${SECURITY_OUTPUT}"
    matched="$(printf '%s\n' "${SECURITY_OUTPUT}" | grep -c '^keychain:' || true)"
    say "partition list ${PARTITIONS} set on ${matched:-0} signing key(s) labelled \"${KEY_LABEL}\""
    SECRET=""
    QUOTED=""

    if intermediate_present "${keychain}"; then
        say "Developer ID G2 intermediate already present"
    else
        security_import "${WORK}/g2.der" -k "${keychain}" -t cert -f x509 || fail "importing the intermediate failed"
        say "imported the Developer ID G2 intermediate"
    fi

    count="$(identity_count "${keychain}" "${sha1}")"
    [ "${count}" -eq 1 ] || fail "expected exactly one identity ${sha1} in ${keychain}, found ${count}"
    if identity_is_valid "${keychain}" "${sha1}"; then
        say "identity ${sha1} is valid for code signing"
    elif [ "${allow_other}" = "yes" ]; then
        say "identity ${sha1} is not valid for code signing (expected for a self-signed placeholder)"
    else
        fail "identity ${sha1} is not valid for code signing: its chain does not validate"
    fi
    say ""
    say "Installed identity ${sha1} in ${keychain}."
    say "Now delete or move ${directory} (it holds the unencrypted private key);"
    say "the key lives on in the keychain. Next: the notary subcommand, then check."
}

cmd_notary() {
    local profile="" apple_id="" team_id="" keychain="" status=0
    if wants_help "$@"; then usage_notary; exit 0; fi
    require_macos
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --profile) need_value "$#" "$1"; profile="$2"; shift 2 ;;
            --apple-id) need_value "$#" "$1"; apple_id="$2"; shift 2 ;;
            --team-id) need_value "$#" "$1"; team_id="$2"; shift 2 ;;
            --keychain) need_value "$#" "$1"; keychain="$2"; shift 2 ;;
            *) refuse "unknown option for notary: $1 (notarytool asks for the app-specific password itself; it is never an option)" ;;
        esac
    done
    [[ "${profile}" =~ ^[A-Za-z0-9._-]+$ ]] || refuse "notary needs --profile <name> (letters, digits, . _ -)"
    [[ "${apple_id}" =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] || refuse "notary needs --apple-id <Apple ID e-mail>"
    [[ "${team_id}" =~ ^[A-Z0-9]{10}$ ]] || refuse "notary needs --team-id <10-character Team ID>"
    say "notarytool will ask for an app-specific password (account.apple.com, formerly appleid.apple.com, Sign-In and Security)."
    if [ -n "${keychain}" ]; then
        keychain="$(absolute "${keychain}")"
        xcrun notarytool store-credentials "${profile}" --apple-id "${apple_id}" --team-id "${team_id}" --keychain "${keychain}" || status=$?
    else
        xcrun notarytool store-credentials "${profile}" --apple-id "${apple_id}" --team-id "${team_id}" || status=$?
    fi
    if [ "${status}" -ne 0 ]; then
        fail "notarytool store-credentials failed (status ${status}); in an SSH session unlock the keychain first"
    fi
    say "stored notarytool profile ${profile}"
}

cmd_check() {
    local keychain="" explicit_keychain="no" profile="" identity="" status=0
    local section devids devid_count resolved probe sha output verdict
    if wants_help "$@"; then usage_check; exit 0; fi
    require_macos
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --keychain) need_value "$#" "$1"; keychain="$2"; explicit_keychain="yes"; shift 2 ;;
            --profile) need_value "$#" "$1"; profile="$2"; shift 2 ;;
            --identity) need_value "$#" "$1"; identity="$2"; shift 2 ;;
            *) refuse "unknown option for check: $1" ;;
        esac
    done
    if [ -n "${profile}" ] && ! [[ "${profile}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        refuse "--profile must be letters, digits, . _ -"
    fi
    if [ -n "${identity}" ] && ! [[ "${identity}" =~ ^[0-9A-Fa-f]{40}$ ]]; then
        refuse "--identity must be a 40-hex SHA-1"
    fi
    identity="$(printf '%s\n' "${identity}" | tr '[:lower:]' '[:upper:]')"
    keychain="$(absolute "${keychain:-$(default_keychain)}")"
    [ -f "${keychain}" ] || refuse "keychain ${keychain} not found"

    say "keychain: ${keychain}"
    section="$(identity_section "${keychain}")"
    devids="$(printf '%s\n' "${section}" | grep 'Developer ID Application' | sed -n 's/^ *[0-9][0-9]*) \([0-9A-F]\{40\}\) .*/\1/p' || true)"
    devid_count="$(printf '%s' "${devids}" | grep -c . || true)"
    say "Developer ID Application identities: ${devid_count:-0}"
    for sha in ${devids}; do
        if identity_is_valid "${keychain}" "${sha}"; then
            say "  ${sha} (valid)"
        else
            say "  ${sha} (not valid)"
        fi
    done

    if intermediate_present "${keychain}"; then
        say "intermediate: present (Developer ID G2, SHA-256 ${DEVELOPER_ID_G2_SHA256})"
    else
        say "intermediate: missing"
        status=1
    fi

    if [ -z "${identity}" ]; then
        if [ "${devid_count:-0}" -eq 1 ]; then
            identity="${devids}"
        fi
    fi
    if [ -z "${identity}" ]; then
        say "non-interactive signing: NOT RUN (need exactly one Developer ID Application identity, or --identity <sha1>)"
        status=1
    elif ! printf '%s\n' "${section}" | grep -q "^ *[0-9][0-9]*) ${identity} "; then
        say "non-interactive signing: FAIL (identity ${identity} not found in ${keychain})"
        status=1
    else
        make_work
        probe="${WORK}/probe"
        cp /usr/bin/true "${probe}"
        resolved="$(resolve_file "${keychain}")"
        verdict="PASS"
        if security list-keychains -d user | on_search_list_from_stdin "${resolved}"; then
            output="$(codesign --force --sign "${identity}" --keychain "${keychain}" --timestamp=none "${probe}" 2>&1)" || verdict="FAIL"
        else
            say "keychain is not on the search list: signing through an isolated HOME whose login keychain links to it"
            mkdir -p "${WORK}/home/Library/Keychains"
            ln -s "${resolved}" "${WORK}/home/Library/Keychains/login.keychain-db"
            output="$(HOME="${WORK}/home" codesign --force --sign "${identity}" --keychain "${resolved}" --timestamp=none "${probe}" 2>&1)" || verdict="FAIL"
        fi
        if [ "${verdict}" = "PASS" ]; then
            output="$(codesign --verify --strict "${probe}" 2>&1)" || verdict="FAIL"
        fi
        if [ "${verdict}" = "PASS" ]; then
            say "non-interactive signing: PASS (identity ${identity})"
        else
            say "non-interactive signing: FAIL (identity ${identity}): $(printf '%s\n' "${output}" | tail -n 1)"
            say "  is the keychain locked? unlock it in this session: security unlock-keychain ${keychain}"
            status=1
        fi
    fi

    if [ -n "${profile}" ]; then
        if [ "${explicit_keychain}" = "yes" ]; then
            output="$(xcrun notarytool history --keychain-profile "${profile}" --keychain "${keychain}" 2>&1)" && verdict="PASS" || verdict="FAIL"
        else
            output="$(xcrun notarytool history --keychain-profile "${profile}" 2>&1)" && verdict="PASS" || verdict="FAIL"
        fi
        if [ "${verdict}" = "PASS" ]; then
            say "notary profile ${profile}: PASS"
        else
            say "notary profile ${profile}: FAIL: $(printf '%s\n' "${output}" | tail -n 1)"
            status=1
        fi
    fi
    return "${status}"
}

on_search_list_from_stdin() {
    local wanted="$1" line entry found=1
    while IFS= read -r line; do
        entry="$(printf '%s\n' "${line}" | sed -e 's/^[[:space:]]*"//' -e 's/"[[:space:]]*$//')"
        [ -n "${entry}" ] || continue
        if [ "$(resolve_file "${entry}")" = "${wanted}" ]; then
            found=0
        fi
    done
    return "${found}"
}

cmd_intermediate() {
    local out="" from=""
    if wants_help "$@"; then usage_intermediate; exit 0; fi
    require_macos
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --out) need_value "$#" "$1"; out="$2"; shift 2 ;;
            --from) need_value "$#" "$1"; from="$2"; shift 2 ;;
            *) refuse "unknown option for intermediate: $1" ;;
        esac
    done
    [ -n "${out}" ] || refuse "intermediate needs --out <file>"
    out="$(absolute "${out}")"
    [ -d "$(dirname "${out}")" ] || refuse "the directory of ${out} does not exist"
    if [ -n "${from}" ]; then
        [ -f "${from}" ] || refuse "intermediate ${from} not found"
    fi
    make_work
    obtain_intermediate "${from}" "${WORK}/g2.der"
    cp "${WORK}/g2.der" "${out}"
    say "Developer ID G2 intermediate matches the pinned SHA-256; wrote ${out}"
}

status_line() {
    printf '%s=%s\n' "$1" "$2"
}

team_from_subject() {
    printf '%s\n' "$1" | tr '/' '\n' | tr ',' '\n' | sed -n 's/^ *OU *= *\([A-Z0-9]\{10\}\) *$/\1/p' | head -n 1
}

cmd_status() {
    local directory="" keychain="" cer="" format subject sha1="" cert_public key_public
    if wants_help "$@"; then usage_status; exit 0; fi
    require_macos
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir) need_value "$#" "$1"; directory="$2"; shift 2 ;;
            --keychain) need_value "$#" "$1"; keychain="$2"; shift 2 ;;
            --cer) need_value "$#" "$1"; cer="$2"; shift 2 ;;
            *) refuse "unknown option for status: $1" ;;
        esac
    done
    [ -n "${directory}" ] || refuse "status needs --dir <private dir>"
    directory="$(absolute "${directory}")"
    cer="${cer:-${directory}/developerID_application.cer}"
    keychain="$(absolute "${keychain:-$(default_keychain)}")"
    status_line dir "${directory}"
    if [ -d "${directory}" ]; then status_line dir_state present; else status_line dir_state absent; fi
    if [ -f "${directory}/devid.key" ]; then status_line key present; else status_line key absent; fi
    if [ -f "${directory}/devid.csr" ]; then status_line csr present; else status_line csr absent; fi
    status_line certificate "${cer}"
    if [ ! -f "${cer}" ]; then
        status_line certificate_state absent
    elif ! format="$(certificate_format "${cer}")"; then
        status_line certificate_state unreadable
    else
        make_work
        openssl x509 -inform "${format}" -in "${cer}" -outform DER -out "${WORK}/status.der" >/dev/null 2>&1
        status_line certificate_state ok
        sha1="$(fingerprint "${WORK}/status.der" sha1)"
        status_line certificate_sha1 "${sha1}"
        subject="$(openssl x509 -inform DER -in "${WORK}/status.der" -noout -subject)"
        case "${subject}" in
            *"CN=Developer ID Application: "*|*"CN = Developer ID Application: "*) status_line certificate_type developer-id ;;
            *) status_line certificate_type other ;;
        esac
        if openssl x509 -inform DER -in "${WORK}/status.der" -noout -checkend 0 >/dev/null 2>&1; then
            status_line certificate_expired no
        else
            status_line certificate_expired yes
        fi
        cert_public="$(openssl x509 -inform DER -in "${WORK}/status.der" -noout -pubkey 2>/dev/null || true)"
        key_public=""
        if [ -f "${directory}/devid.key" ]; then
            key_public="$(openssl pkey -in "${directory}/devid.key" -pubout 2>/dev/null || true)"
        fi
        if [ -n "${cert_public}" ] && [ "${cert_public}" = "${key_public}" ]; then
            status_line certificate_matches_key yes
        else
            status_line certificate_matches_key no
        fi
        status_line team_id_suggestion "$(team_from_subject "${subject}")"
    fi
    status_line keychain "${keychain}"
    if [ ! -f "${keychain}" ]; then
        status_line keychain_state absent
        return 0
    fi
    status_line keychain_state present
    if [ -n "${sha1}" ]; then
        if [ "$(identity_count "${keychain}" "${sha1}")" -ge 1 ]; then
            status_line identity installed
        else
            status_line identity absent
        fi
    fi
    status_line developer_id_identities "$(identity_section "${keychain}" | grep -c 'Developer ID Application' || true)"
    if intermediate_present "${keychain}"; then status_line intermediate present; else status_line intermediate absent; fi
}

main() {
    if [ "$#" -eq 0 ]; then
        usage_main >&2
        exit 2
    fi
    local subcommand="$1"
    shift
    case "${subcommand}" in
        -h|--help) usage_main ;;
        csr) cmd_csr "$@" ;;
        import) cmd_import "$@" ;;
        notary) cmd_notary "$@" ;;
        check) cmd_check "$@" ;;
        status) cmd_status "$@" ;;
        intermediate) cmd_intermediate "$@" ;;
        *) refuse "unknown subcommand: ${subcommand} (see --help)" ;;
    esac
}

main "$@"
