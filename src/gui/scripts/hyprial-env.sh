#!/usr/bin/env bash
# Keep persisted GUI/plugin identifiers compatible while using the new CLI/home.
# A standalone modern GUI setting must not require an unrelated HOME/APP flag.
# Empty-only environments retain legacy behavior; once active, explicitly empty
# modern values still override their legacy aliases in the translation below.
hyprial_gui_modern_environment=0
if [[ -n "${HYPRIAL_HOME:-}${HYPRIAL_GUI_APP:-}${HYPRIAL_GUI_LAUNCH_INFO_FILE:-}" ]]; then
  hyprial_gui_modern_environment=1
else
  while IFS= read -r hyprial_variable; do
    case "$hyprial_variable" in
      HYPRIAL_GUI_*|HYPRIAL_DSH_*|HYPRIAL_KANBAN_*|HYPRIAL_INSTALL_*|DSH_HYPRIAL_*)
        if [[ -n "${!hyprial_variable}" ]]; then
          hyprial_gui_modern_environment=1
          break
        fi
        ;;
    esac
  done < <(compgen -e)
fi
if ((hyprial_gui_modern_environment)); then
  export HYPRIAL_HOME="${HYPRIAL_HOME:-$HOME/.hyprial}"
  export H2B_HOME="$HYPRIAL_HOME"
  while IFS= read -r hyprial_variable; do
    case "$hyprial_variable" in
      HYPRIAL_GUI_*|HYPRIAL_DSH_*|HYPRIAL_KANBAN_*|HYPRIAL_INSTALL_*)
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
unset hyprial_gui_modern_environment
