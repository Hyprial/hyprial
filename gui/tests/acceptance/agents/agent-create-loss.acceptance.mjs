/**
 * Decision record (G6 browser acceptance, 2026-11-05..2026-11-22)
 * - G6 (`agent.create` response drop): uses `harness` -> `pi` and a seeded
 *   synthetic provider key (`agent.secret.provider-write`), drops the single
 *   `agent.create` IPC response, and asserts no automatic retry and eventual
 *   authoritative `agent.list` reload.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { launchBrowser } from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
} from '../support/cli.mjs';
import {
  ipcCall,
  startRealDaemon,
} from '../support/daemon.mjs';
import {
  CONNECTED_READY,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

async function runCreateUnknownBrowser(handoffPath, launchUrl, origin, proxy, unique) {
  const browser = await launchBrowser();
  const createRequests = [];
  const errors = [];
  let listed = null;
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname === '/daemon/request/agent.create') {
        createRequests.push(request.postData());
      }
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || url.pathname !== '/daemon/request/agent.list') return;
      void response.json().then((body) => {
        const agents = body?.result?.agents;
        if (Array.isArray(agents) && agents.some((agent) => agent.actor === unique)) listed = body;
      }).catch(() => undefined);
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: /^Agent/ }).click();
    await page.getByRole('button', { name: '创建 Agent' }).first().click();
    const form = page.locator('form.agent-form');
    await form.getByLabel('名称').fill(unique);
    await form.getByLabel('harness').click();
    await page.getByRole('option', { name: 'pi', exact: true }).click();
    await form.getByRole('button', { name: '创建 Agent' }).click();
    await page.getByText(/请求已发出但结果未知；不会自动重试，请核对列表确认实际状态。/).waitFor();
    assert.equal(await form.getByRole('button', { name: '创建 Agent' }).isDisabled(), true);
    await waitFor(async () => listed !== null, 'authoritative agent.list reload', 20_000);
    assert.equal(createRequests.length, 1);
    const creates = proxy.posts.filter((entry) => entry.method === 'agent.create');
    assert.equal(creates.length, 1);
    assert.equal(creates[0].params.name, unique);
    assert.equal(proxy.dropped.length, 1);
    assert.equal(proxy.dropped[0].method, 'agent.create');
    assert.equal(proxy.dropped[0].response.result?.created, true);
    assert.ok(listed, 'the authoritative agent.list reload must contain the created agent');
    assert.equal(listed.result.agents.filter((agent) => agent.actor === unique).length, 1);
    assert.deepEqual(errors, []);
    return { createRequests: createRequests.length, listed: listed.result.agents.length };
  } finally {
    await browser.close();
  }
}

test('actual CLI real-daemon G6 drops one IPC response without retry', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli g6', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-cli-g6-', async (root) => {
    let daemon = null;
    let gui = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, { throughProxy: true });
      await ipcCall(daemon.upstreamPath, 'agent.secret.provider-write', {
        entryId: 'g6-deepseek',
        fieldName: 'DEEPSEEK_API_KEY',
        value: 'synthetic-g6-model-key',
      });
      gui = await startActualGui({ root, driver: 'daemon' });
      const unique = `m1-cli-g6-${process.pid}-${Date.now()}`;
      await runCreateUnknownBrowser(
        gui.handoffPath,
        gui.launchUrl,
        gui.origin,
        daemon.proxy,
        unique,
      );
      const agents = await ipcCall(daemon.socketPath, 'agent.list', {});
      const matches = agents.agents.filter((agent) => agent.actor === unique);
      assert.equal(matches.length, 1);
      assert.equal(daemon.proxy.posts.filter((entry) => entry.method === 'agent.create').length, 1);
      assert.equal(daemon.proxy.dropped.length, 1);
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli g6 posts=1 listed=${matches.length}\n`);
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
