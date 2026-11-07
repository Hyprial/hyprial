#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
# Resolved relative to this entry point at runtime.
# shellcheck disable=SC1091
. "$SCRIPT_DIR/python-resolver.sh"
hyprial_resolve_python "$REPO_ROOT"
hyprial_require_project_dependencies "$REPO_ROOT"

# An explicit --hyprial remains argparse's highest-priority selection. Otherwise
# preserve an explicit HYPRIAL_BIN, then prefer the executable built by this
# checkout before allowing the standalone CLI runner to fall back to PATH.
hyprial_cli_argument_present=false
for hyprial_contract_arg do
    case $hyprial_contract_arg in
        --hyprial|--hyprial=*) hyprial_cli_argument_present=true ;;
    esac
done
if [ "$hyprial_cli_argument_present" = false ] && [ "${HYPRIAL_BIN+x}" != x ]; then
    hyprial_repo_cli=$REPO_ROOT/.venv/bin/hyprial
    if [ -e "$hyprial_repo_cli" ] || [ -L "$hyprial_repo_cli" ]; then
        if [ -L "$hyprial_repo_cli" ] && [ ! -e "$hyprial_repo_cli" ]; then
            printf 'contract CLI error: repository .venv/bin/hyprial does not exist (broken symlink): %s\n' \
                "$hyprial_repo_cli" >&2
            exit 1
        fi
        if [ ! -f "$hyprial_repo_cli" ]; then
            printf 'contract CLI error: repository .venv/bin/hyprial is not a file: %s\n' \
                "$hyprial_repo_cli" >&2
            exit 1
        fi
        if [ ! -x "$hyprial_repo_cli" ]; then
            printf 'contract CLI error: repository .venv/bin/hyprial is not executable: %s\n' \
                "$hyprial_repo_cli" >&2
            exit 1
        fi
        HYPRIAL_BIN=$hyprial_repo_cli
        export HYPRIAL_BIN
    fi
fi

"$SCRIPT_DIR/cli-blackbox/run.sh" "$@"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/wire/golden/validate.py"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/e2e/validate.py"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/daemon-lifecycle/run.py"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/http-request/validate.py"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/mcp-tools/run.py"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/workers/python-worker/validate.py"
"$HYPRIAL_PYTHON" "$SCRIPT_DIR/workers/user-proxy/validate.py"
