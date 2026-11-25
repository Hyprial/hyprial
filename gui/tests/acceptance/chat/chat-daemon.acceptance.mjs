/**
 * Decision record (S5 real-daemon 1:1 chat browser acceptance, 2026-11-22)
 * - Covers the 8 real-daemon 1:1 chat browser checks (REPORT items 2, 3, 4, 5,
 *   7, 10, 11, 13) using isolated `hyprial daemon run` + `startActualGui` +
 *   Playwright Chromium without any `page.route`, `route.fulfill`, `route.abort`,
 *   or custom HTTP/net server code.
 * - Agents are created via real IPC `agent.create` and agent replies are written
 *   via real IPC `message.send` (`from = agent URI`, no model involvement) so
 *   isolated homes without provider credentials exercise the real daemon path.
 * - Test 1 verifies first send omits `conversationId`, adopts returned id,
 *   replaces local `"已发送"` row with recorded entry, stops `conversations.list`
 *   polling after attach, matches IPC `conversations.history` `messageId`,
 *   displays agent reply in the same conversation, reuses `adoptedId` on second
 *   send, labels own row `'你'` and reply row with agent name, never calls
 *   `message.history`, and keeps steady-state polling bounded.
 * - Test 2 uses `startIpcDropProxy` (`dropMethod: 'message.send'`) to drop one
 *   real `message.send` IPC response, verifies unknown-result recovery without
 *   auto-resend or auto-lookup while switching to `other`, verifies manual
 *   `查询原消息结果` settles onto `unique` while leaving `other` empty, and
 *   verifies legacy `conversationId` listing, reading, and reply preservation.
 * - Test 3 stops the daemon with `SIGTERM` (`stopChild`), verifies polling
 *   failure preserves loaded messages, unlinks stale `daemon.sock`, restarts
 *   daemon on the same root (`prepareHome: false, skipFirstRun: false`), and
 *   verifies manual `刷新消息` clears the error with 0 post-restart 5xx errors.
 */

import assert from 'node:assert/strict';
import { rm } from 'node:fs/promises';
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
  OWNER,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

function attachRecorder(page, origin) {
  const posts = [];
  const receipts = [];
  const pageErrors = [];
  const requestTimes = new WeakMap();
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
    const atMs = Date.now();
    requestTimes.set(request, atMs);
    let params = null;
    try {
      params = JSON.parse(request.postData() ?? 'null');
    } catch {
      params = null;
    }
    posts.push({
      atMs,
      method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
      params,
    });
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
    const requestAtMs = requestTimes.get(response.request()) ?? Date.now();
    receipts.push(
      response.json().then((body) => ({
        atMs: Date.now(),
        requestAtMs,
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        status: response.status(),
        body,
      })).catch(() => ({
        atMs: Date.now(),
        requestAtMs,
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        status: response.status(),
        body: null,
      })),
    );
  });
  return { posts, receipts, pageErrors };
}

async function openChatApp(page, handoffPath, launchUrl) {
  await page.goto(launchUrl, { waitUntil: 'networkidle' });
  await page.locator(CONNECTED_READY).waitFor();
  await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
  await page.getByRole('button', { name: /^聊天/ }).click();
  await page.getByTestId('chat-layout').waitFor();
}

async function createAgent(socketPath, name) {
  const created = await ipcCall(socketPath, 'agent.create', { name });
  assert.equal(created?.ok, true, `agent.create failed: ${JSON.stringify(created)}`);
  assert.equal(created?.created, true, `agent.create did not create ${name}`);
  return created;
}

async function agentUri(socketPath, name) {
  const targets = await ipcCall(socketPath, 'targets', { kind: 'agent' });
  const row = (targets.targets ?? []).find(
    (item) => item.displayName === name || item.targetUri?.endsWith(`:${name}`),
  );
  assert.ok(row?.targetUri, `agent uri not found for ${name}`);
  return row.targetUri;
}

