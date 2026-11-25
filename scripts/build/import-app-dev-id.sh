#!/usr/bin/env bash
# Guided installation of the Apple "Developer ID Application" signing identity
# on the packaging Mac: run ./scripts/build/import-app-dev-id.sh with no
# arguments and answer the questions.
#
# Decision record (file header; no other comments by repository rule):
# - Owner request 2026-11-18: one command that walks through the whole
#   Developer ID procedure. This file is only the guide. Every step that
#   touches a key, a certificate, the keychain or Apple is a subcommand of
#   setup-macos-signing-identity.sh next to it (csr, intermediate, import,
#   notary, check) and progress is read with its read-only `status`
#   subcommand; the decisions and measurements behind those steps are in that
#   file's header. Signing and notarizing the app stay deferred (KP-100).
# - Location: scripts/build/, next to the script it drives. The structural
#   gate does not scan the repository root, but AGENTS.md puts scripts under
#   scripts/ and the root holds only the published installers.
# - Language: English, like every operator script in this repository.
# - Resumable without a state file: each run starts from what exists. A key
#   in the private directory skips key and CSR generation; a certificate at
#   <dir>/developerID_application.cer that matches the key skips the
#   certificate prompt; an identity for that certificate already in the
#   keychain skips the import; a pinned intermediate already at
#   <dir>/DeveloperIDG2CA.cer skips the download. Running it again after an
#   interruption continues at the first missing step.
# - Input: every answer is one line read from stdin (the terminal when run by
#   hand; tests feed it). End of input exits 1 with "run it again to
#   continue"; `q` at any prompt does the same; Ctrl-C exits 130 with the same
#   line. Nothing is reported as done unless every step it ran succeeded.
#   The guide handles no secret: `import` asks for the keychain password and
#   notarytool asks for the app-specific password themselves.
# - The certificate can be given as a path or pasted as base64 or PEM, ended
#   by an empty line. The guide asks for wrapped base64 (`base64 -b 64 -i
#   developerID_application.cer` on a Mac, `openssl base64 -in ...`
#   elsewhere): measured 2026-11-18 on a macOS pty, a line longer than about
#   1024 characters cannot be entered at all (the read never returns), and
#   the one-line output of plain `base64 -i` for a certificate is longer.
#   The pasted base64 is wrapped into PEM (64-character lines),
#   checked with `status` and only then saved as
#   <dir>/developerID_application.cer. The intermediate takes a path or a
#   paste the same way when the download fails (measured 2026-11-18: a direct
#   request to Apple was answered 403, through the configured proxy it
#   worked).
# - Team ID: suggested from the certificate's OU (on a certificate built with
#   Apple's subject layout, UID=<team>/CN=Developer ID Application: <name>
#   (<team>)/OU=<team>/O=<name>/C=US, LibreSSL prints the OU as the Team ID;
#   a real certificate's layout is UNKNOWN until the owner runs this) and
#   always confirmed by the owner.
# - A failing check is usually a keychain locked in this SSH session (a
#   resumed run skips the import, which is what unlocks it; measured: a
#   locked keychain makes codesign fail with errSecInternalComponent). The
#   guide then offers to run the idempotent import again, which unlocks the
#   keychain and changes nothing already installed, and checks again.
# - Deleting the private directory is offered only after a passing check
#   (a repeated import needs devid.key) and needs `yes` typed in full; only the files
#   the procedure puts there (devid.key, devid.csr,
#   developerID_application.cer, DeveloperIDG2CA.cer) are removed, then the
#   directory with rmdir when it is empty. Nothing is removed recursively.
# - Options: --keychain <path> chooses another keychain (default: the login
#   keychain); --allow-other-type is for tests with a self-signed placeholder
#   and is announced on stderr. Tools come from /usr/bin:/bin:/usr/sbin:/sbin,
#   or from the test-only HYPRIAL_SIGNING_SETUP_TOOL_PATH (announced).
# - Exit status: 0 every step done; 1 a step failed, the owner quit, or the
#   input ended; 2 a usage error or not macOS; 130 interrupted.
# - Works with the bash 3.2 of macOS.

