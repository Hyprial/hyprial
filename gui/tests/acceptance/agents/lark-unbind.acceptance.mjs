/**
 * Decision record (S3/S4 browser acceptance, 2026-11-10..2026-11-23)
 * - Drives `AgentDetail` -> `平台绑定` tab `解除绑定` flow across all 5
 *   non-replaying scenarios (`normal-success`, `lost-response-null`,
 *   `lost-response-same`, `explicit-retry-success`, `lost-response-third`).
 * - The page names the bound agent by its targets `displayName`, never by the
 *   id-form URI (UUID addressing).
 * - I6-4: the binding is the agent cell document's adapter member.  Harness-side
 *   reads use `adapter.list` (agent field resolved from the cell routing index);
 *   writes use `agent.cell.get` + `agent.cell.update` (CAS).  The GUI unbind is
 *   the cell.update whose members no longer contain the adapter member.
 * - Seeds `channels.json` and `secrets/lark-lark-e1-test.json` after
 *   `prepareDaemonHome` and before `startRealDaemon(..., { prepareHome: false })`.
 */

import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
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
  delay,
  driverEnabled,
  fileDigest,
  prepareDaemonHome,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

async function runLarkUnbindBrowser(gui, daemon, evidenceDir, actors) {
  const browser = await launchBrowser();
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
  page.setDefaultTimeout(20_000);
  const { adapter, expected, expectedName, third } = actors;
  const observed = [];
  const unbindCount = () => daemon.proxy.posts.filter((entry) => entry.method === 'agent.cell.update'
    && Array.isArray(entry.params?.members)
    && !entry.params.members.some((member) => member?.ref === adapter)).length;
  const boundAdapters = async () => {
    const result = await ipcCall(daemon.upstreamPath, 'adapter.list', {});
    return Object.fromEntries(result.adapters.map((item) => [item.name, item.agent ?? null]));
  };
  const setBinding = async (actor) => {
    const current = await ipcCall(daemon.upstreamPath, 'agent.cell.get', { name: actor });
    const cell = current.cell;
    const memberData = { ...(cell.params['cell.members'] ?? {}) };
    memberData[adapter] = { adapter };
    const members = [
      ...cell.members.filter((member) => member.ref !== adapter),
      { ref: adapter, role: 'adapter' },
    ];
    await ipcCall(daemon.upstreamPath, 'agent.cell.update', {
      name: actor,
      expectedRevision: cell.revision,
      members,
      params: { ...cell.params, 'cell.members': memberData },
    });
  };
  const refreshBound = async () => {
    await panel.getByRole('button', { name: '刷新', exact: true }).click();
    await unbindButton().waitFor();
  };
  let panel;
  let row;
  const unbindButton = () => row.getByRole('button', { name: '解除绑定', exact: true });
  const bindButton = () => row.getByRole('button', { name: '绑定', exact: true });
  const alertNotice = () => panel.locator('p.agent-form-message[role="alert"]');
  try {
    await mkdir(evidenceDir, { recursive: true });
    await page.goto(gui.launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await page.getByRole('button', { name: /^Agent/ }).first().click();
    await page.getByTestId('contact-list').locator('button').filter({ hasText: expectedName }).first().click();
    panel = page.locator('[data-testid="agent-detail-panel"]');
    await panel.waitFor();
    await panel.getByRole('tab', { name: '平台绑定', exact: true }).click();
    await panel.getByRole('heading', { name: '平台绑定', exact: true }).waitFor();
    row = panel.locator('li.platform-binding-row').filter({ hasText: adapter });
    await unbindButton().waitFor();

    assert.equal(await panel.getByText(expected).count(), 0, 'the id-form URI must not be shown');
    assert.equal(await panel.getByText(third).count(), 0, 'the id-form third URI must not be shown');
    for (const width of [375, 760, 1366]) {
      await page.setViewportSize({ width, height: 900 });
      await row.scrollIntoViewIfNeeded();
      await delay(150);
      await page.screenshot({ path: join(evidenceDir, `lark-unbind-confirm-${width}.png`) });
    }
    const beforeNormal = unbindCount();
    await unbindButton().click();
    await bindButton().waitFor();
    await row.getByText('未绑定', { exact: true }).waitFor();
    assert.equal(await alertNotice().count(), 0);
    observed.push({ scenario: 'normal-success', unbindRequests: unbindCount() - beforeNormal,
      bindings: await boundAdapters() });

    await setBinding(expected);
    await refreshBound();
    daemon.dropScenario = 'null';
    daemon.proxy.arm();
    const beforeLostNull = unbindCount();
    await unbindButton().click();
    await alertNotice().waitFor();
    assert.match(await alertNotice().innerText(), /未确认/);
    await bindButton().waitFor();
    await row.getByText('未绑定', { exact: true }).waitFor();
    observed.push({ scenario: 'lost-response-null', unbindRequests: unbindCount() - beforeLostNull,
      bindings: await boundAdapters() });

    await setBinding(expected);
    await refreshBound();
    daemon.dropScenario = 'same';
    daemon.proxy.arm();
    const beforeSame = unbindCount();
    await unbindButton().click();
    await alertNotice().waitFor();
    assert.match(await alertNotice().innerText(), /未确认/);
    await unbindButton().waitFor();
    observed.push({ scenario: 'lost-response-same', unbindRequests: unbindCount() - beforeSame,
      bindings: await boundAdapters() });

    daemon.dropScenario = 'none';
    await unbindButton().click();
    await bindButton().waitFor();
    await row.getByText('未绑定', { exact: true }).waitFor();
    assert.equal(await alertNotice().count(), 0);
    observed.push({ scenario: 'explicit-retry-success', unbindRequests: unbindCount() - beforeSame,
      bindings: await boundAdapters() });

    await setBinding(expected);
    await refreshBound();
    daemon.dropScenario = 'third';
    daemon.proxy.arm();
    const beforeThird = unbindCount();
    await unbindButton().click();
    await alertNotice().waitFor();
    assert.match(await alertNotice().innerText(), /未确认/);
    await row.getByText('lark-e1-third', { exact: false }).waitFor();
    assert.equal(await panel.getByText(third).count(), 0, 'the id-form third URI must not be shown');
    assert.equal(await unbindButton().count(), 0, 'third-bound adapter must not expose unbind here');
    observed.push({ scenario: 'lost-response-third', unbindRequests: unbindCount() - beforeThird,
      bindings: await boundAdapters() });
    assert.equal((await boundAdapters())[adapter], third);

    const screenshots = {};
    for (const width of [375, 760, 1366]) {
      const path = join(evidenceDir, `lark-unbind-confirm-${width}.png`);
      screenshots[width] = { path, sha256: await fileDigest(path) };
    }
    const evidence = { adapter, expected, third, observed, screenshots,
      totalUnbindRequests: unbindCount(), droppedResponses: daemon.proxy.dropped.length };
    await writeFile(join(evidenceDir, 'lark-unbind-e1.json'), `${JSON.stringify(evidence, null, 2)}\n`);
    return evidence;
  } finally {
    await browser.close();
  }
}

test('actual CLI real-daemon Connections page conditionally unbinds Lark', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli lark-unbind', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-lark-unbind-', async (root) => {
    let daemon = null;
    let gui = null;
    try {
      await prepareRoot(root);
      await prepareDaemonHome(root);
      const adapter = 'lark-e1-test';
      await writeFile(join(root, 'home', 'channels.json'), JSON.stringify({
        version: 1,
        gateways: [{
          provider: 'lark',
          name: adapter,
          appId: 'cli_synthetic_e1',
          credentialRef: 'lark-lark-e1-test',
          routes: [],
        }],
      }));
      await mkdir(join(root, 'home', 'secrets'), { recursive: true, mode: 0o700 });
      await writeFile(
        join(root, 'home', 'secrets', 'lark-lark-e1-test.json'),
        JSON.stringify({ appSecret: 'isolated-synthetic-e1-secret' }),
        { mode: 0o600 },
      );
      const bindViaCell = async (upstreamPath, actor) => {
        const current = await ipcCall(upstreamPath, 'agent.cell.get', { name: actor });
        const cell = current.cell;
        const memberData = { ...(cell.params['cell.members'] ?? {}) };
        memberData[adapter] = { adapter };
        await ipcCall(upstreamPath, 'agent.cell.update', {
          name: actor,
          expectedRevision: cell.revision,
          members: [
            ...cell.members.filter((member) => member.ref !== adapter),
            { ref: adapter, role: 'adapter' },
          ],
          params: { ...cell.params, 'cell.members': memberData },
        });
      };
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        prepareHome: false,
        dropMethod: 'agent.cell.update',
        armAfterMethod: '__manual_only__',
        onDrop: async (_record, upstreamPath) => {
          const actor = daemon?.dropScenario === 'same'
            ? daemon.actors.expected
            : daemon?.dropScenario === 'third'
              ? daemon.actors.third
              : null;
          if (actor !== null) {
            await bindViaCell(upstreamPath, actor);
          }
        },
      });
      await waitFor(async () => {
        try {
          return (await ipcCall(daemon.upstreamPath, 'ping', {})).phase === 'serving';
        } catch {
          return false;
        }
      }, 'real daemon serving phase', 30_000);
      const createdExpected = await ipcCall(daemon.upstreamPath, 'agent.create', { name: 'lark-e1-old' });
      const createdThird = await ipcCall(daemon.upstreamPath, 'agent.create', { name: 'lark-e1-third' });
      daemon.actors = {
        expected: createdExpected.agent.uri,
        third: createdThird.agent.uri,
      };
      daemon.dropScenario = 'none';
      await bindViaCell(daemon.upstreamPath, daemon.actors.expected);
      gui = await startActualGui({ root, driver: 'daemon' });
      const evidenceDir = process.env.HYPRIAL_LARK_EVIDENCE_DIR
        ?? join(tmpdir(), `gui-rel-d-lark-e1-${process.pid}`);
      const evidence = await runLarkUnbindBrowser(gui, daemon, evidenceDir, {
        adapter,
        expected: daemon.actors.expected,
        expectedName: 'lark-e1-old',
        third: daemon.actors.third,
      });
      assert.deepEqual(evidence.observed.map((entry) => [entry.scenario, entry.unbindRequests]), [
        ['normal-success', 1],
        ['lost-response-null', 1],
        ['lost-response-same', 1],
        ['explicit-retry-success', 2],
        ['lost-response-third', 1],
      ]);
      assert.equal(evidence.droppedResponses, 3);
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli lark-unbind evidence=${evidenceDir}\n`);
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