async function sendAs(socketPath, from, to, message, conversationId) {
  return ipcCall(socketPath, 'message.send', {
    from,
    to: [to],
    message,
    summary: message.split('\n')[0].slice(0, 40),
    ...(conversationId === undefined ? {} : { conversationId }),
    idempotencyKey: `accept-s5-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
  });
}

async function documentIdOf(socketPath, agentAddress, conversationId) {
  const listed = await ipcCall(socketPath, 'conversations.list', { from: `user:${OWNER}` });
  const head = (listed.conversations ?? []).find(
    (entry) => entry.legacyConversationId === conversationId && entry.peer === agentAddress,
  );
  return head?.conversationId ?? null;
}

async function openTargetChat(page, name) {
  await page.getByRole('button', { name: /^Agent/ }).click();
  const breadcrumb = page.getByRole('link', { name: 'Agent', exact: true });
  if (await breadcrumb.count()) await breadcrumb.first().click();
  await page.getByText(name, { exact: true }).first().click();
  await page.getByRole('button', { name: '开始对话' }).click();
  await page.getByTestId('chat-layout').waitFor();
}

async function sendMessage(page, text) {
  await page.locator('.chat-composer__input').fill(text);
  await page.getByRole('button', { name: '发送' }).click();
}

async function settledReceipts(net) {
  return (await Promise.all(net.receipts)).filter(Boolean);
}

test('real-daemon 1:1 chat happy path, reply, sender label, polling rate, and messageId parity', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli chat-daemon happy-path', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-chat-s5-t1-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      const unique = `s5-agent-${process.pid}-${Date.now()}`;
      await createAgent(daemon.socketPath, unique);
      const agent = await agentUri(daemon.socketPath, unique);
      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await openTargetChat(page, unique);
      await page.getByText('还没有会话；发送第一条消息后会开始新会话。').waitFor();

      const firstText = `s5-first-${Date.now()}`;
      await sendMessage(page, firstText);
      await waitFor(
        () => net.posts.some((entry) => entry.method === 'message.send'),
        'first message.send post',
        10_000,
      );
      const firstSendPost = net.posts.find((entry) => entry.method === 'message.send');
      assert.equal(Object.hasOwn(firstSendPost.params ?? {}, 'conversationId'), false);

      let sendAnswer = null;
      await waitFor(async () => {
        const resolved = await settledReceipts(net);
        sendAnswer = resolved.find((entry) => entry.method === 'message.send' && entry.body?.ok === true);
        return sendAnswer !== undefined;
      }, 'first message.send response', 20_000);
      const adoptedId = sendAnswer.body.result.conversationId;
      const sentMessageId = sendAnswer.body.result.messageId;
      assert.equal(typeof adoptedId, 'string');
      assert.ok(adoptedId.length > 0);
      assert.equal(typeof sentMessageId, 'string');

      await page.locator(`article[data-message-id="${sentMessageId}"]:not([data-delivery="sent"])`).waitFor({ timeout: 20_000 });
      assert.equal(await page.locator('article', { hasText: firstText }).count(), 1);
      assert.equal(await page.getByText('已发送').count(), 0);
      assert.equal(await page.getByText('历史尚未读取，这里只显示你刚发送的消息。').count(), 0);

      const quietFrom = Date.now();
      await delay(7_000);
      const quietListCalls = net.posts.filter(
        (entry) => entry.method === 'conversations.list' && entry.atMs >= quietFrom,
      );
      assert.equal(quietListCalls.length, 0);

      const documentId = await documentIdOf(daemon.socketPath, agent, adoptedId);
      assert.ok(documentId, 'documentId must be listed for adopted conversationId');
      const historyPage = await ipcCall(daemon.socketPath, 'conversations.history', {
        from: `user:${OWNER}`,
        conversationId: documentId,
        mode: 'tail',
      });
      assert.equal(
        historyPage.entries.some((entry) => entry.messageId === sentMessageId),
        true,
      );

      const replyText = `s5-reply-${Date.now()}`;
      const reply = await sendAs(daemon.socketPath, agent, `user:${OWNER}`, replyText);
      await page.locator('article[data-author="assistant"]', { hasText: replyText }).waitFor({ timeout: 15_000 });
      const resolvedAfterReply = await settledReceipts(net);
      const replyEntry = resolvedAfterReply
        .flatMap((entry) => (entry.method === 'conversations.history' ? (entry.body?.result?.entries ?? []) : []))
        .find((entry) => entry.messageId === reply.messageId);
      assert.equal(replyEntry?.conversationId, adoptedId);
      assert.equal(replyEntry?.from, agent);

      const secondText = `s5-second-${Date.now()}`;
      const sendCountBefore = net.posts.filter((entry) => entry.method === 'message.send').length;
      await sendMessage(page, secondText);
      await page.locator('article[data-author="user"]', { hasText: secondText }).first().waitFor({ timeout: 20_000 });
      const secondSendPost = net.posts.filter((entry) => entry.method === 'message.send')[sendCountBefore];
      assert.equal(secondSendPost?.params?.conversationId, adoptedId);

      const ownRow = page.locator(`article[data-message-id="${sentMessageId}"]`);
      assert.equal(await ownRow.getAttribute('data-author'), 'user');
      assert.equal((await ownRow.locator('.chat-workspace__author').innerText()).trim(), '你');
      const replyAuthor = await page.locator(`article[data-message-id="${reply.messageId}"] .chat-workspace__author`).innerText();
      assert.equal(replyAuthor.trim(), unique);

      const rateFrom = Date.now();
      await delay(12_000);
      const windowPosts = net.posts.filter((entry) => entry.atMs >= rateFrom);
      assert.equal(net.posts.filter((entry) => entry.method === 'message.history').length, 0);
      const historyPolls = windowPosts.filter((entry) => entry.method === 'conversations.history').length;
      const agentPolls = windowPosts.filter((entry) => entry.method === 'agent.list').length;
      assert.ok(historyPolls >= 1 && historyPolls <= 8, `expected bounded conversations.history polls, got ${historyPolls}`);
      assert.ok(agentPolls >= 1 && agentPolls <= 8, `expected bounded agent.list polls, got ${agentPolls}`);

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(
        `PASS actual-cli chat-daemon-happy adopted=${adoptedId} historyPolls=${historyPolls} agentPolls=${agentPolls}\n`,
      );
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('real-daemon 1:1 chat recovers dropped message.send response and handles legacy conversationId', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli chat-daemon recovery-legacy', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-chat-s5-t2-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        dropMethod: 'message.send',
      });
      const stamp = `${process.pid}-${Date.now()}`;
      const unique = `s5-unique-${stamp}`;
      const other = `s5-other-${stamp}`;
      await createAgent(daemon.upstreamPath, unique);
      await createAgent(daemon.upstreamPath, other);
      const uniqueUri = await agentUri(daemon.upstreamPath, unique);
      const otherUri = await agentUri(daemon.upstreamPath, other);

      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);
      const activeHeading = async () => (await page.locator('.chat-workspace__heading h2').innerText()).trim();

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await openTargetChat(page, other);
      await page.getByText('还没有会话；发送第一条消息后会开始新会话。').waitFor();
      await openTargetChat(page, unique);
      await page.getByText('还没有会话；发送第一条消息后会开始新会话。').waitFor();

      await sendMessage(page, `s5-drop-${Date.now()}`);
      await page.getByText('发送结果未知；不会自动重发，可用该操作键手动查询。').waitFor({ timeout: 20_000 });
      assert.equal(net.posts.filter((entry) => entry.method === 'message.send').length, 1);
      assert.equal(daemon.proxy.dropped.length, 1);
      assert.equal(daemon.proxy.dropped[0].method, 'message.send');

      await page.locator(`button.chat-workspace__target[data-target-id="${otherUri}"]`).click();
      await page.getByText('还没有会话；发送第一条消息后会开始新会话。').waitFor();
      assert.equal(await activeHeading(), other);
      assert.equal(await page.getByRole('button', { name: '查询原消息结果' }).count(), 1);
      assert.equal(net.posts.filter((entry) => entry.method === 'message.send.key_lookup').length, 0);

      await page.getByRole('button', { name: '查询原消息结果' }).click();
      let lookupReceipt = null;
      await waitFor(async () => {
        const resolved = await settledReceipts(net);
        lookupReceipt = resolved.find(
          (entry) => entry.method === 'message.send.key_lookup' && entry.body?.result?.status === 'accepted',
        );
        return lookupReceipt !== undefined;
      }, 'accepted key_lookup response', 20_000);
      assert.equal(lookupReceipt.body.result.status, 'accepted');
      await page.locator('article[data-author="user"]').first().waitFor({ timeout: 20_000 });
      assert.equal(await activeHeading(), unique);

      await page.locator(`button.chat-workspace__target[data-target-id="${otherUri}"]`).click();
      await page.getByText('还没有会话；发送第一条消息后会开始新会话。').waitFor();
      assert.equal(await page.locator('article.chat-workspace__message').count(), 0);

      const legacyId = `legacy-conv-s5-${stamp}`;
      const legacyText1 = `s5-legacy-1-${Date.now()}`;
      const legacySend = await sendAs(daemon.upstreamPath, `user:${OWNER}`, uniqueUri, legacyText1, legacyId);
      assert.equal(legacySend.conversationId, legacyId);
      await waitFor(async () => {
        const listed = await ipcCall(daemon.upstreamPath, 'conversations.list', { from: `user:${OWNER}` });
        return (listed.conversations ?? []).some(
          (entry) => entry.legacyConversationId === legacyId && entry.peer === uniqueUri,
        );
      }, 'legacy conversation head in conversations.list', 20_000);

      await openTargetChat(page, unique);
      const trigger = page.locator('#chat-conversation');
      await trigger.waitFor({ timeout: 20_000 });
      await trigger.click();
      const options = page.getByRole('option');
      const optionCount = await options.count();
      assert.ok(optionCount >= 2, `expected >= 2 conversation options, got ${optionCount}`);

      let foundLegacy = false;
      for (let index = 0; index < optionCount; index += 1) {
        await options.nth(index).click();
        const found = await page.locator('article[data-author="user"]', { hasText: legacyText1 })
          .waitFor({ timeout: 8_000 })
          .then(() => true)
          .catch(() => false);
        if (found) {
          foundLegacy = true;
          break;
        }
        await trigger.click();
      }
      assert.equal(foundLegacy, true, 'must locate and read the legacy conversation');

      const legacyText2 = `s5-legacy-2-${Date.now()}`;
      const sendsBeforeLegacy = net.posts.filter((entry) => entry.method === 'message.send').length;
      await sendMessage(page, legacyText2);
      await page.locator('article[data-author="user"]', { hasText: legacyText2 }).first().waitFor({ timeout: 20_000 });
      const legacySendPost = net.posts.filter((entry) => entry.method === 'message.send')[sendsBeforeLegacy];
      const legacySendReceipt = (await settledReceipts(net))
        .filter((entry) => entry.method === 'message.send')
        .at(-1);
      assert.equal(legacySendPost?.params?.conversationId, legacyId);
      assert.equal(legacySendReceipt?.body?.result?.conversationId, legacyId);

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli chat-daemon-recovery-legacy legacyId=${legacyId}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close().catch(() => undefined);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('real-daemon 1:1 chat survives daemon SIGTERM stop and recovers cleanly after restart', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli chat-daemon restart-recovery', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-chat-s5-t3-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      const unique = `s5-restart-${process.pid}-${Date.now()}`;
      await createAgent(daemon.socketPath, unique);
      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await openTargetChat(page, unique);
      await page.getByText('还没有会话；发送第一条消息后会开始新会话。').waitFor();

      const firstText = `s5-before-stop-${Date.now()}`;
      await sendMessage(page, firstText);
      await page.locator('article[data-author="user"]:not([data-delivery="sent"])', { hasText: firstText })
        .waitFor({ timeout: 20_000 });
      assert.equal(await page.getByText('已发送').count(), 0);

      await stopChild(daemon.child);
      await waitFor(
        async () => (await page.getByText('消息更新失败', { exact: false }).count()) > 0
          || (await page.getByText('历史消息加载失败', { exact: false }).count()) > 0,
        'polling failure notice after daemon stop',
        30_000,
      );
      assert.ok(
        (await page.locator('article.chat-workspace__message').count()) >= 1,
        'already-loaded messages must remain visible while daemon is stopped',
      );

      await rm(join(root, 'state', 'daemon.sock'), { force: true });
      daemon = await startRealDaemon(root, { prepareHome: false, skipFirstRun: false });
      const restartAt = Date.now();
      await delay(2_000);

      let recovered = false;
      for (let attempt = 0; attempt < 2 && !recovered; attempt += 1) {
        await page.getByRole('button', { name: '刷新消息' }).click();
        recovered = await waitFor(
          async () => (await page.getByText('消息更新失败', { exact: false }).count()) === 0
            && (await page.getByText('历史消息加载失败', { exact: false }).count()) === 0
            && (await page.locator('article[data-author="user"]').count()) >= 1,
          'recovered message view after daemon restart',
          20_000,
        ).then(() => true).catch(() => false);
      }
      assert.equal(recovered, true, 'chat must recover cleanly after daemon restart');

      await delay(500);
      const settled = await settledReceipts(net);
      const afterRestart = settled.filter((entry) => entry.requestAtMs >= restartAt);
      assert.ok(afterRestart.length >= 1, 'expected settled responses after restart');
      const serverErrors = afterRestart.filter((entry) => entry.status >= 500);
      assert.equal(serverErrors.length, 0, `expected 0 post-restart 5xx responses, got ${JSON.stringify(serverErrors)}`);

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli chat-daemon-restart-recovery postRestart=${afterRestart.length}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
