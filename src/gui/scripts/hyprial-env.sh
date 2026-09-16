#!/usr/bin/env bash
# Keep persisted GUI/plugin identifiers compatible while using the new CLI/home.
if [[ -n "${HYPRIAL_HOME:-}${HYPRIAL_GUI_APP:-}${HYPRIAL_GUI_LAUNCH_INFO_FILE:-}" ]]; then
  export HYPRIAL_HOME="${HYPRIAL_HOME:-$HOME/.hyprial}"
  export H2B_HOME="$HYPRIAL_HOME"
  while IFS= read -r hyprial_variable; do
    case "$hyprial_variable" in
      HYPRIAL_GUI_*|HYPRIAL_DSH_*|HYPRIAL_KANBAN_*|HYPRIAL_INSTALL_*|HYPRIAL_DASHBOARD_*)
        printf -v "H2B_${hyprial_variable#HYPRIAL_}" '%s' "${!hyprial_variable}"
        export "H2B_${hyprial_variable#HYPRIAL_}"
        ;;
      DSH_HYPRIAL_*)
        printf -v "DSH_H2B_${hyprial_variable#DSH_HYPRIAL_}" '%s' "${!hyprial_variable}"
        export "DSH_H2B_${hyprial_variable#DSH_HYPRIAL_}"
        ;;
    esac
  done < <(compgen -e)
  export PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/compat-bin" && pwd -P):$PATH"
fi
