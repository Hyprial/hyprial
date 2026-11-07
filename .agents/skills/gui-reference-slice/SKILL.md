---
name: gui-reference-slice
description: Build and verify the opt-in Agent-provider reference slice in root gui/: MethodSpec, generated client, H-01 reads, and development Contract Mock. Not a full GUI backend or write-admission release.
---

# Agent-provider reference integration

Read `../../../gui/docs/agent-provider-reference.md` and the shared-GUI rules before changing this slice.

- Keep `gui/src/features/agent-provider/AgentProviderSection.tsx` shared. The API maps generated business types to component view models; components do not select a backend.
- Change MethodSpec first. Run `uv run python scripts/verify/dump_daemon_openapi.py --write`, then `npm --prefix gui run client:reference`. Never hand-edit generated types/examples.
- HTTP default-provider requests accept only `{}`. Login/logout are declared but not HTTP-exposed until H-03. IPC compatibility is unchanged.
- Select reference targets explicitly with an owner-owned 0600 `HYPRIAL_GUI_REFERENCE_CONFIG`. Keep H-01 tokens in that host file, never in browser code, URL, evidence or a commit. Do not use a production daemon for tests.
- Development scenarios call the real Contract Mock service. Missing configuration fails closed; no local success fallback. Unit fixtures live under `gui/tests/`, not in the product API.
- Use Node 24. Set `DEV01_CHROMIUM_EXECUTABLE` to a local Chromium executable. Run `npm --prefix gui run verify`; also run every remaining `verify:*` package script.
- Real browser proof: `NO_PROXY=127.0.0.1,localhost,::1 uv run pytest tests/integration/request_http/test_gui_reference.py -q -s`. It creates isolated homes, daemon/H-01 processes, Mock service and GUI host. The test-only host supplies the documented synthetic sentinel; product login still requires trusted host input.
- Report live write rejection separately from Mock success. Neither proves H-03 admission, real provider authentication, native-shell operation or wheel release acceptance.
