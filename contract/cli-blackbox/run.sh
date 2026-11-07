#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)
RESOLVER=$SCRIPT_DIR/../python-resolver.sh
if [ -f "$RESOLVER" ]; then
    # Resolved relative to this entry point at runtime.
    # shellcheck disable=SC1090
    . "$RESOLVER"
    hyprial_resolve_python "$REPO_ROOT"
else
    # Keep the language-neutral black-box directory independently copyable.
    if [ "${HYPRIAL_PYTHON+x}" = x ]; then
        if [ -z "$HYPRIAL_PYTHON" ]; then
            printf '%s\n' 'contract Python error: HYPRIAL_PYTHON is empty' >&2
            exit 1
        fi
    else
        HYPRIAL_PYTHON=$(command -v python3 2>/dev/null || true)
        if [ -z "$HYPRIAL_PYTHON" ]; then
            printf '%s\n' 'contract Python error: python3 was not found on PATH' >&2
            exit 1
        fi
    fi
    case $HYPRIAL_PYTHON in
        */*) ;;
        *)
            hyprial_standalone_python=$(command -v "$HYPRIAL_PYTHON" 2>/dev/null || true)
            if [ -z "$hyprial_standalone_python" ]; then
                printf 'contract Python error: HYPRIAL_PYTHON does not exist on PATH: %s\n' \
                    "$HYPRIAL_PYTHON" >&2
                exit 1
            fi
            HYPRIAL_PYTHON=$hyprial_standalone_python
            ;;
    esac
    if [ -L "$HYPRIAL_PYTHON" ] && [ ! -e "$HYPRIAL_PYTHON" ]; then
        printf 'contract Python error: selected interpreter does not exist (broken symlink): %s\n' \
            "$HYPRIAL_PYTHON" >&2
        exit 1
    fi
    if [ ! -e "$HYPRIAL_PYTHON" ]; then
        printf 'contract Python error: selected interpreter does not exist: %s\n' \
            "$HYPRIAL_PYTHON" >&2
        exit 1
    fi
    if [ ! -f "$HYPRIAL_PYTHON" ]; then
        printf 'contract Python error: selected interpreter is not a file: %s\n' \
            "$HYPRIAL_PYTHON" >&2
        exit 1
    fi
    if [ ! -x "$HYPRIAL_PYTHON" ]; then
        printf 'contract Python error: selected interpreter is not executable: %s\n' \
            "$HYPRIAL_PYTHON" >&2
        exit 1
    fi
    hyprial_standalone_identity=$(
        "$HYPRIAL_PYTHON" -c \
            'import sys; print("hyprial-contract-python-v1", *sys.version_info[:3], sep=":")' \
            2>&1
    ) || {
        printf 'contract Python error: selected interpreter failed the Python identity probe (%s): %s\n' \
            "$hyprial_standalone_identity" "$HYPRIAL_PYTHON" >&2
        exit 1
    }
    hyprial_standalone_old_ifs=$IFS
    IFS=:
    # shellcheck disable=SC2086
    set -- $hyprial_standalone_identity
    IFS=$hyprial_standalone_old_ifs
    if [ "$#" -ne 4 ] || [ "$1" != hyprial-contract-python-v1 ]; then
        printf 'contract Python error: selected interpreter did not emit the exact Python identity (got: %s): %s\n' \
            "$hyprial_standalone_identity" "$HYPRIAL_PYTHON" >&2
        exit 1
    fi
    for hyprial_standalone_part in "$2" "$3" "$4"; do
        case $hyprial_standalone_part in
            ''|*[!0-9]*)
                printf 'contract Python error: selected interpreter did not emit the exact Python identity (got: %s): %s\n' \
                    "$hyprial_standalone_identity" "$HYPRIAL_PYTHON" >&2
                exit 1
                ;;
        esac
    done
    if [ "$2" -lt 3 ] || { [ "$2" -eq 3 ] && [ "$3" -lt 12 ]; }; then
        printf 'contract Python error: selected interpreter requires Python >=3.12 (got Python %s.%s.%s): %s\n' \
            "$2" "$3" "$4" "$HYPRIAL_PYTHON" >&2
        exit 1
    fi
    export HYPRIAL_PYTHON
fi
exec "$HYPRIAL_PYTHON" "$SCRIPT_DIR/run.py" "$@"
