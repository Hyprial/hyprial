/**
 * Decision record (M1/S1/S3/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - Enters Settings through the ordinary account menu (`我的账号与 Agent 设置菜单`
 *   -> `用户设置`), never a page-level test capability.
 * - Reads dispatched method names, parameters, and the pinned `/daemon/session`
 *   catalog from the wire and asserts all Settings dispatches are read-only
 *   for both `contract-mock` (`success`) and `daemon` (`empty`).
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { assertSettingsReadOnly } from '../support/assertions.mjs';
import { launchBrowser } from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
} from '../support/cli.mjs';
import { startRealDaemon } from '../support/daemon.mjs';
import {
  CONNECTED_READY,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

async function runSettingsBrowser(handoffPath, launchUrl, origin, secrets = []) {
  const browser = await launchBrowser();
  const methodRequests = [];
  const sessionBodies = [];
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      methodRequests.push({
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        body: request.postData() ?? '',
        headers: request.headers(),
      });
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || url.pathname !== '/daemon/session') return;
      sessionBodies.push(response.json().catch(() => null));
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: '我的账号与 Agent 设置菜单' }).click();
    await page.getByRole('menuitem', { name: '用户设置' }).click();
    await page.getByRole('heading', { level: 1, name: '用户设置' }).waitFor();

    await page.locator('section[aria-label="账号登录"]').waitFor();
    const sessionCatalog = (await Promise.all(sessionBodies)).filter(Boolean).at(-1);
    assert.ok(sessionCatalog, 'the Settings page must retain the pinned session catalog');
    const requestHeaders = methodRequests.map((entry) => entry.headers);
    const requests = methodRequests.map((entry) => ({
      method: entry.method,
      params: entry.body === '' ? null : JSON.parse(entry.body),
    }));
    const storage = await page.evaluate(() => ({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      cookie: document.cookie,
      text: document.body.textContent ?? '',
    }));
    assert.equal(storage.cookie, '');
    assert.equal(JSON.stringify(storage).includes('Bearer '), false);
    for (const secret of secrets) assert.equal(JSON.stringify(storage).includes(secret), false);
    assert.equal(requestHeaders.every((entry) => (entry.authorization ?? null) === null), true);
    assert.equal(JSON.stringify(requestHeaders).includes('Bearer '), false);
    for (const secret of secrets) assert.equal(JSON.stringify(requestHeaders).includes(secret), false);
    assert.deepEqual(errors, []);
    return {
      requests,
      catalogMethods: sessionCatalog.catalog.methods,
      requestCount: requests.length,
    };
  } finally {
    await browser.close();
  }
}

test('actual CLI Settings reads are wired and read-only for both drivers', { timeout: 420_000 }, async () => {
  const evidence = [];
  const requested = process.env.HYPRIAL_SETTINGS_DRIVERS?.split(',').filter(Boolean);
  const allCases = [
    { driver: 'contract-mock', scenario: 'success' },
    { driver: 'daemon', scenario: 'empty' },
  ];
  const cases = [];
  for (const entry of allCases) {
    if (!driverEnabled(entry.driver)) {
      recordNotRun(`settings-reads ${entry.driver}/${entry.scenario}`, 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
      continue;
    }
    if (requested !== undefined && !requested.includes(entry.driver)) {
      recordNotRun(`settings-reads ${entry.driver}/${entry.scenario}`, 'filtered by HYPRIAL_SETTINGS_DRIVERS');
      continue;
    }
    cases.push(entry);
  }
  if (cases.length === 0) return;
  for (const { driver, scenario } of cases) {
    await withRoot(`hyprial-gui-settings-${driver}-`, async (root) => {
      let daemon = null;
      let gui = null;
      try {
        await prepareRoot(root);
        if (driver === 'daemon') daemon = await startRealDaemon(root);
        gui = await startActualGui({ root, driver, scenario });
        const result = await runSettingsBrowser(
          gui.handoffPath, gui.launchUrl, gui.origin, gui.secrets,
        );
        const methods = new Set(result.requests.map((entry) => entry.method));
        assert.ok(methods.has('onboarding.status'), `${driver} Settings did not read the account status`);
        assertSettingsReadOnly(result);
        evidence.push({
          driver,
          scenario,
          requests: result.requests,
          buildDigest: gui.buildDigest,
        });
        await terminateGuiAndCheck(gui, { daemon });
        process.stdout.write(
          `PASS settings-reads ${driver} methods=${[...methods].sort().join(',')}\n`,
        );
      } finally {
        if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
        if (daemon !== null) await stopChild(daemon.child);
      }
    });
  }
  assert.equal(evidence.length, cases.length);
  assert.ok(evidence.length >= 1);
  if (evidence.length === 2) assert.equal(evidence[0].buildDigest, evidence[1].buildDigest);
});
