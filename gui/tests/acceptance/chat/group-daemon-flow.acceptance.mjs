/**
 * Decision record (S6 real-daemon group chat flow acceptance, 2026-11-22)
 * - Covers G1, G2, G3, G4, G5, G10, G11, and G12 using isolated
 *   `hyprial daemon run` + `startActualGui` + Playwright Chromium without any
 *   `page.route`, `route.fulfill`, `route.abort`, or custom HTTP/net servers.
 * - Agents are created via real IPC `agent.create`, the Test 1 group is
 *   created via the real `hyprial message group create` CLI, and agent
 *   messages/replies are written via real IPC `message.send` (`from = agent
 *   URI`, no model involvement) so isolated homes without provider credentials
 *   exercise the real daemon group chat pipeline.
 * - Test 1 verifies group-list ordering and absence of online badges (G1),
 *   exact 3-row ordered 中文 membership events + <=5000ms agent message/reply
 *   latency (G2), full click-to-receipt single tail read + >=1800ms
 *   poll-to-poll steady and 1:1-isolation intervals (G3), MutationObserver
 *   capture of optimistic `data-delivery="sent"` / `"已发送"` replaced cleanly
 *   by polling without duplication + `operationId === idempotencyKey` (G4), UI
 *   reply context / payload / persisted `replyTo` + zero reply buttons on
 *   membership rows (G5), and departed/destroyed member author persistence via
 *   `memberNames` across reload (G10).
 * - Before G2/G4 composer sends, tests synchronize with a settled
 *   `conversations.history` response and install a `MutationObserver` on
 *   `.chat-workspace__messages` so optimistic `data-delivery="sent"` / `"已发送"`
 *   is observed race-free before polling replaces it with the recorded row.
 * - Test 2 verifies 390x844 narrow viewport usability without horizontal
 *   overflow and full keyboard accessibility (Enter, initial input focus,
 *   Tab focus trap, Escape focus return) of the create-group dialog (G11).
 */

import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import test from 'node:test';
import { promisify } from 'node:util';
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
  HYPRIAL,
  OWNER,
  cleanEnvironment,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

const runCli = promisify(execFile);