set -euo pipefail
umask 077

PROG="import-app-dev-id"
CERTIFICATE_PAGE="https://developer.apple.com/account/resources/certificates/add"
APP_PASSWORD_PAGE="https://account.apple.com (formerly appleid.apple.com): Sign-In and Security, App-Specific Passwords"
DEFAULT_PROFILE="devid-notary"
CER_NAME="developerID_application.cer"
G2_NAME="DeveloperIDG2CA.cer"
G2_URL="https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer"
EMAIL_PATTERN='^[^@[:space:]/]+@[^@[:space:]/]+[.][^@[:space:]/]+$'

if [ -n "${HYPRIAL_SIGNING_SETUP_TOOL_PATH:-}" ]; then
    PATH="${HYPRIAL_SIGNING_SETUP_TOOL_PATH}"
    printf '%s: test tool path in use (HYPRIAL_SIGNING_SETUP_TOOL_PATH)\n' "${PROG}" >&2
else
    PATH="/usr/bin:/bin:/usr/sbin:/sbin"
fi
export PATH

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SETUP="${HERE}/setup-macos-signing-identity.sh"
WORK=""
KEYCHAIN=""
ALLOW_OTHER="no"
ANSWER=""
STATUS_TEXT=""
DIR=""
CER=""
SHA1=""
PROFILE=""
SKIPPED=""

cleanup() {
    if [ -n "${WORK}" ] && [ -d "${WORK}" ]; then
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT
trap 'printf "\n%s: interrupted; nothing more was done. Run it again to continue.\n" "${PROG}" >&2; exit 130' INT
trap 'exit 143' TERM HUP

say() { printf '%s\n' "$*"; }
err() { printf '%s: %s\n' "${PROG}" "$*" >&2; }
stop() {
    err "$1 Run ./scripts/build/import-app-dev-id.sh again to continue."
    exit 1
}
step() { printf '\n== %s\n' "$*"; }

usage() {
    printf '%s\n' \
        "usage: ./scripts/build/import-app-dev-id.sh [--keychain <path>]" \
        "" \
        "Guides you through installing an Apple \"Developer ID Application\" signing" \
        "identity on this Mac, with questions at each step:" \
        "  1. checks this Mac and shows which keychain is used" \
        "  2. picks a private directory for the key (default ~/devid-private)" \
        "  3. makes a private key and a certificate signing request (CSR)" \
        "  4. tells you how to get the certificate from developer.apple.com and takes it" \
        "     back as a file or as pasted base64 (handy over SSH)" \
        "  5. installs key and certificate for codesign (asks for the keychain password)" \
        "  6. optionally stores notarytool credentials (notarytool asks for the password)" \
        "  7. checks that signing works, then offers to delete the private directory" \
        "" \
        "Run it again after an interruption: it continues where it stopped. Type q at" \
        "any prompt to stop. The steps themselves are subcommands of" \
        "scripts/build/setup-macos-signing-identity.sh (see its --help)." \
        "" \
        "SSH: the login keychain is locked in an SSH session; the import step unlocks it." \
        "A CI runner started in the GUI session has a different keychain state;" \
        "whether it can use the identity is UNKNOWN until tried." \
        "" \
        "Options:" \
        "  --keychain <path>    use this keychain instead of the login keychain" \
        "  --allow-other-type   for tests only: accept a certificate that is not a" \
        "                       Developer ID Application certificate" \
        "  -h, --help           show this help" \
        "" \
        "Exit status: 0 done, 1 a step failed or you stopped, 2 usage error, 130 interrupted."
}

ask() {
    local prompt="$1" default="${2:-}"
    if [ -n "${default}" ]; then
        printf '%s [%s]: ' "${prompt}" "${default}"
    else
        printf '%s: ' "${prompt}"
    fi
    if ! IFS= read -r ANSWER; then
        printf '\n'
        stop "No more input."
    fi
    ANSWER="$(printf '%s' "${ANSWER}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "${ANSWER}" in
        q|Q|quit) stop "Stopped." ;;
    esac
    if [ -z "${ANSWER}" ]; then
        ANSWER="${default}"
    fi
}

