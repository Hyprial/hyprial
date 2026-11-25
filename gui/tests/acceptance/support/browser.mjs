/**
 * Decision record (M1/PR 4b-C/S3/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - Shared Playwright browser launcher (`launchBrowser`), HTTP `probe`,
 *   chat layout capture (`captureChatLayout`), ordinary App navigation checks
 *   (`runBrowser`), and developer-tools / API-docs checks (`runDevToolsBrowser`).
 * - `runBrowser` verifies that the daemon empty directory renders `群聊` with
 *   zero `[data-target-id]` items, Contract Mock `success` renders the worker
 *   chat history, and the launch grant is single-use (`409 LAUNCH_GRANT_USED`).
 * - `runDevToolsBrowser` observes the full browser context (covering both the
 *   main tab and the second `/docs` tab) to assert no `Authorization` header
 *   and no bearer leakage.
 */

import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { chromium } from 'playwright-core';
import { browserExecutable } from '../../support/browser.mjs';
import { builtToolPaths } from './assertions.mjs';
import { CONNECTED_READY, delay, exists, waitFor } from './home.mjs';

export function launchBrowser() {
  return chromium.launch({
    executablePath: browserExecutable(),
    headless: true,
    args: ['--no-sandbox'],
  });
}

export async function probe(origin, path) {
  const response = await fetch(`${origin}${path}`, { redirect: 'manual' });
  return { status: response.status, body: await response.text() };
}

export async function captureChatLayout(page, name) {
  const layout = page.getByTestId('chat-layout');
  await layout.waitFor();
  await page.getByRole('heading', { name: '聊天对象' }).waitFor();
  await delay(300);
  const chatText = await page.locator('[data-page="chat"]').innerText();
  assert.equal(chatText.includes('READ_FAILED'), false);
  assert.equal(chatText.includes('历史会话目录读取失败'), false);
  const focus = await page.evaluate(() => {
    const active = document.activeElement;
    return {
      tag: active?.tagName ?? null,
      label: active?.getAttribute('aria-label') ?? active?.textContent?.trim().slice(0, 60) ?? null,
      insideChat: Boolean(active?.closest('[data-page="chat"]')),
    };
  });
  const directory = process.env.HYPRIAL_CHAT_EVIDENCE_DIR;
  if (directory) {
    await mkdir(directory, { recursive: true });
    await page.screenshot({ path: join(directory, `${name}-desktop.png`), fullPage: true });
    await page.setViewportSize({ width: 420, height: 900 });
    await delay(200);
    await page.screenshot({ path: join(directory, `${name}-narrow.png`), fullPage: true });
    await page.setViewportSize({ width: 1366, height: 900 });
    await delay(200);
  }
  return { focus, chatText };
}

