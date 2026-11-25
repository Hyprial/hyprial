/**
 * Decision record (M1/S1/S3/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - Covers ordinary App driver parity (`contract-mock/success` and `daemon/empty`,
 *   with `contract-mock/empty` marked `NOT_RUN` because its `onboarding.status`
 *   returns `authenticated=false`), `gui dev` private handoff consumption,
 *   launch expiry removal, and SIGTERM child/private-file reclamation.
 */

import assert from 'node:assert/strict';
import { once } from 'node:events';
import { stat } from 'node:fs/promises';
import test from 'node:test';
import { runBrowser } from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
  waitForPidGone,
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

test('actual CLI ordinary App: both drivers share build, page and paths', { timeout: 420_000 }, async () => {
  const evidence = [];
  const cases = [
    { driver: 'contract-mock', scenario: 'empty' },
    { driver: 'contract-mock', scenario: 'success' },
    { driver: 'daemon', scenario: 'empty' },
  ];
  for (const { driver, scenario } of cases) {
    if (!driverEnabled(driver)) {
      recordNotRun(`actual-cli ${driver}/${scenario}`, 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
      continue;
    }
    if (driver === 'contract-mock' && scenario === 'empty') {
      recordNotRun(
        'actual-cli contract-mock/empty',
        'contract-mock empty onboarding.status returns authenticated=false so AppShell renders LoginRequiredPage instead of chat-layout',
      );
      continue;
    }
    await withRoot(`hyprial-gui-cli-${driver}-${scenario}-`, async (root) => {
      let daemon = null;
      let gui = null;
      try {
        await prepareRoot(root);
        if (driver === 'daemon') daemon = await startRealDaemon(root);
        gui = await startActualGui({ root, driver, scenario });
        const before = await stat(gui.handoffPath);
        assert.equal(before.mode & 0o777, 0o600);
        const browser = await runBrowser(gui.handoffPath, gui.launchUrl, gui.origin, gui.secrets, driver, scenario);
        assert.ok(browser.requestCount >= 3);
        assert.ok(browser.receiptCount >= 1);
        assert.equal(browser.title, 'Hyprial OS');
        assert.equal(browser.path, '/');
        assert.ok(browser.requestPaths.includes('/daemon/session'));
        assert.ok(browser.requestPaths.some((path) => path.startsWith('/daemon/request/')));
        assert.equal(browser.chatBlocked, false);
        evidence.push({
          driver,
          scenario,
          buildDigest: gui.buildDigest,
          guiHome: gui.payload.gui_home,
          mode: gui.payload.mode,
          title: browser.title,
          path: browser.path,
          connectedSelector: CONNECTED_READY,
          chatState: browser.chatState,
        });
        await terminateGuiAndCheck(gui, { daemon });
        process.stdout.write(
          `PASS actual-cli ${driver}/${scenario} requests=${browser.requestCount} ` +
          `receipts=${browser.receiptCount} chat=${browser.chatState}\n`,
        );
      } finally {
        if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
        if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
        if (daemon !== null) await stopChild(daemon.child);
      }
    });
  }
  if (evidence.length === 0) return;
  const daemonEntry = evidence.find((entry) => entry.driver === 'daemon');
  const mockEmpty = evidence.find((entry) => entry.driver === 'contract-mock' && entry.scenario === 'empty');
  const mockSuccess = evidence.find((entry) => entry.driver === 'contract-mock' && entry.scenario === 'success');
  const baseline = daemonEntry ?? mockSuccess ?? evidence[0];
  for (const entry of evidence) {
    assert.equal(entry.buildDigest, baseline.buildDigest);
    assert.equal(entry.guiHome, baseline.guiHome);
    assert.equal(entry.mode, 'normal');
    assert.equal(entry.title, baseline.title);
    assert.equal(entry.path, baseline.path);
    assert.equal(entry.connectedSelector, baseline.connectedSelector);
  }
  if (mockSuccess) {
    assert.equal(mockSuccess.chatState, 'selectable-history',
      'the Contract Mock success directory must offer the workspace agent and its history');
  }
  if (mockEmpty) {
    assert.equal(mockEmpty.chatState, 'empty-directory',
      'the Contract Mock empty scenario must render the empty chat directory');
  }
  if (daemonEntry) {
    assert.equal(daemonEntry.chatState, 'empty-directory',
      'an isolated daemon with no agent renders the empty chat directory');
  }
  process.stdout.write(`PASS actual-cli driver-parity legs=${evidence.map((entry) => `${entry.driver}/${entry.scenario}`).join(',')}\n`);
});

test('actual CLI dev entry uses the same App and consumes its private handoff', { timeout: 180_000 }, async () => {
  if (!driverEnabled('contract-mock')) {
    recordNotRun('actual-cli dev', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-cli-dev-', async (root) => {
    let gui = null;
    try {
      await prepareRoot(root);
      gui = await startActualGui({ root, driver: 'contract-mock', mode: 'dev', scenario: 'success' });
      const result = await runBrowser(gui.handoffPath, gui.launchUrl, gui.origin, gui.secrets, 'contract-mock', 'success');
      assert.ok(result.requestCount >= 3);
      assert.equal(result.title, 'Hyprial OS');
      assert.equal(result.path, '/');
      await terminateGuiAndCheck(gui);
      process.stdout.write(`PASS actual-cli dev requests=${result.requestCount}\n`);
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
    }
  });
});

test('actual CLI no-open handoff is removed by launch expiry', { timeout: 180_000 }, async () => {
  if (!driverEnabled('contract-mock')) {
    recordNotRun('actual-cli handoff-expiry', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-cli-expiry-', async (root) => {
    let gui = null;
    try {
      await prepareRoot(root);
      gui = await startActualGui({ root, driver: 'contract-mock', mode: 'dev', ttlMs: 3_000 });
      assert.equal(await exists(gui.handoffPath), true);
      await waitFor(async () => !(await exists(gui.handoffPath)), 'expired handoff removal', 10_000);
      assert.equal(gui.child.exitCode, null);
      await terminateGuiAndCheck(gui);
      process.stdout.write('PASS actual-cli handoff-expiry\n');
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
    }
  });
});

test('actual CLI SIGTERM reclaims owned children and private files', { timeout: 180_000 }, async () => {
  if (!driverEnabled('contract-mock')) {
    recordNotRun('actual-cli SIGTERM cleanup', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-cli-sigterm-', async (root) => {
    let gui = null;
    try {
      await prepareRoot(root);
      gui = await startActualGui({ root, driver: 'contract-mock', mode: 'dev' });
      assert.equal(await exists(gui.handoffPath), true);
      const exited = once(gui.child, 'exit');
      gui.child.kill('SIGTERM');
      const [code, signal] = await exited;
      assert.equal(signal, null);
      assert.equal(code, 143);
      await waitForPidGone(gui.payload.frontend_pid);
      await waitForPidGone(gui.payload.backend_pid);
      await waitForPidGone(gui.payload.assets_pid);
      for (const path of [...Object.values(gui.runtimeFiles), gui.handoffPath]) {
        await waitFor(async () => !(await exists(path)), `SIGTERM cleanup of ${path}`, 10_000);
      }
      process.stdout.write('PASS actual-cli SIGTERM cleanup\n');
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
    }
  });
});