ask_yes_no() {
    local prompt="$1" default="$2"
    while :; do
        ask "${prompt} (y/n)" "${default}"
        case "${ANSWER}" in
            y|Y|yes) return 0 ;;
            n|N|no) return 1 ;;
        esac
        say "Please answer y or n (or q to stop)."
    done
}

setup() {
    bash "${SETUP}" "$@"
}

read_status() {
    local extra=()
    if [ -n "${KEYCHAIN}" ]; then extra=(--keychain "${KEYCHAIN}"); fi
    STATUS_TEXT="$(setup status --dir "${DIR}" "${extra[@]+"${extra[@]}"}" "$@")" \
        || stop "Reading the progress failed (see the message above)."
}

field() {
    printf '%s\n' "${STATUS_TEXT}" | sed -n "s/^$1=//p" | head -n 1
}

expand_path() {
    case "$1" in
        \~) printf '%s\n' "${HOME}" ;;
        \~/*) printf '%s/%s\n' "${HOME}" "${1#\~/}" ;;
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "${PWD}" "$1" ;;
    esac
}

make_work() {
    if [ -z "${WORK}" ]; then
        WORK="$(mktemp -d "${TMPDIR:-/tmp}/import-dev-id.XXXXXX")" || stop "Cannot create a temp directory."
    fi
}

read_paste() {
    local target="$1" line body="" in_pem="no" saw_pem="no"
    say "Paste the base64 (or PEM) text now and finish with an empty line:"
    while :; do
        if ! IFS= read -r line; then
            stop "No more input."
        fi
        line="$(printf '%s' "${line}" | tr -d '\r')"
        case "${line}" in
            "") [ -n "${body}" ] && break; continue ;;
            q|Q) stop "Stopped." ;;
            *"-----BEGIN CERTIFICATE-----"*) in_pem="yes"; saw_pem="yes"; continue ;;
            *"-----END CERTIFICATE-----"*) in_pem="no"; continue ;;
        esac
        if [ "${saw_pem}" = "yes" ] && [ "${in_pem}" = "no" ]; then
            continue
        fi
        body="${body}$(printf '%s' "${line}" | tr -d ' \t')"
    done
    if ! [[ "${body}" =~ ^[A-Za-z0-9+/]+=*$ ]]; then
        say "That is not base64 text (only A-Z a-z 0-9 + / = are allowed)."
        return 1
    fi
    {
        printf '%s\n' "-----BEGIN CERTIFICATE-----"
        printf '%s\n' "${body}" | fold -w 64
        printf '%s\n' "-----END CERTIFICATE-----"
    } > "${target}"
}

preflight() {
    local tool missing=""
    step "Step 1 of 7: check this Mac"
    if [ "$(uname -s 2>/dev/null || true)" != "Darwin" ]; then
        err "macOS only"
        exit 2
    fi
    [ -f "${SETUP}" ] || { err "missing ${SETUP}"; exit 1; }
    for tool in openssl security codesign xcrun curl; do
        command -v "${tool}" >/dev/null 2>&1 || missing="${missing} ${tool}"
    done
    if [ -n "${missing}" ]; then
        err "missing tools:${missing} (install the Xcode Command Line Tools: xcode-select --install)"
        exit 1
    fi
    say "Tools found: openssl, security, codesign, xcrun, curl."
    if [ ! -f "${KEYCHAIN:-${HOME}/Library/Keychains/login.keychain-db}" ]; then
        err "keychain ${KEYCHAIN:-${HOME}/Library/Keychains/login.keychain-db} not found"
        exit 1
    fi
    say "Keychain: ${KEYCHAIN:-${HOME}/Library/Keychains/login.keychain-db (your login keychain)}"
    say "Note (KP-100): the CI runner on this Mac runs as this same macOS user, so any"
    say "workflow that reaches the runner can sign with this identity while the keychain is unlocked."
    ask_yes_no "Continue" "y" || stop "Stopped."
}

choose_directory() {
    local default="${HOME}/devid-private"
    step "Step 2 of 7: private directory"
    say "The private key is made in a directory only you can read, outside any git checkout."
    ask "Private directory" "${default}"
    DIR="$(expand_path "${ANSWER}")"
    while [ "${DIR}" != "/" ] && [ "${DIR%/}" != "${DIR}" ]; do DIR="${DIR%/}"; done
    CER="${DIR}/${CER_NAME}"
    read_status
}

make_key() {
    local email name count
    step "Step 3 of 7: private key and certificate signing request (CSR)"
    if [ "$(field key)" = "present" ]; then
        say "${DIR}/devid.key already exists: skipping key and CSR generation."
        [ "$(field csr)" = "present" ] || stop "${DIR}/devid.csr is missing next to the key; restore it or start over in a new directory."
        return 0
    fi
    count="$(field developer_id_identities)"
    if [ "${count:-0}" -ge 1 ] 2>/dev/null; then
        say "The keychain already holds ${count} Developer ID Application identity(ies)."
        say "A new key needs a new certificate from Apple, and Apple limits how many you can have."
        ask_yes_no "Make a new key anyway" "n" || stop "Stopped."
    fi
    say "Apple ignores most of the CSR, but it needs an e-mail address and a name."
    while :; do
        ask "Your e-mail address (for the CSR)"
        email="${ANSWER}"
        if [[ "${email}" =~ ${EMAIL_PATTERN} ]]; then break; fi
        say "That does not look like an e-mail address."
    done
    while :; do
        ask "Your name (common name for the CSR)"
        name="${ANSWER}"
        case "${name}" in
            ""|*/*|*\\*|*=*|*+*) say "The name must not be empty and must not contain / \\ = +." ;;
            *) break ;;
        esac
    done
    setup csr --dir "${DIR}" --email "${email}" --common-name "${name}" >/dev/null \
        || stop "Making the key and CSR failed (see the message above)."
    say "Made ${DIR}/devid.key (private, mode 0600) and ${DIR}/devid.csr."
    read_status
}

