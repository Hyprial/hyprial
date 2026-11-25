/*
 * Decision record (WP2, gui-dev-product-parity-2026-11-09; dev tools seam,
 * PR 4a, 2026-11-14):
 * - DevPreviewApp is the fixture pairing of the floating tool layer: the
 *   shell from `./tools` plus the fixture tool contributions from
 *   `./fixture-tools`. It renders no product App and stays outside the
 *   product #root. Unit tests and fixture harnesses render it directly.
 * - Entries do not import this file. They load `./tools` dynamically and call
 *   mountDevTools with a context, so the same layer can be mounted over the
 *   real backend composition without the fixture tools.
 * - The preview store and its URL mirror live in `./fixture-tools`.
 */
import { createFixtureDevToolsContext } from './fixture-tools';
import { DevToolsLayer } from './tools';

export function DevPreviewApp() {
  return <DevToolsLayer context={createFixtureDevToolsContext()} />;
}
