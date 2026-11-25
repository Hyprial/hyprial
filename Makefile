.PHONY: debug-gui

MODE ?= normal
PORT ?= 4173

debug-gui:
	@bash scripts/build/docker/run-gui-debug.sh "$(MODE)" "$(PORT)"

.PHONY: accept-gui
accept-gui:
	@bash scripts/build/docker/run-gui-debug.sh "$(MODE)" "$(PORT)" acceptance
