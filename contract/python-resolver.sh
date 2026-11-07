#!/bin/sh
# Shared Python selection for contract shell entry points. This file is sourced.

hyprial_python_fail() {
    printf 'contract Python error: %s\n' "$*" >&2
    return 1
}

hyprial_resolve_python() {
    hyprial_repo_root=$1

    if [ "${HYPRIAL_PYTHON+x}" = x ]; then
        if [ -z "$HYPRIAL_PYTHON" ]; then
            hyprial_python_fail 'HYPRIAL_PYTHON is empty; set it to a Python >=3.12 executable'
            return 1
        fi
        hyprial_python_candidate=$HYPRIAL_PYTHON
        hyprial_python_source=HYPRIAL_PYTHON
    elif [ -e "$hyprial_repo_root/.venv/bin/python" ] || [ -L "$hyprial_repo_root/.venv/bin/python" ]; then
        hyprial_python_candidate=$hyprial_repo_root/.venv/bin/python
        hyprial_python_source='repository .venv/bin/python'
    elif [ -n "${VIRTUAL_ENV:-}" ]; then
        hyprial_python_candidate=$VIRTUAL_ENV/bin/python
        hyprial_python_source='asserted VIRTUAL_ENV/bin/python'
    else
        hyprial_python_candidate=$(command -v python3 2>/dev/null || true)
        hyprial_python_source='python3 on PATH'
        if [ -z "$hyprial_python_candidate" ]; then
            hyprial_python_fail 'python3 was not found; run uv sync --extra test or set HYPRIAL_PYTHON'
            return 1
        fi
    fi

    case $hyprial_python_candidate in
        */*)
            ;;
        *)
            hyprial_python_path=$(command -v "$hyprial_python_candidate" 2>/dev/null || true)
            if [ -z "$hyprial_python_path" ]; then
                hyprial_python_fail "$hyprial_python_source does not exist on PATH: $hyprial_python_candidate"
                return 1
            fi
            hyprial_python_candidate=$hyprial_python_path
            ;;
    esac
    if [ -L "$hyprial_python_candidate" ] && [ ! -e "$hyprial_python_candidate" ]; then
        hyprial_python_fail "$hyprial_python_source does not exist (broken symlink): $hyprial_python_candidate"
        return 1
    fi
    if [ ! -e "$hyprial_python_candidate" ]; then
        hyprial_python_fail "$hyprial_python_source does not exist: $hyprial_python_candidate"
        return 1
    fi
    if [ ! -f "$hyprial_python_candidate" ]; then
        hyprial_python_fail "$hyprial_python_source is not a file: $hyprial_python_candidate"
        return 1
    fi
    if [ ! -x "$hyprial_python_candidate" ]; then
        hyprial_python_fail "$hyprial_python_source is not executable: $hyprial_python_candidate"
        return 1
    fi

    hyprial_python_identity=$(
        "$hyprial_python_candidate" -c \
            'import sys; print("hyprial-contract-python-v1", *sys.version_info[:3], sep=":")' \
            2>&1
    ) || {
        hyprial_python_fail "$hyprial_python_source failed the Python identity probe ($hyprial_python_identity): $hyprial_python_candidate"
        return 1
    }

    hyprial_python_old_ifs=$IFS
    IFS=:
    # The probe must emit exactly four colon-delimited fields on one line.
    # shellcheck disable=SC2086
    set -- $hyprial_python_identity
    IFS=$hyprial_python_old_ifs
    if [ "$#" -ne 4 ] || [ "$1" != hyprial-contract-python-v1 ]; then
        hyprial_python_fail "$hyprial_python_source did not emit the exact Python identity (got: $hyprial_python_identity): $hyprial_python_candidate"
        return 1
    fi
    for hyprial_python_part in "$2" "$3" "$4"; do
        case $hyprial_python_part in
            ''|*[!0-9]*)
                hyprial_python_fail "$hyprial_python_source did not emit the exact Python identity (got: $hyprial_python_identity): $hyprial_python_candidate"
                return 1
                ;;
        esac
    done
    if [ "$2" -lt 3 ] || { [ "$2" -eq 3 ] && [ "$3" -lt 12 ]; }; then
        hyprial_python_fail "$hyprial_python_source requires Python >=3.12 (got Python $2.$3.$4): $hyprial_python_candidate"
        return 1
    fi

    HYPRIAL_PYTHON=$hyprial_python_candidate
    export HYPRIAL_PYTHON
}

hyprial_require_project_dependencies() {
    hyprial_repo_root=$1
    hyprial_missing_modules=$(
        PYTHONPATH="$hyprial_repo_root/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$HYPRIAL_PYTHON" -c '
import importlib.util
modules = ("zenoh", "lark_oapi", "mcp", "typer", "rich")
missing = [name for name in modules if importlib.util.find_spec(name) is None]
print(", ".join(missing))
raise SystemExit(bool(missing))
' 2>/dev/null
    ) || {
        if [ -z "$hyprial_missing_modules" ]; then
            hyprial_missing_modules='dependency probe failed before it could identify modules'
        fi
        hyprial_python_fail "missing required project dependencies for $HYPRIAL_PYTHON: $hyprial_missing_modules; run 'uv sync --extra test' in $hyprial_repo_root"
        return 1
    }
}