export async function runBrowser(handoffPath, launchUrl, origin, secrets = [], driver = 'contract-mock', scenario = 'empty') {
  const browser = await launchBrowser();
  const requests = [];
  const receipts = [];
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      if (!request.url().startsWith('http')) return;
      requests.push({
        url: request.url(),
        path: new URL(request.url()).pathname,
        headers: request.headers(),
        body: request.postData() ?? '',
      });
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      receipts.push(response.json().then((body) => ({ path: url.pathname, body })).catch(() => null));
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    assert.equal(new URL(page.url()).hash, '');
    assert.equal(page.url().includes(launchUrl.split('#')[1]), false);
    await page.getByRole('button', { name: /^聊天/ }).click();
    const chat = await captureChatLayout(page, `ordinary-${driver}-${scenario}`);
    await delay(500);
    const chatReceipts = (await Promise.all(receipts)).filter(Boolean);
    const queryReceipts = chatReceipts.filter((entry) => entry.path === '/daemon/request/message.query');
    assert.equal(queryReceipts.length, 2, 'the page must read both message.query views');
    const queryRequests = requests.filter((entry) => entry.path === '/daemon/request/message.query');
    const requestedViews = queryRequests.map((entry) => {
      const body = JSON.parse(entry.body);
      assert.equal(typeof body === 'object' && body !== null && !Array.isArray(body), true);
      return body.view;
    });
    assert.deepEqual(requestedViews.slice().sort(), ['inbox', 'outbox'],
      'the page must ask for both inbox and outbox');
    assert.deepEqual(
      queryReceipts.map((entry) => entry.body.result.view).sort(),
      requestedViews.slice().sort(),
      'each answer must carry the view its request asked for',
    );
    for (const entry of queryReceipts) {
      assert.equal(entry.body.ok, true);
      assert.equal(entry.body.dispatch, 'dispatched');
    }
    let chatState;
    if (driver === 'contract-mock' && scenario === 'success') {
      await page.getByRole('button', { name: /worker/ }).click();
      await page.getByText('hello', { exact: true }).waitFor();
      chatState = 'selectable-history';
    } else if (driver === 'daemon') {
      const targets = page.locator('aside[aria-label="聊天对象"]');
      await targets.getByRole('group', { name: '群聊' }).waitFor();
      assert.equal(
        await targets.locator('[data-target-id]').count(),
        0,
        'the empty scenario must have zero selectable targets',
      );
      chatState = 'empty-directory';
    } else {
      const emptyState = page.getByRole('status').filter({ hasText: '暂无聊天对象' });
      await emptyState.first().waitFor();
      assert.equal(await emptyState.count(), 1,
        'the empty scenario must render exactly one explicit empty-directory state');
      chatState = 'empty-directory';
    }
    await delay(500);
    const resolvedReceipts = (await Promise.all(receipts)).filter(Boolean);
    assert.ok(resolvedReceipts.some((entry) => entry.path === '/daemon/request/message.query'));
    for (const entry of resolvedReceipts) {
      assert.match(String(entry.body.requestId), /^(http|mock)-/);
      assert.notEqual(entry.body.dispatch, 'unknown');
    }
    assert.ok(requests.some((entry) => entry.path === '/daemon/session'));
    assert.ok(requests.some((entry) => entry.path.startsWith('/daemon/request/')));
    assert.equal(requests.every((entry) => new URL(entry.url).origin === origin), true);
    assert.equal(requests.every((entry) => entry.url.includes('#grant=') === false), true);
    assert.equal(requests.every((entry) => (entry.headers.authorization ?? null) === null), true);
    const headers = JSON.stringify(requests.map((entry) => entry.headers));
    for (const secret of secrets) {
      assert.equal(headers.includes(secret), false);
      assert.equal(headers.includes(`Bearer ${secret}`), false);
    }
    assert.equal(headers.includes('Bearer '), false);
    const storage = await page.evaluate(() => ({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      cookie: document.cookie,
      text: document.body.textContent ?? '',
    }));
    assert.equal(storage.cookie, '');
    assert.equal(JSON.stringify(storage).includes('Bearer '), false);
    for (const secret of secrets) assert.equal(JSON.stringify(storage).includes(secret), false);
    assert.deepEqual(errors, []);
    const repeat = await fetch(`${origin}/daemon/session`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin },
      body: JSON.stringify({ grant: new URLSearchParams(new URL(launchUrl).hash.slice(1)).get('grant') }),
    });
    assert.equal(repeat.status, 409);
    assert.equal((await repeat.json()).code, 'LAUNCH_GRANT_USED');
    return {
      requestCount: requests.length,
      receiptCount: resolvedReceipts.length,
      title: await page.title(),
      path: new URL(page.url()).pathname,
      requestPaths: [...new Set(requests.map((entry) => entry.path))].sort(),
      chatBlocked: false,
      chatBlockedDetail: null,
      chatState,
      focus: chat.focus,
    };
  } finally {
    await browser.close();
  }
}