certificate_problem() {
    local state
    state="$(field certificate_state)"
    case "${state}" in
        absent) printf 'the file does not exist\n'; return 0 ;;
        unreadable) printf 'it is not an X.509 certificate\n'; return 0 ;;
    esac
    if [ "$(field certificate_matches_key)" != "yes" ]; then
        printf 'it was not made from %s/devid.csr (its key does not match devid.key)\n' "${DIR}"; return 0
    fi
    if [ "$(field certificate_expired)" = "yes" ]; then
        printf 'it has expired\n'; return 0
    fi
    if [ "$(field certificate_type)" != "developer-id" ] && [ "${ALLOW_OTHER}" != "yes" ]; then
        printf 'it is not a "Developer ID Application" certificate\n'; return 0
    fi
    return 1
}

web_steps() {
    say "Get the certificate from Apple, in a browser on any computer:"
    say "  1. sign in as the Account Holder and open ${CERTIFICATE_PAGE}"
    say "  2. choose \"Developer ID Application\", then \"G2 Sub-CA (Xcode 11.4.1 or later)\""
    say "  3. upload the CSR (devid.csr) and download the certificate (developerID_application.cer)"
    if ask_yes_no "Print the CSR here so you can copy it into a file on that computer" "y"; then
        say "---- copy everything between these lines into a file named devid.csr ----"
        cat "${DIR}/devid.csr"
        say "---- end of devid.csr ----"
    fi
    say "Or copy it with scp from that computer:  scp <user>@<this-mac>:${DIR}/devid.csr ."
}

