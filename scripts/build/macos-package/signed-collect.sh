# shellcheck shell=bash
# Collection of a --developer-id run of scripts/build/package-macos-shell.sh,
# sourced by it (never run on its own; it uses that script's variables and
# status functions).
#
# Decision record (file header; no other comments by repository rule):
# - Moved out of package-macos-shell.sh unchanged (PR #2980 review 38872:
#   that file had reached the 1000-line limit); the decisions on signed
#   collection are in its header and in packaging/clients/README.md
#   "Developer ID signing".
# - Two changes from the same review: the signed record for BUILD-INFO.txt is
#   assembled before anything is appended, and when it cannot be (the signed
#   output has no BUILD-INFO.txt) the dmg, manifest and SHA256SUMS already
#   copied into --out are removed and the run fails like any other collect
#   failure (signed work removed, NOT-SIGNED.txt naming collect), so
#   NOT-SIGNED.txt's "no dmg was collected" stays true. An append to
#   BUILD-INFO.txt that fails removes the copies too and fails collect. A passing collect also
#   removes the UNFINISHED.txt of the final copy directory.

first_unsuccessful_stage() {
    local id outcome
    for id in ${ALL_STAGES}; do
        outcome="$(status_of "${id}")"
        if [ "${id}" != collect ] && [ "${outcome}" != success ]; then
            printf '%s (%s)\n' "${id}" "${outcome}"
            return 0
        fi
    done
    printf 'none\n'
}

source_unchanged() {
    [ "$(git -C "${ROOT}" rev-parse HEAD)" = "${SOURCE_SHA}" ] && [ -z "$(git -C "${ROOT}" status --porcelain)" ] && return 0
    echo "HEAD is no longer ${SOURCE_SHA} or the tree is no longer clean; nothing signed is collected"
    git -C "${ROOT}" status --short
    return 1
}

notary_ids() {
    if [ -f "${OUT}/sign-bundle.json" ]; then
        sed -n 's/^ *"id": "\([0-9a-f-][0-9a-f-]*\)",*$/\1/p' "${OUT}/sign-bundle.json"
    fi
}

verified_sha() {
    sed -n "/\"$1\": {/,/}/s/^ *\"$2\": \"\([0-9a-f]\{64\}\)\",*\$/\\1/p" "${OUT}/verify-signed.json"
}

matches_verified() {
    local sum
    sum="$(verified_sha "$1" "$2")"
    if [ -n "${sum}" ] && [ "${sum}" = "$(shasum -a 256 "$3" | sed 's/ .*//')" ]; then
        return 0
    fi
    echo "${3##*/} is not the file verify_signed checked ($1 $2: ${sum:-not recorded})"
    return 1
}

collect_verified() {
    local name="${1##*/}"
    cp "$1" "${OUT}/" || return 1
    cp "${SIGNED}/runtime-manifest.json" "${OUT}/" || return 1
    (cd "${OUT}" && shasum -a 256 "${name}" runtime-manifest.json) > "${OUT}/SHA256SUMS" || return 1
    matches_verified verifiedSha256 "${name}" "${OUT}/${name}" || return 1
    matches_verified finalCopy dmgSha256 "${OUT}/${name}" || return 1
    matches_verified verifiedSha256 runtime-manifest.json "${OUT}/runtime-manifest.json" || return 1
    matches_verified verifiedSha256 SHA256SUMS "${OUT}/SHA256SUMS" || return 1
}

write_not_signed() {
    {
        echo "the Developer ID signed run FAILED; the first stage that did not succeed: $1"
        echo "nothing in this directory is signed, notarized or a release, and no dmg was collected"
        echo "(the ad-hoc dmg of build_app is never collected by a signed run)"
        echo "notary submissions spent by this run (do not resubmit; their status is in sign-bundle.json):"
        notary_ids | sed 's/^/  /'
    } > "${OUT}/NOT-SIGNED.txt"
}

