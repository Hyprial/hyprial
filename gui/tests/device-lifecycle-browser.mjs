/* Shared device-page lifecycle in explicit frontend preview; no daemon or issuer proof. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright-core';
import { browserExecutable } from './support/browser.mjs';
const origin = process.argv[2] ?? 'http://127.0.0.1:4180';
const output = process.argv[3] ?? '/tmp/gui-device-browser';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname));
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: browserExecutable(), headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [], failed = [], external = [], daemonRequests = [], screenshots = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('requestfailed', request => failed.push(new URL(request.url()).pathname));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.protocol.startsWith('http') && url.origin !== origin) external.push(url.origin);
    if (url.pathname.includes('/daemon/request/')) daemonRequests.push(url.pathname);
  });
  const settings = async () => {
    await page.getByRole('button', { name: '我的账号与 Agent 设置菜单' }).click();
    await page.getByRole('menuitem', { name: '用户设置', exact: true }).click();
  };
  const device = () => page.getByRole('button', { name: '设备', exact: true }).click();
  await page.goto(`${origin}/?hyprialScenario=j1=logged-out-init&hyprialFirstRun=false`);
  await settings();
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).waitFor();
  for (const name of ['Agent', '聊天', '组织']) {
    await page.getByRole('button', { name, exact: true }).click();
    await page.getByText('请先登录', { exact: true }).waitFor();
    assert.deepEqual(await page.locator('.content button').allTextContents(), ['去登录']);
  }
  await page.getByRole('button', { name: '我的账号与 Agent 设置菜单' }).click();
  await page.getByRole('menuitem', { name: 'Agent 设置', exact: true }).click();
  await page.getByText('请先登录', { exact: true }).waitFor();
  await page.screenshot({ path: `${output}/protected-page.png` });
  await page.getByRole('button', { name: '开始', exact: true }).click();
  assert.equal(await page.locator('.start-bento .recommendation-card').count(), 1);
  assert.equal(await page.locator('.start-bento .start-status').count(), 1);
  await page.screenshot({ path: `${output}/start-logged-out.png` });
  await device();
  await page.getByRole('button', { name: '初始化当前设备', exact: true }).click();
  await page.getByText('请先登录', { exact: true }).waitFor();
  await settings();
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
  await page.getByRole('button', { name: '退出本机登录', exact: true }).waitFor();
  await device();
  const initialize = page.getByRole('button', { name: '初始化当前设备', exact: true });
  await initialize.waitFor();
  assert.equal(await page.locator('[data-page="device"] button').count(), 1);
  await initialize.click();
  await page.getByText('preview-daemon', { exact: true }).waitFor();
  assert.equal(await page.getByRole('heading', { name: '网络状态' }).count(), 0);
  await page.getByText('暂无迁移记录', { exact: true }).waitFor();
  await page.getByText('本页没有可显示的安全事件摘要。', { exact: true }).waitFor();
  assert.equal(await page.getByText('Agent 观测条目', { exact: true }).locator('..').innerText(), 'Agent 观测条目\n0');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
    const file = `initialized-${width}.png`;
    await page.getByRole('heading', { name: '本机状态', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${output}/${file}` });
    await page.getByRole('heading', { name: '日志事件摘要', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${output}/readouts-${width}.png` });
    screenshots.push(`readouts-${width}.png`);
    screenshots.push(file);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await settings();
  await page.getByRole('button', { name: '退出本机登录', exact: true }).click();
  await page.getByRole('button', { name: '确认退出', exact: true }).click();
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).waitFor();
  await device();
  await page.getByRole('button', { name: '初始化当前设备', exact: true }).click();
  await page.getByText('请先登录', { exact: true }).waitFor();
  assert.equal(await page.getByText('preview-daemon', { exact: true }).count(), 0);
  await page.screenshot({ path: `${output}/logged-out.png`, fullPage: true });
  await settings();
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
  await page.getByRole('button', { name: '退出本机登录', exact: true }).waitFor();
  await device();
  await page.getByText('preview-daemon', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '初始化当前设备', exact: true }).count(), 0);
  assert.deepEqual(errors, []); assert.deepEqual(failed, []); assert.deepEqual(external, []); assert.deepEqual(daemonRequests, []);
  const result = { verdict: 'PASS', mode: 'frontend-preview', viewports: [1440, 390], screenshots,
    checks: ['signed-out business pages only offer login', 'original Start card layout retained', 'logged-out init prompts login', 'login then single init action', 'initialize then readouts', 'zero Agents and no invented log history', 'no hosts', 'logout removes data', 'same-account preview relogin retains initialized device', 'no overflow'],
    errors, failed, external, daemonRequests, backendAcceptance: 'NOT RUN by this script' };
  await writeFile(`${output}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