function nextKey(tag) {
  return `accept-s6-${tag}-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function attachRecorder(page, origin) {
  const posts = [];
  const receipts = [];
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
    let params = null;
    try {
      params = JSON.parse(request.postData() ?? 'null');
    } catch {
      params = null;
    }
    posts.push({
      atMs: Date.now(),
      method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
      params,
    });
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
    receipts.push(
      response.json().then((body) => ({
        atMs: Date.now(),
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        status: response.status(),
        body,
      })).catch(() => null),
    );
  });
  return { posts, receipts, pageErrors };
}

async function settledReceipts(net, fromIndex = 0) {
  return (await Promise.all(net.receipts.slice(fromIndex))).filter(Boolean);
}

async function waitForReceipt(net, fromIndex, method, predicate, label, timeoutMs = 20_000) {
  let found = null;
  await waitFor(async () => {
    const resolved = await settledReceipts(net, fromIndex);
    found = resolved.find((entry) => entry.method === method && predicate(entry)) ?? null;
    return found !== null;
  }, label, timeoutMs);
  return found;
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
  return name;
}

async function agentRow(socketPath, name) {
  const targets = await ipcCall(socketPath, 'targets', { kind: 'agent' });
  const row = (targets.targets ?? []).find(
    (item) => item.displayName === name || item.targetUri?.endsWith(`:${name}`),
  );
  assert.ok(row?.targetUri && row?.agentId, `agent target not found for ${name}`);
  return { uri: row.targetUri, agentId: row.agentId, name };
}

async function openTargetChat(page, name) {
  await page.getByRole('button', { name: /^Agent/ }).click();
  const breadcrumb = page.getByRole('link', { name: 'Agent', exact: true });
  if (await breadcrumb.count()) await breadcrumb.first().click();
  await page.getByText(name, { exact: true }).first().click();
  await page.getByRole('button', { name: '开始对话' }).click();
  await page.getByTestId('chat-layout').waitFor();
}

async function sendFromComposer(page, text) {
  await page.locator('.chat-composer__input').fill(text);
  await page.getByRole('button', { name: '发送', exact: true }).click();
}

const groupRow = (page, groupId) => page.locator(`button.chat-workspace__target[data-target-id="${groupId}"]`);
const groupSection = (page) => page.locator('section.chat-workspace__target-group[aria-label="群聊"]');
const messageRow = (page, messageId) => page.locator(`article.chat-workspace__message[data-message-id="${messageId}"]`);

async function historyLoaded(page) {
  await waitFor(async () => {
    const text = await page.locator('.chat-workspace__history-state').innerText({ timeout: 2_000 }).catch(() => 'loading');
    return !text.includes('正在加载');
  }, 'group history loaded', 20_000);
}

async function clickGroupAndVerifySingleRead(page, net, groupId, title) {
  const openIdx = net.posts.length;
  const openReceiptIdx = net.receipts.length;
  await groupRow(page, groupId).click();
  await page.locator('.chat-workspace__heading h2', { hasText: title }).waitFor();
  await waitForReceipt(net, openReceiptIdx, 'conversations.history', (entry) => entry.body?.ok === true, 'initial group conversations.history response', 10_000);
  const postsUntilReceipt = net.posts.slice(openIdx).filter((entry) => entry.method === 'conversations.history');
  assert.equal(postsUntilReceipt.length, 1, `expected single conversations.history until receipt, got ${postsUntilReceipt.length}`);
  const firstOpen = postsUntilReceipt[0];
  assert.equal(firstOpen.params?.conversationId, groupId);
  assert.equal(firstOpen.params?.mode, 'tail');
  assert.equal(firstOpen.params?.cursor, undefined);
  await historyLoaded(page);
  const openHistories = net.posts.slice(openIdx).filter((entry) => entry.method === 'conversations.history');
  const tailReads = openHistories.filter((entry) => entry.params?.mode === 'tail');
  assert.equal(tailReads.length, 1, `expected 1 tail read on open, got ${tailReads.length}`);
  assert.ok(openHistories.length <= 2, `expected <=2 history requests across open settle, got ${openHistories.length}`);
  if (openHistories.length === 2) {
    assert.equal(openHistories[1].params?.conversationId, groupId);
    assert.equal(openHistories[1].params?.mode, 'after');
    assert.notEqual(openHistories[1].params?.cursor, undefined);
  }
  return firstOpen;
}

function assertMinHistoryInterval(entries, minGapMs, label) {
  for (let i = 1; i < entries.length; i += 1) {
    const gapMs = entries[i].atMs - entries[i - 1].atMs;
    assert.ok(gapMs >= minGapMs, `${label} gap[${i - 1}->${i}]=${gapMs}ms < ${minGapMs}ms`);
  }
}

async function waitForHistoryPollSettle(net, label) {
  return waitForReceipt(net, net.receipts.length, 'conversations.history', (entry) => entry.body?.ok === true, label, 10_000);
}

async function installSentObserver(page, targetText) {
  await page.evaluate((text) => {
    window.__s6SentSeen ??= {};
    const container = document.querySelector('.chat-workspace__messages');
    if (!container) throw new Error('missing .chat-workspace__messages');
    const inspect = () => {
      for (const article of container.querySelectorAll('article.chat-workspace__message')) {
        const matched = Array.from(article.querySelectorAll('p')).some(
          (p) => !p.classList.contains('chat-workspace__reply-ref') && p.textContent?.trim() === text,
        );
        if (!matched) continue;
        const delivery = article.getAttribute('data-delivery');
        const metaText = Array.from(article.querySelectorAll('.chat-workspace__message-meta'))
          .map((el) => el.textContent?.trim() ?? '')
          .find((label) => label === '已发送') ?? null;
        if (delivery === 'sent' && metaText === '已发送') {
          window.__s6SentSeen[text] = { delivery, metaText };
          return true;
        }
      }
      return false;
    };
    if (inspect()) return;
    const observer = new MutationObserver(() => {
      if (inspect()) observer.disconnect();
    });
    observer.observe(container, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-delivery'],
    });
  }, targetText);
}

test('real-daemon group list, polling isolation, messages, reply, and departed member names', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli group-daemon-flow', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-group-s6-flow-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      const stamp = `${process.pid}-${Date.now()}`;
      const nameA = await createAgent(daemon.socketPath, `s6-ga-${stamp}`);
      const nameB = await createAgent(daemon.socketPath, `s6-gb-${stamp}`);
      const rowA = await agentRow(daemon.socketPath, nameA);
      const rowB = await agentRow(daemon.socketPath, nameB);
      const cliEnv = {
        ...cleanEnvironment(root),
        HYPRIAL_DAEMON_AUTOSTART: '0',
        HYPRIAL_SERVICE_AUTOINSTALL: '0',
      };
      const { stdout: groupStdout } = await runCli(
        HYPRIAL,
        ['message', 'group', 'create', '--title', '发布组', '--member', rowA.agentId, '--member', nameB, '--json'],
        { env: cliEnv, cwd: root, timeout: 60_000, maxBuffer: 1024 * 1024 },
      );
      const createdGroup = JSON.parse(groupStdout.trim());
      const groupId = createdGroup.conversationId;
      assert.equal(typeof groupId, 'string');
      assert.ok(groupId.length > 0);

      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await groupRow(page, groupId).waitFor();

      const firstSectionLabel = await page.locator('.chat-workspace__target-list section').first().getAttribute('aria-label');
      assert.equal(firstSectionLabel, '群聊');
      const toggleText = (await groupSection(page).locator('.chat-workspace__target-group-toggle').innerText()).replace(/\s+/g, ' ').trim();
      assert.ok(toggleText.includes('群聊 1'), `expected group toggle count, got ${toggleText}`);
      assert.equal((await groupRow(page, groupId).locator('strong').innerText()).trim(), '发布组');
      assert.equal((await groupRow(page, groupId).locator('.chat-workspace__target-role').innerText()).trim(), '3 位成员');
      assert.equal(await groupRow(page, groupId).locator('.badge').count(), 0);

      await clickGroupAndVerifySingleRead(page, net, groupId, '发布组');

      const steadyStart = Date.now();
      await delay(10_000);
      const steadyPosts = net.posts.filter((entry) => entry.atMs >= steadyStart && entry.atMs <= steadyStart + 10_000);
      const steadyHistories = steadyPosts.filter((entry) => entry.method === 'conversations.history');
      assert.ok(steadyHistories.length >= 2 && steadyHistories.length <= 6, `expected 2..6 history polls in 10s, got ${steadyHistories.length}`);
      assert.ok(steadyHistories.every((entry) => entry.params?.conversationId === groupId));
      assertMinHistoryInterval(steadyHistories, 1_800, 'G3 steady polling');
      const unexpectedSteady = [...new Set(steadyPosts.map((entry) => entry.method))]
        .filter((method) => method !== 'agent.list' && method !== 'conversations.history');
      assert.deepEqual(unexpectedSteady, []);

      const membershipSpans = page.locator('article[data-membership-event="true"] .chat-workspace__membership-event span');
      await waitFor(async () => (await membershipSpans.count()) === 3, '3 initial membership rows', 10_000);
      const initialMembershipTexts = (await membershipSpans.allInnerTexts()).map((text) => text.trim());
      assert.deepEqual(initialMembershipTexts, [
        '你创建了群聊「发布组」',
        `你添加了 ${nameA}`,
        `你添加了 ${nameB}`,
      ]);

      const firstText = `s6-a-first-${stamp}`;
      const firstBeforeMs = Date.now();
      const firstAnswer = await ipcCall(daemon.socketPath, 'message.send', {
        from: rowA.uri,
        conversationId: groupId,
        message: firstText,
        summary: firstText.slice(0, 40),
        idempotencyKey: nextKey('g2-a'),
      });
      const firstRow = page.locator('article[data-author="assistant"]', { hasText: firstText });
      await firstRow.waitFor({ timeout: 10_000 });
      const firstLatencyMs = Date.now() - firstBeforeMs;
      assert.ok(firstLatencyMs <= 5_000, `expected first agent message within 5000ms, got ${firstLatencyMs}ms`);
      assert.equal((await firstRow.locator('.chat-workspace__author').innerText()).trim(), nameA);

      const ownerText = `s6-owner-${stamp}`;
      await waitForHistoryPollSettle(net, 'G2 conversations.history settle before send');
      await installSentObserver(page, ownerText);
      const ownerReceiptIdx = net.receipts.length;
      await sendFromComposer(page, ownerText);
      const ownerReceipt = await waitForReceipt(
        net,
        ownerReceiptIdx,
        'conversations.post',
        (entry) => entry.body?.ok === true,
        'owner conversations.post receipt',
      );
      const ownerMessageId = ownerReceipt.body.result.messageId;
      assert.equal(typeof ownerMessageId, 'string');
      await waitFor(
        async () => (await messageRow(page, ownerMessageId).count()) === 1
          && (await messageRow(page, ownerMessageId).getAttribute('data-delivery')) === null,
        'G2 polling refresh replaces optimistic sent row',
        10_000,
      );
      assert.equal(await page.locator('article.chat-workspace__message', { hasText: ownerText }).count(), 1);
      const ownerSentSeen = await page.evaluate((text) => window.__s6SentSeen?.[text] ?? null, ownerText);
      assert.deepEqual(ownerSentSeen, { delivery: 'sent', metaText: '已发送' });
      assert.equal((await messageRow(page, ownerMessageId).locator('.chat-workspace__author').innerText()).trim(), '你');

      const replyFromAText = `s6-a-reply-${stamp}`;
      const replyBeforeMs = Date.now();
      const replyAnswer = await ipcCall(daemon.socketPath, 'message.send', {
        from: rowA.uri,
        conversationId: groupId,
        message: replyFromAText,
        summary: replyFromAText.slice(0, 40),
        replyTo: ownerMessageId,
        idempotencyKey: nextKey('g2-reply'),
      });
      await page.locator('article[data-author="assistant"]', { hasText: replyFromAText }).waitFor({ timeout: 10_000 });
      const replyLatencyMs = Date.now() - replyBeforeMs;
      assert.ok(replyLatencyMs <= 5_000, `expected agent reply within 5000ms, got ${replyLatencyMs}ms`);
      assert.equal(
        (await messageRow(page, replyAnswer.messageId).locator('.chat-workspace__reply-ref').innerText()).trim(),
        `回复 你：${ownerText}`,
      );

      const g4Text = `s6-g4-${stamp}`;
      await waitForHistoryPollSettle(net, 'G4 conversations.history settle before send');
      await installSentObserver(page, g4Text);
      const g4PostIdx = net.posts.length;
      const g4ReceiptIdx = net.receipts.length;
      await sendFromComposer(page, g4Text);
      const g4Receipt = await waitForReceipt(
        net,
        g4ReceiptIdx,
        'conversations.post',
        (entry) => entry.body?.ok === true,
        'G4 conversations.post receipt',
      );
      const g4Posts = net.posts.slice(g4PostIdx).filter((entry) => entry.method === 'conversations.post');
      assert.equal(g4Posts.length, 1);
      assert.equal(g4Receipt.body.result.operationId, g4Posts[0].params.idempotencyKey);
      const g4MessageId = g4Receipt.body.result.messageId;
      await waitFor(
        async () => (await messageRow(page, g4MessageId).count()) === 1
          && (await messageRow(page, g4MessageId).getAttribute('data-delivery')) === null,
        'G4 polling refresh replaces optimistic sent row',
        10_000,
      );
      assert.equal(await page.locator('article.chat-workspace__message', { hasText: g4Text }).count(), 1);
      const g4SentSeen = await page.evaluate((text) => window.__s6SentSeen?.[text] ?? null, g4Text);
      assert.deepEqual(g4SentSeen, { delivery: 'sent', metaText: '已发送' });
      const g4History = await ipcCall(daemon.socketPath, 'conversations.history', {
        from: `user:${OWNER}`,
        conversationId: groupId,
        mode: 'tail',
      });
      assert.equal(g4History.entries.some((entry) => entry.messageId === g4MessageId), true);

      await messageRow(page, firstAnswer.messageId).locator('.chat-workspace__message-actions button', { hasText: '回复' }).click();
      assert.equal(
        (await page.locator('.chat-composer__context-text').innerText()).trim(),
        `正在回复 ${nameA}：${firstText}`,
      );
      const g5Text = `s6-g5-reply-${stamp}`;
      const g5PostIdx = net.posts.length;
      const g5ReceiptIdx = net.receipts.length;
      await sendFromComposer(page, g5Text);
      await page.locator('article[data-author="user"]', { hasText: g5Text }).first().waitFor({ timeout: 15_000 });
      const g5Receipt = await waitForReceipt(
        net,
        g5ReceiptIdx,
        'conversations.post',
        (entry) => entry.body?.ok === true,
        'G5 conversations.post receipt',
      );
      const g5Posts = net.posts.slice(g5PostIdx).filter((entry) => entry.method === 'conversations.post');
      assert.equal(g5Posts.length, 1);
      assert.equal(g5Posts[0].params.replyTo, firstAnswer.messageId);
      const g5MessageId = g5Receipt.body.result.messageId;
      await waitFor(
        async () => (await messageRow(page, g5MessageId).count()) === 1
          && (await messageRow(page, g5MessageId).getAttribute('data-delivery')) === null,
        'G5 polling refresh updates reply row',
        10_000,
      );
      assert.equal(
        (await messageRow(page, g5MessageId).locator('.chat-workspace__reply-ref').innerText()).trim(),
        `回复 ${nameA}：${firstText}`,
      );
      const g5History = await ipcCall(daemon.socketPath, 'conversations.history', {
        from: `user:${OWNER}`,
        conversationId: groupId,
        mode: 'tail',
      });
      const g5Entry = g5History.entries.find((entry) => entry.messageId === g5MessageId);
      assert.equal(g5Entry?.replyTo?.messageId, firstAnswer.messageId);

      const membershipRows = await page.locator('article[data-membership-event="true"]').count();
      const membershipReplyButtons = await page.locator('article[data-membership-event="true"] .chat-workspace__message-actions button').count();
      const normalRows = await page.locator('article.chat-workspace__message:not([data-membership-event])').count();
      const normalReplyButtons = await page.locator('article.chat-workspace__message:not([data-membership-event]) .chat-workspace__message-actions button').count();
      assert.equal(membershipRows, 3);
      assert.equal(membershipReplyButtons, 0);
      assert.equal(normalReplyButtons, normalRows);

      await openTargetChat(page, nameB);
      const oneToOneText = `s6-1to1-${stamp}`;
      const oneToOneReceiptIdx = net.receipts.length;
      await sendFromComposer(page, oneToOneText);
      await page.locator('article[data-author="user"]', { hasText: oneToOneText }).first().waitFor({ timeout: 15_000 });
      await waitForReceipt(
        net,
        oneToOneReceiptIdx,
        'message.send',
        (entry) => entry.body?.ok === true,
        '1:1 message.send receipt',
      );
      await delay(3_500);

      await clickGroupAndVerifySingleRead(page, net, groupId, '发布组');
      const isoStart = Date.now();
      await delay(8_000);
      const isoPosts = net.posts.filter((entry) => entry.atMs >= isoStart && entry.atMs <= isoStart + 8_000);
      const isoHistories = isoPosts.filter((entry) => entry.method === 'conversations.history');
      assert.equal(isoHistories.filter((entry) => entry.params?.conversationId !== groupId).length, 0);
      assert.ok(isoHistories.length >= 2 && isoHistories.length <= 5, `expected bounded group polls in 8s, got ${isoHistories.length}`);
      assertMinHistoryInterval(isoHistories, 1_800, 'G3 isolation polling');
      assert.deepEqual(
        [...new Set(isoPosts.map((entry) => entry.method))].filter((m) => m !== 'agent.list' && m !== 'conversations.history'),
        [],
      );

      const backToBIdx = net.posts.length;
      await page.locator(`button.chat-workspace__target[data-target-id="${rowB.uri}"]`).click();
      await waitFor(
        () => net.posts.slice(backToBIdx).some(
          (entry) => entry.method === 'conversations.history' && entry.params?.conversationId !== groupId,
        ),
        '1:1 conversations.history resumes',
        8_000,
      );

      await groupRow(page, groupId).click();
      await historyLoaded(page);
      await ipcCall(daemon.socketPath, 'conversations.members.remove', {
        from: `user:${OWNER}`,
        conversationId: groupId,
        memberId: rowA.agentId,
        idempotencyKey: nextKey('g10-remove'),
      });
      await page.getByText(`你移除了 ${nameA}`, { exact: false }).waitFor({ timeout: 10_000 });
      assert.equal(
        (await messageRow(page, firstAnswer.messageId).locator('.chat-workspace__author').innerText()).trim(),
        nameA,
      );

      const preview = await ipcCall(daemon.socketPath, 'agent.destroy.preview', { name: nameA });
      const destroyed = await ipcCall(daemon.socketPath, 'agent.destroy', { operationId: preview.operationId });
      assert.equal(destroyed?.destroyed, true);

      await page.reload({ waitUntil: 'networkidle' });
      await page.locator(CONNECTED_READY).waitFor();
      await page.getByRole('button', { name: /^聊天/ }).click();
      await page.getByTestId('chat-layout').waitFor();
      await groupRow(page, groupId).click();
      await historyLoaded(page);

      assert.equal(
        (await messageRow(page, firstAnswer.messageId).locator('.chat-workspace__author').innerText()).trim(),
        nameA,
      );
      const allAuthors = await page.locator('article.chat-workspace__message .chat-workspace__author').allInnerTexts();
      assert.equal(allAuthors.filter((text) => /^a\./.test(text.trim())).length, 0);
      const ipcHistoryAfter = await ipcCall(daemon.socketPath, 'conversations.history', {
        from: `user:${OWNER}`,
        conversationId: groupId,
        mode: 'tail',
      });
      assert.equal(ipcHistoryAfter.memberNames?.[rowA.agentId], nameA);

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli group-daemon-flow groupId=${groupId}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('real-daemon group chat 390px narrow viewport and create-dialog keyboard accessibility', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli group-daemon-narrow-a11y', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-group-s6-narrow-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      const stamp = `${process.pid}-${Date.now()}`;
      const nameA = await createAgent(daemon.socketPath, `s6-na-${stamp}`);
      const nameB = await createAgent(daemon.socketPath, `s6-nb-${stamp}`);
      const rowA = await agentRow(daemon.socketPath, nameA);
      const rowB = await agentRow(daemon.socketPath, nameB);
      const created = await ipcCall(daemon.socketPath, 'conversations.create', {
        from: `user:${OWNER}`,
        title: 'G11 窄屏群',
        members: [rowA.agentId, rowB.agentId],
        idempotencyKey: nextKey('g11-create'),
      });
      const groupId = created.conversationId;

      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await groupRow(page, groupId).waitFor();
      assert.equal(await groupSection(page).isVisible(), true);
      const rowBox = await groupRow(page, groupId).boundingBox();
      assert.ok(rowBox && rowBox.x + rowBox.width <= 390, `group row overflows 390px: ${JSON.stringify(rowBox)}`);
      const createButton = page.getByRole('button', { name: '新建群聊' });
      assert.equal(await createButton.isVisible(), true);

      await groupRow(page, groupId).click();
      await historyLoaded(page);
      assert.equal(await page.locator('.chat-workspace__heading h2', { hasText: 'G11 窄屏群' }).isVisible(), true);

      const narrowText = `s6-g11-${stamp}`;
      const postReceiptIdx = net.receipts.length;
      await sendFromComposer(page, narrowText);
      await page.locator('article[data-author="user"]', { hasText: narrowText }).first().waitFor({ timeout: 15_000 });
      const postReceipt = await waitForReceipt(
        net,
        postReceiptIdx,
        'conversations.post',
        (entry) => entry.body?.ok === true,
        'G11 conversations.post receipt',
      );
      assert.equal(postReceipt.body.result.ok, true);

      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      assert.ok(
        overflow.scrollWidth <= overflow.innerWidth,
        `expected no horizontal overflow, got scrollWidth=${overflow.scrollWidth} innerWidth=${overflow.innerWidth}`,
      );

      await createButton.focus();
      await page.keyboard.press('Enter');
      await page.getByRole('dialog').filter({ hasText: '新建群聊' }).waitFor();
      const initialFocus = await page.evaluate(() => ({
        tag: document.activeElement?.tagName ?? null,
        inDialog: Boolean(document.activeElement?.closest('.ui-dialog')),
      }));
      assert.equal(initialFocus.inDialog, true);
      assert.equal(initialFocus.tag, 'INPUT');

      for (let step = 0; step < 8; step += 1) {
        await page.keyboard.press('Tab');
        const insideDialog = await page.evaluate(() => Boolean(document.activeElement?.closest('.ui-dialog')));
        assert.equal(insideDialog, true, `Tab step ${step + 1} escaped .ui-dialog`);
      }

      await page.keyboard.press('Escape');
      await waitFor(async () => (await page.getByRole('dialog').count()) === 0, 'create dialog closed by Escape', 5_000);
      const afterEscape = await page.evaluate(() => ({
        label: (document.activeElement?.getAttribute('aria-label') ?? document.activeElement?.textContent ?? '').trim(),
        inDialog: Boolean(document.activeElement?.closest('.ui-dialog')),
      }));
      assert.equal(afterEscape.inDialog, false);
      assert.equal(afterEscape.label, '新建群聊');

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli group-daemon-narrow-a11y groupId=${groupId}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