distribution_line() {
    local wheel
    wheel="$(find "${WHEEL_DIR}" -maxdepth 1 -name "${WHEEL_GLOB}" -print -quit)"
    if [ -n "${wheel}" ] && uv run --no-project --python "${PYTHON_VERSION}" python scripts/ci/wheel_source_remote.py "${wheel}" >&2; then
        echo "distributable: internal verification only (Developer ID signed, notarized and stapled, clean tree at the requested source sha, the wheel records the public source remote). External distribution needs the recorded commit to exist on the public remote, which this build does not check; until the repository has moved to GitHub, signed packages are not distributed externally (owner decision 2026-11-19, KP-100)"
    else
        echo "distributable: no (the wheel does not record the public source remote: for internal testers only, not for distribution)"
    fi
}

signed_record() {
    echo "signing: Developer ID (--developer-id), app and dmg notarized and stapled; checked on the final artifact and both smokes ran on the app copied out of the final dmg with a quarantine attribute"
    distribution_line
    echo "signed output record:"
    sed 's/^/  /' "${SIGNED}/BUILD-INFO.txt"
}

remove_collected() {
    rm -f "${OUT}/${1##*/}" "${OUT}/runtime-manifest.json" "${OUT}/SHA256SUMS"
}

collect_signed() {
    local dmg="" blocker="" record=""
    if [ "$(status_of build_app)" != success ]; then
        write_not_built
    fi
    write_build_info
    rm -f "${OUT}/SHA256SUMS"
    if [ "${FAILED}" -eq 0 ] && ! source_unchanged; then
        blocker="collect (HEAD or the work tree changed during the run)"
    elif [ "${FAILED}" -eq 0 ]; then
        dmg="$(find "${SIGNED}" -maxdepth 1 -name '*_aarch64-signed.dmg' -print)"
        if [ -z "${dmg}" ] || [ ! -f "${dmg}" ]; then
            echo "every stage passed but there is no signed dmg in ${SIGNED}"
            dmg=""
        elif ! collect_verified "${dmg}"; then
            remove_collected "${dmg}"
            dmg=""
        elif ! record="$(signed_record)"; then
            echo "the signed record for BUILD-INFO.txt could not be written; the collected files are removed"
            remove_collected "${dmg}"
            blocker="collect (the signed record for BUILD-INFO.txt could not be written)"
            dmg=""
        fi
    fi
    if [ -n "${dmg}" ]; then
        if ! printf '%s\n' "${record}" >> "${OUT}/BUILD-INFO.txt"; then
            echo "BUILD-INFO.txt could not be appended; the collected files are removed"
            remove_collected "${dmg}"
            exit 1
        fi
        rm -f "${SIGNED}/UNFINISHED.txt" "${FINAL}/UNFINISHED.txt"
        rm -f "${OUT}/NOT-SIGNED.txt"
        cat "${OUT}/SHA256SUMS"
    else
        [ -n "${blocker}" ] || blocker="$(first_unsuccessful_stage)"
        if [ "${blocker}" = none ]; then
            blocker="collect (no signed dmg identical to the verified one could be collected)"
        fi
        if remove_signed_work; then
            echo "the signed work directory was removed" > "${WORK}/signed-cleanup.txt"
        else
            echo "the signed work directory could NOT be removed (a dmg stays attached); its signed copies are not a release" > "${WORK}/signed-cleanup.txt"
        fi
        write_not_signed "${blocker}"
        cat "${WORK}/signed-cleanup.txt" >> "${OUT}/NOT-SIGNED.txt"
        {
            echo "signing: FAILED at ${blocker}; NOT signed with a Developer ID, NOT notarized, NOT a release (NOT-SIGNED.txt)"
            echo "distributable: no (the signed run failed)"
        } >> "${OUT}/BUILD-INFO.txt"
        ls -l "${OUT}"
        [ "${FAILED}" -ne 0 ] || exit 1
        return 0
    fi
    ls -l "${OUT}"
}