export async function runDevToolsBrowser(gui, { layer, apiDocs, live = false }) {
  const browser = await launchBrowser();
  const requests = [];
  const bodies = [];
  const errors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
    context.on('request', (request) => {
      if (!request.url().startsWith('http')) return;
      requests.push(request.allHeaders().catch(() => request.headers()).then((headers) => ({
        url: request.url(),
        path: new URL(request.url()).pathname,
        headers,
      })));
    });
    context.on('response', (response) => {
      const path = new URL(response.url()).pathname;
      bodies.push(response.text().then((text) => ({ path, text })).catch(() => ({ path, text: '' })));
    });
    const page = await context.newPage();
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(gui.launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(gui.handoffPath)), 'consumed handoff removal', 10_000);
    const shell = await probe(gui.origin, '/');
    assert.equal(shell.status, 200);
    const result = { layer, apiDocs, docsStatus: null, attachCalls: 0 };
    if (!layer) {
      await delay(1_500);
      assert.equal(await page.locator('[data-dev-tools]').count(), 0, 'no tool layer without the flag');
      assert.equal(await page.locator('#hyprial-dev-tools').count(), 0);
      for (const path of await builtToolPaths()) {
        const answer = await probe(gui.origin, path);
        assert.equal(answer.status, 404, `${path} must be 404 without the flag`);
        assert.equal(JSON.parse(answer.body).code, 'NOT_FOUND');
      }
      for (const path of ['/docs-assets/hyprial-docs.js', '/docs-assets/swagger-ui-bundle.js']) {
        assert.equal((await probe(gui.origin, path)).status, 404, path);
      }
      const docs = await probe(gui.origin, '/docs');
      assert.equal(docs.status, 200);
      assert.equal(docs.body, shell.body, '/docs is the app shell without the flag');
    } else {
      const mounted = page.locator('[data-dev-tools][data-dev-tools-source="backend"]');
      await mounted.waitFor({ state: 'attached' });
      assert.equal(await page.locator('[data-dev-tools]').count(), 1);
      await page.locator('[data-dev-tools-toggle]').click();
      await page.locator('[data-dev-tools-panel]').waitFor();
      await page.locator('.dev-module-index').waitFor();
      assert.ok(await page.locator('[data-dev-module]').count() > 0, 'the module index lists modules');
      assert.equal(await page.locator('[data-dev-tools-api-docs]').count(), apiDocs ? 1 : 0);
    }
    if (layer && !apiDocs) {
      const docs = await probe(gui.origin, '/docs');
      assert.equal(docs.body, shell.body, '/docs is the app shell when the host serves no API docs');
      assert.equal((await probe(gui.origin, '/docs-assets/hyprial-docs.js')).status, 404);
    }
    if (apiDocs) {
      const [docs] = await Promise.all([
        context.waitForEvent('page'),
        page.locator('[data-dev-tools-api-docs]').click(),
      ]);
      docs.setDefaultTimeout(30_000);
      docs.on('pageerror', (error) => errors.push(error.message));
      await docs.locator('.opblock').first().waitFor();
      assert.equal(new URL(docs.url()).origin, gui.origin);
      assert.equal(new URL(docs.url()).pathname, '/docs');
      assert.equal(await docs.locator('.btn.authorize').count(), 0, 'the docs page offers no credential entry');
      const operation = docs.locator('.opblock').filter({
        has: docs.locator('.opblock-summary-path[data-path="/daemon/request/agent.list"]'),
      });
      assert.equal(await operation.count(), 1, 'the projected schema lists agent.list once');
      await operation.locator('.opblock-summary').click();
      await operation.locator('.try-out__btn').click();
      const body = operation.locator('textarea.body-param__text');
      if (await body.count() > 0) await body.fill('{}');
      await operation.locator('.btn.execute').click();
      const status = operation.locator('.live-responses-table tr.response td.response-col_status');
      await status.first().waitFor();
      result.docsStatus = (await status.first().textContent()).trim().slice(0, 3);
      assert.equal(result.docsStatus, '200', 'one read method answers 200 through Try it out');
      const docsStorage = await docs.evaluate(() => JSON.stringify({
        local: Object.entries(localStorage),
        cookie: document.cookie,
        text: document.body.textContent ?? '',
      }));
      for (const secret of gui.secrets) assert.equal(docsStorage.includes(secret), false);
      const [next] = await Promise.all([
        page.waitForResponse((response) => new URL(response.url()).pathname.startsWith('/daemon/request/')),
        page.locator('[data-dev-tools-toggle]').click().then(() => page.getByRole('button', { name: /^聊天/ }).click()),
      ]);
      assert.equal(next.status(), 200, "the main tab's next call still succeeds");
      assert.equal((await next.json()).ok, true);
    }
    await delay(500);
    const seen = await Promise.all(requests);
    const answers = await Promise.all(bodies);
    const texts = answers.map((entry) => entry.text);
    const sessions = answers.filter((entry) => entry.path === '/daemon/session').map((entry) => JSON.parse(entry.text));
    assert.ok(sessions.length >= 1, 'the page opened a session');
    for (const session of sessions) {
      if (layer) assert.deepEqual(session.devTools, { apiDocs });
      else assert.equal(Object.hasOwn(session, 'devTools'), false, 'no devTools in the session body without the flag');
    }
    const toolRequests = seen.filter((entry) => entry.path.startsWith('/dev-tools/'));
    if (layer && live) {
      assert.ok(seen.some((entry) => entry.path.startsWith('/src/dev/tools/')), 'live source serves the layer as source');
    } else if (layer) assert.ok(toolRequests.length > 0, 'the layer is loaded from the gated prefix');
    else assert.deepEqual(toolRequests.map((entry) => entry.path), [], 'no request under /dev-tools/ without the flag');
    assert.equal(seen.every((entry) => new URL(entry.url).origin === gui.origin), true);
    assert.deepEqual(seen.filter((entry) => Object.hasOwn(entry.headers, 'authorization')).map((entry) => entry.path), []);
    const headerText = JSON.stringify(seen.map((entry) => entry.headers));
    assert.equal(headerText.includes('Bearer '), false);
    for (const secret of gui.secrets) {
      assert.equal(headerText.includes(secret), false);
      assert.equal(texts.some((text) => text.includes(secret)), false, 'the bearer occurs in no response body');
    }
    result.attachCalls = seen.filter((entry) => entry.path === '/daemon/session/attach').length;
    if (apiDocs) assert.ok(result.attachCalls >= 2, 'the docs page attaches for the schema and for the call');
    else assert.equal(result.attachCalls, 0);
    assert.deepEqual(errors, []);
    result.requestCount = seen.length;
    result.toolRequests = toolRequests.length;
    return result;
  } finally {
    await browser.close();
  }
}
