# Independent Hyprial GUI

This directory is the independent Hyprial GUI product: deep modules
(studio/session/host/client/presentation) plus an assemblable HTTP composition
root (`product/server.mjs`). The real backend transport, browser E2E and
release stamping are not implemented; anything untested there is UNKNOWN rather
than promised.

The public entry points are `studio/index.mjs`, `session/index.mjs`,
`host/index.mjs` and `client/index.mjs`. The pure `presentation/index.mjs`
foundation is the single style/validation authority shared by Studio and client.
Consumers must not import another module's implementation files.
Studio owns GUI documents/revisions/storage/tools, session hides the mock or
future transport mapping, client hides presentation/composer/plugin lifecycle,
host resolves trusted identity through an injected authenticator and composes
injected ports, and `product/` is the server composition root: it binds the
HTTP boundary, issues the per-boot bearer token, serves the built assets and
maps requests onto the host port. None of these creates a second Hyprial
business session/inbox/turn authority.

## Product entry and install contract

`hyprial-install.json` declares the `hyprial.install/v2` contract:
`install.sh` (requires Node >= 24, then `npm ci --ignore-scripts`,
`npm run check`, `npm run build`) and `scripts/start-gui.sh`
(`exec node product/server.mjs`). Configuration is
explicit environment only — `HYPRIAL_GUI_TRANSPORT_DRIVER`,
`HYPRIAL_GUI_TRANSPORT_CONFIG` and `HYPRIAL_GUI_PRINCIPAL_ID` are inherited
from the caller's environment; there is no default mock in production and no
reading of real daemon credentials. With any required piece missing the server
fails closed with a named `GUI_PRODUCT_CONFIG` error before binding a socket.

When `HYPRIAL_GUI_LAUNCH_INFO_FILE` is set, the ready server atomically writes
`{schema:"hyprial.gui-launch/v1", url}` (0600) for the detached caller, and
stdout logs the origin only — the token-bearing URL never goes to a log that
would be kept. Without that variable (plain development) the full URL goes to
stdout. The mock transport (`mock-fixtures` driver) exists for development and
declared tests only; test fixtures live under `session/fixtures/` and tests
always name the driver explicitly.

The desktop wrapper (`desktop/src/gui/launcher.cjs`, via
`desktop/src/runtime/boot.cjs`) launches this same server and loads it in the
main window.

From this directory, using an isolated npm cache and explicit project prefix:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run verify
```

`node scripts/reproduce.mjs` reconstructs a fresh temporary project from these
delivered source paths, runs clean installation and the complete checks. For a
hermetic repeat with an explicitly prepared test-only npm tarball cache, use
`node scripts/reproduce.mjs --cache-from /absolute/test-cache` (offline). It never
copies login configuration or relies on the old D0 prototype directory.

The installed dependencies are pinned by the local lockfile. The project uses
the repository Apache-2.0 license; third-party licenses remain their own and
are listed in `NOTICE.md`.

Source provenance and the unchanged D0 input snapshot live in
`docs/evidence/gui-deep-modules-d1-2026-10-02/` at repository level. They are
not loaded by the runtime. The snapshot's old SHASUMS self-reference and old
bootstrap are historical evidence, not executable verification tools.

This checkpoint retains bounded mock/SSR behavior, declarative GUI document
customization and trusted local Cordis lifecycle. It does not promise arbitrary
third-party plugin binary compatibility or an in-process plugin sandbox. GUI credentials
and trusted execution context must come from an authenticated composition root;
the browser request body cannot define its own identity.
