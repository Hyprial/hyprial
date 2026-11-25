/**
 * Contract Mock browser contract: the org page's org.list primary read and the explicit org.fetch sync.
 *
 * Run: node --test tests/modules/dev04/org-request-browser.mjs
 * Env: HYPRIAL_BROWSER_EXECUTABLE (optional) points at an installed Chromium/Chrome.
 *
 * Decision record (1e-3, 2026-11-15)
 * - Data comes from the Contract Mock through tests/support/contract-mock-gui.mjs and the product
 *   entry; a state is selected by a declared example name (`cases`), never by a canned browser
 *   reply. It proves HTTP admission, call counts and that the page does not invent success; it
 *   does not show a real mesh result. The product bootstrap keeps one per-view session proof in
 *   sessionStorage (the only key allowed) and nothing in localStorage.
 * - The earlier version opened the reference org entry, which stays on "账号状态未确认" and never
 *   issues org.list, so all six of its tests failed on the base and no workflow ran the file.
 *   Three of them are reachable here. The other three were deleted with it because no declared
 *   example produces their state; a daemon example (or a host fault) must exist first:
 *   - folder restore keeps a partial result: orgfs.trash/orgfs.restore examples that answer per
 *     node (one confirmed, one ORGFS_RESTORE_DENIED);
 *   - recovery after a first org.list failure: a failure followed by a success, which the
 *     stateless mock has no way to express (the failure and refresh half is covered below);
 *   - damaged org.fetch answers (HTTP 502 and 401 with a text body, expecting ENVELOPE_NOT_JSON
 *     and HTTP_UNAUTHORIZED): non-envelope replies, which the mock never sends;
 *   - a not-dispatched org.fetch failure: a domain-error example whose envelope says
 *     not-dispatched (the mock marks a declared domain error as dispatched; the plain-failure
 *     rendering of the code is covered below).
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright-core';
import { openContractMockView, startContractMock } from '../../support/contract-mock-gui.mjs';
import { browserExecutable } from '../../support/browser.mjs';

const WRITE_METHODS = new Set([
  'org.create',
  'org.delete',
  'org.fetch',
  'org.invite',
  'org.join',
  'org.leave',
  'org.publish',
  'org.remove',
  'org.repair',
]);
let browser;

before(async () => {
  browser = await chromium.launch({ executablePath: browserExecutable(), headless: true });
});

after(async () => {
  await browser?.close();
});

async function openOrgPage(selection, run) {
  const mock = await startContractMock(selection);
  const view = await openContractMockView(mock);
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const requests = [];
  const problems = [];
  page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`));
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/daemon/')) {
      requests.push({ path, method: request.method(), body: request.postData() });
    }
  });
  try {
    await page.goto(view.launchUrl());
    await page.getByRole('button', { name: '组织', exact: true }).click();
    return await run({ page, requests, problems });
  } finally {
    await page.close();
    await view.close();
    await mock.close();
  }
}

function orgMethods(requests) {
  return requests
    .filter((item) => item.path.includes('/daemon/request/org.'))
    .map((item) => item.path.split('/').pop());
}

async function openVersionSettings(page) {
  await page.getByRole('heading', { name: 'hyprial-dev', exact: true }).waitFor();
  const orgTabs = page.getByRole('tablist', { name: '组织子页签' });
  await orgTabs.getByRole('tab', { name: /^设置/ }).click();
  await page.getByText('设备数').waitFor();
  const settingsTabs = page.getByRole('tablist', { name: '设置分组' });
  await settingsTabs.getByRole('tab', { name: '版本' }).click();
}

test('org page reads org.list and only org.fetch on the explicit sync action', async () => {
  await openOrgPage({ scenario: 'success' }, async ({ page, requests, problems }) => {
    await openVersionSettings(page);
    const fetchResponse = page.waitForResponse((response) =>
      response.url().endsWith('/daemon/request/org.fetch'),
    );
    await page.getByRole('button', { name: '同步组织配置' }).click();
    const fetched = await (await fetchResponse).json();
    assert.equal(fetched.ok, true);
    assert.equal(fetched.dispatch, 'dispatched');
    await page.getByText('请求 2 个来源 · 应答 2 · 接收 1 · 拒绝 1').waitFor();
    await page.getByText('版本 7 · user:lin · 来源 node-2').waitFor();
    assert.equal(await page.getByText(/候选采信/).count(), 0, '没有采信入口，也没有采信说明');

    const orgCalls = requests.filter((item) => item.path.includes('/daemon/request/org.'));
    assert.ok(orgCalls.some((item) => item.path.endsWith('/org.list')), 'the page reads org.list first');
    assert.deepEqual(
      [
        ...new Set(
          orgCalls
            .map((item) => item.path.split('/').pop())
            .filter((method) => WRITE_METHODS.has(method)),
        ),
      ].sort(),
      ['org.fetch'],
      'only the explicit sync action writes',
    );
    assert.equal(
      orgCalls.filter((item) => item.path.endsWith('/org.fetch')).length,
      1,
      'the write is sent once and never retried',
    );
    const fetchCall = orgCalls.find((item) => item.path.endsWith('/org.fetch'));
    assert.deepEqual(JSON.parse(fetchCall.body), {
      timeoutSeconds: 2,
      requestMode: 'neighbors',
    });
    assert.ok(
      requests.every((item) => item.path === '/daemon/session' || item.path.startsWith('/daemon/request')),
      'no second REST surface',
    );
    const storage = await page.evaluate(() => ({
      local: localStorage.length,
      session: Object.keys(sessionStorage),
    }));
    assert.deepEqual(storage, { local: 0, session: ['hyprial.gui.session-view'] });
    assert.deepEqual(problems, []);
  });
});

test('first org.list failure shows the reason and offers a page refresh that reads org.list again', async () => {
  await openOrgPage({ cases: { 'org.list': 'network-fallback' } }, async ({ page, requests }) => {
    await page.getByText('组织列表读取失败').waitFor();
    await page.getByText('组织网络请求失败。').waitFor();
    const refresh = page.getByRole('button', { name: '刷新', exact: true });
    assert.equal(await refresh.count(), 1);
    const reads = () => orgMethods(requests).filter((method) => method === 'org.list').length;
    const before = reads();
    await refresh.click();
    await page.getByText('组织列表读取失败').waitFor();
    await page.waitForTimeout(300);
    assert.equal(reads(), before + 1, 'the refresh reads org.list exactly once more');
    assert.equal(orgMethods(requests).includes('org.fetch'), false);
  });
});

test('an org.fetch domain failure stays a plain failure with its code and is not retried', async () => {
  await openOrgPage({ cases: { 'org.fetch': 'source-unreachable' } }, async ({ page, requests }) => {
    await openVersionSettings(page);
    await page.getByRole('button', { name: '同步组织配置', exact: true }).click();
    await page.getByText(/同步未完成：选定的来源不可达。/).waitFor();
    await page.getByText(/ORG_SOURCE_UNREACHABLE/).waitFor();
    assert.equal(await page.getByText(/结果未知/).count(), 0);
    assert.equal(orgMethods(requests).filter((method) => method === 'org.fetch').length, 1);
    await page.waitForTimeout(300);
    assert.equal(orgMethods(requests).filter((method) => method === 'org.fetch').length, 1);
  });
});
