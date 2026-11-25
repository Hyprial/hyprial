/**
 * Decision record (PR 4b-C/S3/S4 browser acceptance, 2026-11-17..2026-11-22)
 * - Tests `hyprial gui` with and without `--with-dev-tools` across
 *   `contract-mock` (N, M) and the isolated real daemon (N-daemon, D).
 * - Under the single-process Python host (#2985), `gui.secrets.length === 0`
 *   (no `request-http` bearer capability file exists).
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { runDevToolsBrowser } from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
} from '../support/cli.mjs';
import { startRealDaemon } from '../support/daemon.mjs';
import {
  driverEnabled,
  prepareRoot,
  recordNotRun,
  withRoot,
} from '../support/home.mjs';

for (const scenario of [
  { name: 'N', driver: 'contract-mock', devTools: false, layer: false, apiDocs: false },
  { name: 'N-daemon', driver: 'daemon', devTools: false, layer: false, apiDocs: false },
  { name: 'M', driver: 'contract-mock', devTools: true, layer: true, apiDocs: false },
  { name: 'D', driver: 'daemon', devTools: true, layer: true, apiDocs: true },
]) {
  test(`actual CLI developer tools case ${scenario.name}: ${scenario.devTools ? '--with-dev-tools' : 'no flag'} over ${scenario.driver}`, { timeout: 420_000 }, async () => {
    if (!driverEnabled(scenario.driver)) {
      recordNotRun(`actual-cli dev-tools ${scenario.name}`, 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
      return;
    }
    await withRoot(`hyprial-gui-dev-tools-${scenario.name.toLowerCase()}-`, async (root) => {
      let daemon = null;
      let gui = null;
      try {
        await prepareRoot(root);
        if (scenario.driver === 'daemon') daemon = await startRealDaemon(root);
        gui = await startActualGui({ root, driver: scenario.driver, devTools: scenario.devTools });
        assert.equal(gui.payload.dev_tools, scenario.devTools);
        assert.equal(gui.payload.api_docs, scenario.apiDocs);
        if (scenario.driver === 'daemon') assert.equal(gui.secrets.length, 0);
        const browser = await runDevToolsBrowser(gui, scenario);
        await terminateGuiAndCheck(gui, { daemon });
        process.stdout.write(
          `PASS actual-cli dev-tools ${scenario.name} driver=${scenario.driver} flag=${scenario.devTools} ` +
          `layer=${browser.layer} apiDocs=${browser.apiDocs} toolRequests=${browser.toolRequests} ` +
          `docsStatus=${browser.docsStatus} attach=${browser.attachCalls}\n`,
        );
      } finally {
        if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
        if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
        if (daemon !== null) await stopChild(daemon.child);
      }
    });
  });
}