take_certificate() {
    local candidate problem
    step "Step 4 of 7: the certificate from Apple"
    if [ "$(field certificate_state)" = "ok" ] && ! problem="$(certificate_problem)"; then
        say "Found ${CER}: it matches the key; using it."
        return 0
    fi
    if [ "$(field certificate_state)" != "absent" ]; then
        say "${CER} cannot be used: $(certificate_problem)."
    fi
    web_steps
    say "Then put the certificate on this Mac as ${CER} (for example with scp), give its"
    say "path, or type paste and paste it as wrapped base64 or PEM. On the other computer:"
    say "  Mac:      base64 -b 64 -i developerID_application.cer"
    say "  Linux:    openssl base64 -in developerID_application.cer"
    say "  Windows:  certutil -encode developerID_application.cer devid.txt (then paste devid.txt)"
    say "Keep the lines short: a terminal does not accept a line much over 1000 characters."
    while :; do
        ask "Certificate file, or paste" "${CER}"
        if [ "${ANSWER}" = "paste" ]; then
            make_work
            candidate="${WORK}/pasted.pem"
            read_paste "${candidate}" || continue
        else
            candidate="$(expand_path "${ANSWER}")"
            if [ ! -f "${candidate}" ]; then
                say "${candidate} does not exist yet."
                continue
            fi
        fi
        read_status --cer "${candidate}"
        if problem="$(certificate_problem)"; then
            say "That certificate cannot be used: ${problem}."
            continue
        fi
        if [ "${candidate}" != "${CER}" ]; then
            cp "${candidate}" "${CER}" || stop "Cannot save the certificate as ${CER}."
            say "Saved the certificate as ${CER}."
        fi
        read_status
        return 0
    done
}

take_intermediate() {
    local target="${DIR}/${G2_NAME}" rc candidate
    make_work
    if [ -f "${target}" ]; then
        if setup intermediate --from "${target}" --out "${WORK}/g2-check.der" >/dev/null 2>&1; then
            say "Using the Apple intermediate already saved in ${target}."
            return 0
        fi
        say "${target} is not Apple's Developer ID G2 intermediate; fetching it again."
    fi
    say "Fetching Apple's Developer ID G2 intermediate certificate (checked against a pinned SHA-256)."
    rc=0
    setup intermediate --out "${target}" >/dev/null 2>&1 || rc=$?
    while [ "${rc}" -ne 0 ]; do
        if [ "${rc}" -eq 1 ]; then
            say "The download failed. Apple's server may refuse this network (a direct request"
            say "was answered 403 here once; it worked through a proxy)."
        else
            say "That file is not Apple's Developer ID G2 intermediate (the SHA-256 does not match)."
        fi
        say "Download ${G2_URL} on any computer, then give the file's path, or type paste and"
        say "paste it as wrapped base64 (base64 -b 64 -i DeveloperIDG2CA.cer) or PEM; or type retry."
        ask "Intermediate file, paste, or retry" "retry"
        case "${ANSWER}" in
            retry) rc=0; setup intermediate --out "${target}" >/dev/null 2>&1 || rc=$? ;;
            paste)
                candidate="${WORK}/g2-pasted.pem"
                if read_paste "${candidate}"; then
                    rc=0; setup intermediate --from "${candidate}" --out "${target}" >/dev/null 2>&1 || rc=$?
                else
                    rc=2
                fi
                ;;
            *)
                candidate="$(expand_path "${ANSWER}")"
                if [ -f "${candidate}" ]; then
                    rc=0; setup intermediate --from "${candidate}" --out "${target}" >/dev/null 2>&1 || rc=$?
                else
                    say "${candidate} does not exist."
                    rc=1
                fi
                ;;
        esac
    done
    say "Saved the intermediate as ${target}."
}

