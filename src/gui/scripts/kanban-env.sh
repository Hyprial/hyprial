#!/usr/bin/env bash
# Load only literal, allowlisted GUI paths from the installed Kanban config.
# node must already have been activated by runtime.sh.
h2b_gui_load_kanban_environment() {
  local assignment
  while IFS= read -r -d '' assignment; do
    export "$assignment"
  done < <(node "$repo_root/integration/kanban-gui.mjs" environment)
}
