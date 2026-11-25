/**
 * Decision record (G1/S1/S4 browser acceptance, 2026-11-18..2026-11-22)
 * - Drives Contract Mock `success` chat workspace: selects `worker`, waits
 *   for `hello`, asserts `message.history` is never called and
 *   `conversations.history` reads `c.defaultconversationaaaaaaa` with a tick
 *   rate bounded by `2 + Math.floor(elapsed / 2000)`.
 * - Accepts Contract Mock echo responses directly when `operationId` matches
 *   the caller's `idempotencyKey`; keeps recovery-row / key-lookup /
 *   key-retention assertions for non-echo responses.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import {
  captureChatLayout,
  launchBrowser,
} from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
} from '../support/cli.mjs';
import {
  CONNECTED_READY,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

async function runChatMockBrowser(handoffPath, launchUrl, origin) {
  const browser = await launchBrowser();
  const posts = [];
  const receipts = [];
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      posts.push({ method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        body: request.postData() ?? '' });
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      receipts.push(response.json().then((body) => ({
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)), body,
      })).catch(() => null));
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: /^聊天/ }).click();
    await captureChatLayout(page, 'contract-mock-success');
    const selectedAt = Date.now();
    await page.getByRole('button', { name: /worker/ }).click();
    await page.getByText('hello', { exact: true }).waitFor();
    await captureChatLayout(page, 'contract-mock-selected');
    const composer = page.getByLabel('给 worker 的消息');
    await composer.focus();
    const focused = await page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? null);
    await composer.fill('browser hello');
    const sendResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === origin && url.pathname === '/daemon/request/message.send';
    });
    await page.getByRole('button', { name: '发送' }).click();
    const sendResponse = await sendResponsePromise;
    assert.equal(sendResponse.status(), 200);
    const sendBody = await sendResponse.json();
    assert.equal(sendBody.ok, true);
    const sentOperationKey = JSON.parse(
      posts.find((entry) => entry.method === 'message.send').body,
    ).idempotencyKey;
    assert.equal(typeof sentOperationKey, 'string');
    const sendAccepted = sendBody.result?.operationId === sentOperationKey;
    const recoveryRow = page.locator('.live-status-row.chat-page-status[role="status"]');
    const lookupButton = recoveryRow.getByRole('button', { name: '查询原消息结果' });
    const abandonButton = recoveryRow.getByRole('button', { name: '放弃该操作键' });
    let keyAfterSend = null;
    let keyAfterLookup = null;
    let lookupOutcome = 'not-needed';
    if (sendAccepted) {
      await page.getByText('browser hello', { exact: true }).waitFor();
      await waitFor(async () => (await composer.inputValue()) === '', 'composer cleared after accepted send', 10_000);
      await waitFor(async () => (await recoveryRow.count()) === 0, 'recovery row absent after accepted send', 10_000);
      assert.equal(await page.evaluate(() => sessionStorage.getItem('hyprial.gui.chat.send.operationKey')), null);
    } else {
      await recoveryRow.waitFor();
      await lookupButton.waitFor();
      await abandonButton.waitFor();
      await delay(300);
      keyAfterSend = await page.evaluate(() => sessionStorage.getItem('hyprial.gui.chat.send.operationKey'));
      assert.equal(typeof keyAfterSend, 'string');
      const lookupResponsePromise = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return url.origin === origin && url.pathname === '/daemon/request/message.send.key_lookup';
      });
      await lookupButton.click();
      const lookupResponse = await lookupResponsePromise;
      assert.equal(lookupResponse.status(), 200);
      const lookupBody = await lookupResponse.json();
      assert.equal(lookupBody.ok, true);
      await delay(300);
      keyAfterLookup = await page.evaluate(() => sessionStorage.getItem('hyprial.gui.chat.send.operationKey'));
      const lookupAccepted = lookupBody.result?.status === 'accepted'
        && lookupBody.result?.commandId === keyAfterSend;
      lookupOutcome = lookupAccepted ? 'accepted' : 'unknown';
      if (lookupAccepted) {
        await waitFor(async () => (await recoveryRow.count()) === 0, 'recovery row cleared after accepted lookup', 10_000);
        assert.equal(keyAfterLookup, null);
      } else {
        await recoveryRow.waitFor();
        await lookupButton.waitFor();
        await abandonButton.waitFor();
        const recoveryText = (await recoveryRow.locator('span').first().innerText()).trim();
        assert.ok(recoveryText.length > 0);
        assert.equal(recoveryText.includes('消息已提交'), false);
        assert.equal(recoveryText.includes('已有持久发送记录'), false);
        assert.equal(keyAfterLookup, keyAfterSend);
      }
    }
    assert.equal(posts.filter((entry) => entry.method === 'message.send').length, 1);
    assert.equal(posts.filter((entry) => entry.method === 'message.send.key_lookup').length, sendAccepted ? 0 : 1);
    const resolved = (await Promise.all(receipts)).filter(Boolean);
    const query = resolved.filter((entry) => entry.method === 'message.query');
    assert.equal(query.length, 2);
    assert.deepEqual(query.map((entry) => entry.body.result.view).sort(), ['inbox', 'outbox']);
    for (const entry of query) {
      assert.equal(entry.body.ok, true);
      assert.ok(entry.body.result.entries.length >= 1);
      assert.equal(entry.body.result.entries[0].conversationId, 'conversation-1');
    }
    const elapsed = Date.now() - selectedAt;
    assert.equal(posts.filter((entry) => entry.method === 'message.history').length, 0);
    assert.equal(resolved.filter((entry) => entry.method === 'message.history').length, 0);
    const historyRequests = posts.filter((entry) => entry.method === 'conversations.history')
      .map((entry) => JSON.parse(entry.body));
    assert.ok(historyRequests.length >= 1 && historyRequests.length <= 2 + Math.floor(elapsed / 2000),
      `history reads ${historyRequests.length} in ${elapsed} ms`);
    for (const params of historyRequests) assert.equal(params.conversationId, 'c.defaultconversationaaaaaaa');
    const history = resolved.filter((entry) => entry.method === 'conversations.history');
    assert.ok(history.length >= 1);
    for (const entry of history) assert.deepEqual(entry.body.result, history[0].body.result);
    assert.equal(history[0].body.result.conversationId, 'c.defaultconversationaaaaaaa');
    assert.equal(history[0].body.result.legacyConversationId, 'dc.example-default-conversation');
    assert.deepEqual(history[0].body.result.entries.map((row) => row.messageId), ['message-1']);
    assert.deepEqual(errors, []);
    return {
      focused, sendOutcome: sendAccepted ? 'accepted' : 'unknown', lookupOutcome,
      keysRetained: sendAccepted ? false : keyAfterSend === keyAfterLookup,
      sends: posts.filter((entry) => entry.method === 'message.send').length,
      lookups: posts.filter((entry) => entry.method === 'message.send.key_lookup').length,
      queryViews: query.map((entry) => entry.body.result.view).sort(),
      historyMessages: history[0].body.result.entries.length,
    };
  } finally {
    await browser.close();
  }
}

test('actual CLI Contract Mock chat workspace reads, sends and recovers without resending', { timeout: 300_000 }, async () => {
  if (!driverEnabled('contract-mock')) {
    recordNotRun('actual-cli chat-mock', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-chat-mock-', async (root) => {
    let gui = null;
    try {
      await prepareRoot(root);
      gui = await startActualGui({ root, driver: 'contract-mock', scenario: 'success', ttlMs: 180_000 });
      const result = await runChatMockBrowser(gui.handoffPath, gui.launchUrl, gui.origin);
      assert.equal(result.focused, '给 worker 的消息');
      assert.equal(result.sendOutcome, 'accepted');
      assert.equal(result.lookupOutcome, 'not-needed');
      assert.equal(result.keysRetained, false);
      assert.equal(result.sends, 1);
      assert.equal(result.lookups, 0);
      assert.deepEqual(result.queryViews, ['inbox', 'outbox']);
      assert.equal(result.historyMessages, 1);
      await terminateGuiAndCheck(gui);
      process.stdout.write(
        `PASS actual-cli chat-mock outcome=${result.lookupOutcome} sends=${result.sends} ` +
        `lookups=${result.lookups} history=${result.historyMessages}\n`,
      );
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
    }
  });
});