install_identity() {
    step "Step 5 of 7: install key and certificate in the keychain"
    if [ "$(field identity)" = "installed" ]; then
        say "Identity $(field certificate_sha1) is already in the keychain: skipping the import."
        SHA1="$(field certificate_sha1)"
        return 0
    fi
    run_import
}

run_import() {
    local args=() rc
    take_intermediate
    args=(--dir "${DIR}" --cer "${CER}" --intermediate "${DIR}/${G2_NAME}")
    if [ -n "${KEYCHAIN}" ]; then args+=(--keychain "${KEYCHAIN}"); fi
    if [ "${ALLOW_OTHER}" = "yes" ]; then args+=(--allow-other-type); fi
    say "The import asks for the keychain password (for the login keychain: your macOS"
    say "login password). It goes straight to the import; this guide never sees it."
    while :; do
        rc=0
        setup import "${args[@]}" || rc=$?
        if [ "${rc}" -eq 0 ]; then
            read_status
            [ "$(field identity)" = "installed" ] || stop "The import reported success but the identity is not in the keychain."
            SHA1="$(field certificate_sha1)"
            say "(The import's own hints above are for the manual subcommands; this guide goes on.)"
            return 0
        fi
        say "The import failed (exit ${rc}); the message above says why. Nothing was reported as installed."
        while :; do
            ask "Type r to retry or q to stop" "r"
            case "${ANSWER}" in
                r|R|retry) break ;;
            esac
        done
    done
}

notary_credentials() {
    local team apple_id rc args=()
    step "Step 6 of 7: notarization credentials (optional)"
    say "notarytool needs your Apple ID, your Team ID and an app-specific password, stored"
    say "once in the keychain under a profile name. You can skip this and do it later."
    if ! ask_yes_no "Set up notarization credentials now" "n"; then
        SKIPPED="${SKIPPED} notarization-credentials"
        return 0
    fi
    team="$(field team_id_suggestion)"
    if [ -n "${team}" ]; then
        say "The certificate's OU suggests the Team ID ${team}; check it against Membership"
        say "details at developer.apple.com before you accept it."
    fi
    while :; do
        ask "Team ID (10 letters/digits)" "${team}"
        if [[ "${ANSWER}" =~ ^[A-Z0-9]{10}$ ]]; then team="${ANSWER}"; break; fi
        say "A Team ID is 10 capital letters or digits."
    done
    while :; do
        ask "Apple ID (e-mail you sign in to Apple with)"
        if [[ "${ANSWER}" =~ ${EMAIL_PATTERN} ]]; then apple_id="${ANSWER}"; break; fi
        say "That does not look like an e-mail address."
    done
    while :; do
        ask "Profile name" "${DEFAULT_PROFILE}"
        if [[ "${ANSWER}" =~ ^[A-Za-z0-9._-]+$ ]]; then PROFILE="${ANSWER}"; break; fi
        say "Use letters, digits, dot, underscore or dash."
    done
    say "notarytool now asks for an app-specific password. It is not your Apple ID password:"
    say "create one at ${APP_PASSWORD_PAGE}."
    args=(--profile "${PROFILE}" --apple-id "${apple_id}" --team-id "${team}")
    if [ -n "${KEYCHAIN}" ]; then args+=(--keychain "${KEYCHAIN}"); fi
    while :; do
        rc=0
        setup notary "${args[@]}" || rc=$?
        [ "${rc}" -eq 0 ] && return 0
        say "Storing the credentials failed (exit ${rc}); see the message above."
        ask "Type r to retry, s to skip, or q to stop" "r"
        case "${ANSWER}" in
            s|S|skip) PROFILE=""; SKIPPED="${SKIPPED} notarization-credentials"; return 0 ;;
        esac
    done
}

