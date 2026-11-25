/**
 * Decision record (S6 real-daemon group chat admin & recovery acceptance, 2026-11-22)
 * - Covers G6, G7, G8, G9, and G12 using isolated `hyprial daemon run` +
 *   `startActualGui` + Playwright Chromium without any `page.route`,
 *   `route.fulfill`, `route.abort`, or custom HTTP/net server code.
 * - Test 1 uses `startRealDaemon({ throughProxy: true, dropMethod:
 *   'conversations.post' })` (with all helper IPC calls targeting
 *   `daemon.upstreamPath`) to drop one real `conversations.post` IPC answer
 *   and assert unknown-result recovery without auto-resend, 1:1 independence,
 *   persisted warning across tab switch, manual dismiss, and full
 *   `page.reload()` persistence of the message with clean composer state (G6).
 * - Test 2 verifies zero extra backend requests of any method when opening
 *   「新建群聊」(synchronized after `agent.list` settle), UI group creation
 *   without auto-open + top highlighted placement (G7), members panel
 *   `conversations.get` / add / remove without extra reloads, and real
 *   `AGENT_NOT_FOUND` rejection triggered by `agent.destroy.preview` +
 *   `agent.destroy` (with up to 3 attempts in case the 2s `agent.list` poll
 *   clears the selection before clicking add) (G8).
 * - Test 3 uses `startRealDaemon({ throughProxy: true, dropMethod:
 *   'conversations.create' })` to verify dialog unknown-outcome recovery and
 *   manual retry reusing the exact `idempotencyKey` to converge on one group
 *   (G9).
 * - Own-message waits target `article[data-author="user"]` (plus
 *   `message.send` receipt on 1:1 sends) instead of transient
 *   `article[data-delivery="sent"]` so fast hardware cannot replace the
 *   optimistic row before the selector attaches.
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
  OWNER,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

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

async function historyLoaded(page) {
  await waitFor(async () => {
    const text = await page.locator('.chat-workspace__history-state').innerText({ timeout: 2_000 }).catch(() => 'loading');
    return !text.includes('正在加载');
  }, 'group history loaded', 20_000);
}

test('real-daemon group chat recovers dropped conversations.post answer without auto-resend', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli group-daemon-post-drop', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-group-s6-g6-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        dropMethod: 'conversations.post',
      });
      const stamp = `${process.pid}-${Date.now()}`;
      const nameA = await createAgent(daemon.upstreamPath, `s6-g6a-${stamp}`);
      const nameB = await createAgent(daemon.upstreamPath, `s6-g6b-${stamp}`);
      const rowA = await agentRow(daemon.upstreamPath, nameA);
      const rowB = await agentRow(daemon.upstreamPath, nameB);
      const created = await ipcCall(daemon.upstreamPath, 'conversations.create', {
        from: `user:${OWNER}`,
        title: 'G6 群',
        members: [rowA.agentId, rowB.agentId],
        idempotencyKey: nextKey('g6-create'),
      });
      const groupId = created.conversationId;

      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await openTargetChat(page, nameB);
      await groupRow(page, groupId).waitFor();
      await groupRow(page, groupId).click();
      await historyLoaded(page);

      const lostText = `s6-g6-lost-${stamp}`;
      const postIdx = net.posts.length;
      await sendFromComposer(page, lostText);
      const unknownNotice = page.getByText('发送结果未知；请在上方消息里确认是否已发出。');
      await unknownNotice.waitFor({ timeout: 20_000 });

      assert.equal(await page.locator('.chat-composer__input').inputValue(), lostText);
      assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true);
      const actionLabels = await page.locator('.chat-composer__actions button').allInnerTexts();
      assert.equal(actionLabels.some((label) => /重试|重发/.test(label)), false);

      await delay(4_000);
      assert.equal(net.posts.slice(postIdx).filter((entry) => entry.method === 'conversations.post').length, 1);

      assert.equal(daemon.proxy.dropped.length, 1);
      assert.equal(daemon.proxy.dropped[0].method, 'conversations.post');
      assert.equal(daemon.proxy.dropped[0].response?.result?.ok, true);

      await waitFor(
        async () => (await page.locator('article.chat-workspace__message', { hasText: lostText }).count()) >= 1,
        'G6 poll brings persisted message into history',
        12_000,
      );
      assert.equal(await page.locator('article.chat-workspace__message', { hasText: lostText }).count(), 1);
      const ipcHistory = await ipcCall(daemon.upstreamPath, 'conversations.history', {
        from: `user:${OWNER}`,
        conversationId: groupId,
        mode: 'tail',
      });
      assert.equal(ipcHistory.entries.filter((entry) => entry.message === lostText).length, 1);

      await page.locator(`button.chat-workspace__target[data-target-id="${rowB.uri}"]`).click();
      const oneToOneText = `s6-g6-1to1-${stamp}`;
      const oneToOnePostIdx = net.posts.length;
      const oneToOneReceiptIdx = net.receipts.length;
      await sendFromComposer(page, oneToOneText);
      await page.locator('article[data-author="user"]', { hasText: oneToOneText }).first().waitFor({ timeout: 15_000 });
      await waitForReceipt(
        net,
        oneToOneReceiptIdx,
        'message.send',
        (entry) => entry.body?.ok === true,
        'G6 1:1 message.send receipt',
      );
      assert.equal(net.posts.slice(oneToOnePostIdx).filter((entry) => entry.method === 'message.send').length, 1);

      await groupRow(page, groupId).click();
      await historyLoaded(page);
      await unknownNotice.waitFor({ timeout: 10_000 });
      await page.getByRole('button', { name: '关闭提示' }).click();
      assert.equal(await unknownNotice.count(), 0);
      assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isEnabled(), true);

      await page.reload({ waitUntil: 'networkidle' });
      await page.locator(CONNECTED_READY).waitFor();
      await page.getByRole('button', { name: /^聊天/ }).click();
      await page.getByTestId('chat-layout').waitFor();
      await groupRow(page, groupId).waitFor();
      await groupRow(page, groupId).click();
      await historyLoaded(page);
      assert.equal(await page.locator('article.chat-workspace__message', { hasText: lostText }).count(), 1);
      assert.equal(await unknownNotice.count(), 0);
      assert.equal(await page.locator('.chat-composer__input').inputValue(), '');
      assert.equal(await page.locator('.chat-composer__input').isEnabled(), true);
      assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true);

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli group-daemon-post-drop groupId=${groupId}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close().catch(() => undefined);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('real-daemon UI group create, members panel add/remove, and real AGENT_NOT_FOUND rejection', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli group-daemon-create-members', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-group-s6-admin-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      const stamp = `${process.pid}-${Date.now()}`;
      const names = {};
      const rows = {};
      for (const label of ['a', 'b', 'c', 'd']) {
        names[label] = await createAgent(daemon.socketPath, `s6-gm-${label}-${stamp}`);
        rows[label] = await agentRow(daemon.socketPath, names[label]);
      }
      const existingGroup = await ipcCall(daemon.socketPath, 'conversations.create', {
        from: `user:${OWNER}`,
        title: 'G8 群',
        members: [rows.a.agentId, rows.b.agentId],
        idempotencyKey: nextKey('g8-create'),
      });
      const existingGroupId = existingGroup.conversationId;

      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await groupSection(page).waitFor();
      const syncAgentReceiptIdx = net.receipts.length;
      await waitForReceipt(
        net,
        syncAgentReceiptIdx,
        'agent.list',
        (entry) => entry.body?.ok === true,
        'agent.list settle before opening create dialog',
      );

      const dialogOpenIdx = net.posts.length;
      await page.getByRole('button', { name: '新建群聊' }).click();
      await page.getByRole('dialog').filter({ hasText: '新建群聊' }).waitFor();
      await delay(1_200);
      assert.deepEqual(net.posts.slice(dialogOpenIdx), []);

      const optionNames = await page.locator('.group-create-dialog__member-option .group-create-dialog__member-name').allInnerTexts();
      const optionIds = await page.locator('.group-create-dialog__member-option .group-create-dialog__member-id').allInnerTexts();
      assert.deepEqual(
        optionNames.map((text) => text.trim()).sort(),
        [names.a, names.b, names.c, names.d].sort(),
      );
      assert.equal(optionIds.every((text) => text.trim().startsWith('a.')), true);

      const newTitle = `G7 新群 ${stamp}`;
      await page.getByLabel('群聊名称').fill(newTitle);
      await page.locator('.group-create-dialog__member-option', { hasText: names.c }).locator('input[type="checkbox"]').check();
      const submitIdx = net.posts.length;
      const submitReceiptIdx = net.receipts.length;
      await page.getByRole('button', { name: '创建', exact: true }).click();
      await waitFor(async () => (await page.getByRole('dialog').count()) === 0, 'create dialog closed', 20_000);
      const createReceipt = await waitForReceipt(
        net,
        submitReceiptIdx,
        'conversations.create',
        (entry) => entry.body?.ok === true,
        'G7 conversations.create receipt',
      );
      const newGroupId = createReceipt.body.result.conversationId;
      await delay(2_000);
      const createPosts = net.posts.slice(submitIdx).filter((entry) => entry.method === 'conversations.create');
      const postSubmitHistories = net.posts.slice(submitIdx).filter((entry) => entry.method === 'conversations.history');
      assert.equal(createPosts.length, 1);
      assert.deepEqual(createPosts[0].params.members, [rows.c.agentId]);
      assert.equal(postSubmitHistories.length, 0);

      const firstGroupRow = page.locator('.chat-workspace__target-group[aria-label="群聊"] .chat-workspace__target').first();
      assert.equal((await firstGroupRow.locator('strong').innerText()).trim(), newTitle);
      assert.equal(await firstGroupRow.getAttribute('data-highlighted'), 'true');

      await groupRow(page, newGroupId).click();
      await historyLoaded(page);
      const g7MsgText = `s6-g7-first-${stamp}`;
      const g7ReceiptIdx = net.receipts.length;
      await sendFromComposer(page, g7MsgText);
      await page.locator('article[data-author="user"]', { hasText: g7MsgText }).first().waitFor({ timeout: 15_000 });
      const g7PostReceipt = await waitForReceipt(
        net,
        g7ReceiptIdx,
        'conversations.post',
        (entry) => entry.body?.ok === true,
        'G7 first conversations.post receipt',
      );
      assert.equal(g7PostReceipt.body.result.ok, true);
      const newGroupDetail = await ipcCall(daemon.socketPath, 'conversations.get', {
        from: `user:${OWNER}`,
        conversationId: newGroupId,
      });
      assert.deepEqual(
        newGroupDetail.members.map((member) => member.memberId).sort(),
        [rows.c.agentId, `user:${OWNER}`].sort(),
      );

      await groupRow(page, existingGroupId).click();
      await historyLoaded(page);
      const memberIdx = net.posts.length;
      await page.getByRole('button', { name: '成员', exact: true }).click();
      await page.locator('.group-members-panel').waitFor();
      await page.locator('.group-members-panel__row').first().waitFor();
      assert.equal(net.posts.slice(memberIdx).filter((entry) => entry.method === 'conversations.get').length, 1);
      assert.equal(await page.locator('.group-members-panel__row').count(), 3);
      const ownerPanelRow = page.locator(`.group-members-panel__row[data-member-id="user:${OWNER}"]`);
      assert.ok((await ownerPanelRow.locator('.group-members-panel__kind').innerText()).includes('所有者'));
      assert.equal(await ownerPanelRow.locator('button[aria-label^="移除"]').count(), 0);
      assert.equal(
        await page.locator(`.group-members-panel__row[data-member-id="${rows.a.agentId}"] button[aria-label^="移除"]`).count(),
        1,
      );

      await page.getByLabel('添加成员').click();
      await page.getByRole('option', { name: names.c, exact: true }).click();
      const addIdx = net.posts.length;
      await page.getByRole('button', { name: '添加', exact: true }).click();
      await page.locator(`.group-members-panel__row[data-member-id="${rows.c.agentId}"]`).waitFor({ timeout: 15_000 });
      await delay(500);
      const addPosts = net.posts.slice(addIdx).filter((entry) => entry.method === 'conversations.members.add');
      assert.equal(addPosts.length, 1);
      assert.equal(addPosts[0].params.member, rows.c.agentId);
      assert.equal(net.posts.slice(addIdx).filter((entry) => entry.method === 'conversations.get').length, 0);
      assert.equal(await page.locator('.group-members-panel__row').count(), 4);

      await page.locator(`.group-members-panel__row[data-member-id="${rows.c.agentId}"] button[aria-label^="移除"]`).click();
      await page.getByRole('dialog').filter({ hasText: '移除成员？' }).waitFor();
      const removeIdx = net.posts.length;
      await page.getByRole('button', { name: '确认移除' }).click();
      await waitFor(
        async () => (await page.locator(`.group-members-panel__row[data-member-id="${rows.c.agentId}"]`).count()) === 0,
        'member row removed',
        15_000,
      );
      await delay(500);
      const removePosts = net.posts.slice(removeIdx).filter((entry) => entry.method === 'conversations.members.remove');
      assert.equal(removePosts.length, 1);
      assert.equal(removePosts[0].params.memberId, rows.c.agentId);
      assert.equal(net.posts.slice(removeIdx).filter((entry) => entry.method === 'conversations.get').length, 0);
      assert.equal(await page.locator('.group-members-panel__row').count(), 3);
      const detailAfterRemove = await ipcCall(daemon.socketPath, 'conversations.get', {
        from: `user:${OWNER}`,
        conversationId: existingGroupId,
      });
      assert.deepEqual(
        detailAfterRemove.members.map((member) => member.memberId).sort(),
        [rows.a.agentId, rows.b.agentId, `user:${OWNER}`].sort(),
      );

      let rejectionVerified = false;
      for (let attempt = 0; attempt < 3 && !rejectionVerified; attempt += 1) {
        const candidateName = attempt === 0
          ? names.d
          : await createAgent(daemon.socketPath, `s6-gm-retry-${stamp}-${attempt}`);
        await page.getByLabel('添加成员').click();
        await page.getByRole('option', { name: candidateName, exact: true }).waitFor({ timeout: 10_000 });
        await page.getByRole('option', { name: candidateName, exact: true }).click();
        const preview = await ipcCall(daemon.socketPath, 'agent.destroy.preview', { name: candidateName });
        const destroyed = await ipcCall(daemon.socketPath, 'agent.destroy', { operationId: preview.operationId });
        assert.equal(destroyed?.destroyed, true);

        const rejectPostIdx = net.posts.length;
        const rejectReceiptIdx = net.receipts.length;
        await page.getByRole('button', { name: '添加', exact: true }).click();
        await delay(1_500);
        if (net.posts.slice(rejectPostIdx).filter((entry) => entry.method === 'conversations.members.add').length === 0) {
          continue;
        }
        const rejectReceipt = await waitForReceipt(
          net,
          rejectReceiptIdx,
          'conversations.members.add',
          () => true,
          'AGENT_NOT_FOUND rejection receipt',
        );
        const codeSeen = rejectReceipt.body?.error?.code ?? rejectReceipt.body?.code;
        assert.equal(codeSeen, 'AGENT_NOT_FOUND');
        const noticeText = await page.locator('.group-members-panel__add .ui-notice--danger').innerText();
        assert.ok(noticeText.includes('找不到这个 Agent'), `expected AGENT_NOT_FOUND copy, got ${noticeText}`);
        rejectionVerified = true;
      }
      assert.equal(rejectionVerified, true, 'must trigger and verify real AGENT_NOT_FOUND rejection within 3 attempts');

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli group-daemon-create-members newGroupId=${newGroupId}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('real-daemon group create recovers dropped conversations.create answer with identical idempotencyKey', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('actual-cli group-daemon-create-drop', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-group-s6-g9-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        dropMethod: 'conversations.create',
      });
      const stamp = `${process.pid}-${Date.now()}`;
      const nameA = await createAgent(daemon.upstreamPath, `s6-g9a-${stamp}`);

      gui = await startActualGui({ root, driver: 'daemon', ttlMs: 300_000 });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const net = attachRecorder(page, gui.origin);

      await openChatApp(page, gui.handoffPath, gui.launchUrl);
      await groupSection(page).waitFor();
      await page.getByRole('button', { name: '新建群聊' }).click();
      await page.getByRole('dialog').filter({ hasText: '新建群聊' }).waitFor();

      const title = `G9 群 ${stamp}`;
      await page.getByLabel('群聊名称').fill(title);
      await page.locator('.group-create-dialog__member-option', { hasText: nameA }).locator('input[type="checkbox"]').check();

      const firstIdx = net.posts.length;
      await page.getByRole('button', { name: '创建', exact: true }).click();
      const unknownNotice = page.locator('.ui-dialog .ui-notice--warning').filter({ hasText: '结果未知。' });
      await unknownNotice.waitFor({ timeout: 15_000 });
      const retryButton = unknownNotice.getByRole('button', { name: '重试' });
      assert.equal(await retryButton.count(), 1);
      const firstCreates = net.posts.slice(firstIdx).filter((entry) => entry.method === 'conversations.create');
      assert.equal(firstCreates.length, 1);

      const retryIdx = net.posts.length;
      const retryReceiptIdx = net.receipts.length;
      await retryButton.click();
      await waitFor(async () => (await page.getByRole('dialog').count()) === 0, 'create dialog closed after retry', 20_000);
      const retryCreates = net.posts.slice(retryIdx).filter((entry) => entry.method === 'conversations.create');
      assert.equal(retryCreates.length, 1);
      assert.equal(retryCreates[0].params.idempotencyKey, firstCreates[0].params.idempotencyKey);

      const retryReceipt = await waitForReceipt(
        net,
        retryReceiptIdx,
        'conversations.create',
        (entry) => entry.body?.ok === true,
        'G9 retry conversations.create receipt',
      );
      assert.equal(daemon.proxy.dropped.length, 1);
      assert.equal(daemon.proxy.dropped[0].method, 'conversations.create');
      assert.equal(
        daemon.proxy.dropped[0].response?.result?.conversationId,
        retryReceipt.body.result.conversationId,
      );

      const groupsList = await ipcCall(daemon.upstreamPath, 'conversations.list', {
        from: `user:${OWNER}`,
        kinds: ['group'],
      });
      const matches = (groupsList.conversations ?? []).filter((entry) => entry.kind === 'group' && entry.title === title);
      assert.equal(matches.length, 1);

      assert.deepEqual(net.pageErrors, []);
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(`PASS actual-cli group-daemon-create-drop groupId=${retryReceipt.body.result.conversationId}\n`);
    } finally {
      if (browser !== null) await browser.close().catch(() => undefined);
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close().catch(() => undefined);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
