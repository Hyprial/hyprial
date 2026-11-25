/**
 * Decision record (PR 5/S3/S4 browser acceptance, 2026-11-17..2026-11-22)
 * - Cases L (`hyprial gui dev --gui-home`) and L-plain
 *   (`hyprial gui --live-source --gui-home`) run over a detached temporary Git
 *   worktree of HEAD with `node_modules` symlinked to this checkout's, rewrite
 *   one label of `RailFrame.tsx` on disk, and verify HMR/reload updates the
 *   open page within 10 s without a second launch grant on the host's public
 *   origin.
 * - Case L-daemon runs `hyprial gui dev --gui-home` over the isolated real
 *   daemon and verifies `/docs` and "Try it out" return 200 without leaking
 *   credentials.
 */

import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import test from 'node:test';
import {
  launchBrowser,
  probe,
  runDevToolsBrowser,
} from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
} from '../support/cli.mjs';
import { startRealDaemon } from '../support/daemon.mjs';
import {
  CONNECTED_READY,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
  withSourceWorktree,
} from '../support/home.mjs';

async function runLiveSourceBrowser(gui, guiHome, { layer }) {
  const browser = await launchBrowser();
  const requests = [];
  const sessions = [];
  const sockets = [];
  const errors = [];
  let navigations = 0;
  try {
    const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
    context.on('request', (request) => {
      if (!request.url().startsWith('http')) return;
      requests.push({
        url: request.url(),
        path: new URL(request.url()).pathname,
        method: request.method(),
        grant: (request.postData() ?? '').includes('"grant"'),
      });
    });
    context.on('response', (response) => {
      if (new URL(response.url()).pathname !== '/daemon/session') return;
      sessions.push(response.json().catch(() => null));
    });
    const page = await context.newPage();
    page.setDefaultTimeout(30_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('websocket', (socket) => sockets.push(socket.url()));
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) navigations += 1;
    });
    await page.goto(gui.launchUrl, { waitUntil: 'load' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(gui.handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: /^聊天/ }).waitFor();
    if (layer) {
      await page.locator('[data-dev-tools][data-dev-tools-source="backend"]').waitFor({ state: 'attached' });
    } else {
      await delay(1_500);
      assert.equal(await page.locator('[data-dev-tools]').count(), 0, 'no tool layer without --with-dev-tools');
      for (const path of ['/docs', '/docs-assets/swagger-ui.css', '/docs-assets/hyprial-docs.js', '/dev-tools/tools.js']) {
        const answer = await probe(gui.origin, path);
        assert.equal(answer.status, 404, `${path} must be 404 with live source and no tools`);
        assert.equal(JSON.parse(answer.body).code, 'NOT_FOUND');
      }
      assert.equal((await probe(gui.origin, '/src/main.tsx')).status, 200, 'a source module stays fetchable');
    }
    const opened = (await Promise.all(sessions)).filter(Boolean);
    assert.equal(opened.length, 1, 'one session was opened');
    if (layer) assert.equal(typeof opened[0].devTools, 'object');
    else assert.equal(Object.hasOwn(opened[0], 'devTools'), false);
    const navigated = navigations;
    const file = join(guiHome, 'src', 'components', 'RailFrame.tsx');
    const source = await readFile(file, 'utf8');
    assert.equal(source.split("label: '聊天'").length, 2, 'the rail label is defined once');
    const started = Date.now();
    await writeFile(file, source.replace("label: '聊天'", "label: '聊天热更'"));
    await page.getByRole('button', { name: /^聊天热更/ }).waitFor({ timeout: 10_000 });
    const elapsedMs = Date.now() - started;
    await page.locator(CONNECTED_READY).waitFor();
    const granted = requests.filter((entry) => entry.path === '/daemon/session' && entry.grant);
    assert.equal(granted.length, 1, 'the edit needed no new launch grant');
    assert.equal(await exists(gui.handoffPath), false);
    assert.equal(requests.every((entry) => new URL(entry.url).origin === gui.origin), true,
      'every request is on the public port');
    assert.ok(sockets.length >= 1, 'the page holds the HMR socket');
    assert.equal(sockets.every((url) => new URL(url).host === new URL(gui.origin).host), true,
      'every WebSocket is on the public port');
    assert.deepEqual(errors, []);
    return {
      layer, elapsedMs, requestCount: requests.length, sockets: sockets.length, reloads: navigations - navigated,
    };
  } finally {
    await browser.close();
  }
}

for (const scenario of [
  { name: 'L', mode: 'dev', liveSource: false, layer: true },
  { name: 'L-plain', mode: 'normal', liveSource: true, layer: false },
]) {
  test(`actual CLI live source case ${scenario.name}: ${scenario.mode === 'dev' ? 'gui dev' : 'gui --live-source'} reloads an edit on the public port`, { timeout: 300_000 }, async () => {
    if (!driverEnabled('contract-mock')) {
      recordNotRun(`actual-cli live-source ${scenario.name}`, 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
      return;
    }
    await withSourceWorktree(async (guiHome) => {
      await withRoot(`hyprial-gui-live-${scenario.name.toLowerCase()}-`, async (root) => {
        let gui = null;
        try {
          await prepareRoot(root);
          gui = await startActualGui({
            root, driver: 'contract-mock', mode: scenario.mode, liveSource: scenario.liveSource, guiHome,
          });
          assert.equal(gui.payload.live_source, true);
          assert.equal(gui.payload.dev_tools, scenario.layer);
          const browser = await runLiveSourceBrowser(gui, guiHome, scenario);
          await terminateGuiAndCheck(gui);
          process.stdout.write(
            `PASS actual-cli live-source ${scenario.name} layer=${browser.layer} editVisibleMs=${browser.elapsedMs} ` +
            `reloads=${browser.reloads} requests=${browser.requestCount} sockets=${browser.sockets}\n`,
          );
        } finally {
          if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
        }
      });
    });
  });
}

test('actual CLI live source case L-daemon: gui dev over the real daemon serves the API docs page', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli live-source L-daemon', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withSourceWorktree(async (guiHome) => {
    await withRoot('hyprial-gui-live-daemon-', async (root) => {
      let daemon = null;
      let gui = null;
      try {
        await prepareRoot(root);
        daemon = await startRealDaemon(root);
        gui = await startActualGui({ root, driver: 'daemon', mode: 'dev', guiHome });
        assert.equal(gui.payload.live_source, true);
        assert.equal(gui.payload.api_docs, true);
        assert.equal(gui.secrets.length, 0);
        const browser = await runDevToolsBrowser(gui, { layer: true, apiDocs: true, live: true });
        await terminateGuiAndCheck(gui, { daemon });
        process.stdout.write(
          `PASS actual-cli live-source L-daemon layer=${browser.layer} apiDocs=${browser.apiDocs} ` +
          `docsStatus=${browser.docsStatus} attach=${browser.attachCalls}\n`,
        );
      } finally {
        if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
        if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
        if (daemon !== null) await stopChild(daemon.child);
      }
    });
  });
});