final_check() {
    local args=(--identity "${SHA1}") rc=0
    step "Step 7 of 7: check"
    if [ -n "${KEYCHAIN}" ]; then args+=(--keychain "${KEYCHAIN}"); fi
    if [ -n "${PROFILE}" ]; then
        args+=(--profile "${PROFILE}")
        say "Checking signing, and the notary profile (this contacts Apple)."
    else
        say "Checking that codesign can use the identity without a prompt (nothing leaves this Mac)."
    fi
    setup check "${args[@]}" || rc=$?
    return "${rc}"
}

offer_delete() {
    local name removed="" left entry
    say ""
    say "${DIR} still holds the unencrypted private key (devid.key). The key now lives in"
    say "the keychain, so the directory is no longer needed."
    ask "Type yes to delete the files this procedure put there now" "no"
    if [ "${ANSWER}" != "yes" ]; then
        say "Kept ${DIR}. Delete or move it yourself when you are done."
        return 0
    fi
    for name in devid.key devid.csr "${CER_NAME}" "${G2_NAME}"; do
        if [ -f "${DIR}/${name}" ] && [ ! -L "${DIR}/${name}" ]; then
            rm -f "${DIR}/${name}"
            removed="${removed} ${name}"
        fi
    done
    say "Deleted:${removed:- nothing}."
    if rmdir "${DIR}" 2>/dev/null; then
        say "Removed the empty directory ${DIR}."
    else
        left=""
        for entry in "${DIR}"/* "${DIR}"/.[!.]*; do
            if [ -e "${entry}" ]; then left="${left}$(basename "${entry}") "; fi
        done
        say "Kept ${DIR}: it still contains ${left:-other files}(not created by this procedure)."
    fi
}

main() {
    local check_rc=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help) usage; exit 0 ;;
            --keychain)
                [ "$#" -ge 2 ] || { err "--keychain needs a path"; exit 2; }
                KEYCHAIN="$(expand_path "$2")"; shift 2 ;;
            --allow-other-type)
                ALLOW_OTHER="yes"
                err "test-only option --allow-other-type in use"
                shift ;;
            *) err "unknown option: $1 (see --help)"; exit 2 ;;
        esac
    done
    if [ ! -t 0 ]; then
        err "answers are read from stdin, which is not a terminal"
    fi
    preflight
    choose_directory
    make_key
    take_certificate
    install_identity
    notary_credentials
    final_check || check_rc=$?
    while [ "${check_rc}" -ne 0 ]; do
        say ""
        say "The check failed. In a new SSH session that is usually the locked keychain. Running the"
        say "import again unlocks it (it asks for the keychain password and changes nothing already"
        say "installed)."
        ask "Type r to run the import again and check again, or s to finish without a passing check" "r"
        case "${ANSWER}" in
            s|S|skip) break ;;
            r|R|retry)
                run_import
                check_rc=0
                final_check || check_rc=$?
                ;;
        esac
    done
    step "Summary"
    say "identity:  ${SHA1}"
    say "keychain:  ${KEYCHAIN:-${HOME}/Library/Keychains/login.keychain-db}"
    say "profile:   ${PROFILE:-none}"
    say "skipped:  ${SKIPPED:- nothing}"
    if [ "${check_rc}" -eq 0 ]; then
        say "check:     PASS"
    else
        say "check:     FAIL (see above)"
    fi
    say "The login keychain is locked again in every new SSH session; unlock it with"
    say "security unlock-keychain ~/Library/Keychains/login.keychain-db before signing there."
    if [ "${check_rc}" -eq 0 ]; then
        offer_delete
    else
        say "Kept ${DIR}: the check did not pass, and running the import again needs devid.key."
    fi
    if [ "${check_rc}" -ne 0 ]; then
        err "the check failed; the identity is installed but not proven usable."
        exit 1
    fi
    say "Done."
}

main "$@"
