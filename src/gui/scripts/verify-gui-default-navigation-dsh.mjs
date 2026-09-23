// Read-only native business navigation. No model prompts or business actions.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { writeFile } from 'node:fs/promises';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const endpoint = new URL(process.env.GUI_NAV_DSH_URL || 'http://127.0.0.1:3198');
assert.ok(['127.0.0.1', 'localhost'].includes(endpoint.hostname));
if (endpoint.port === '3080') assert.equal(process.env.GUI_NAV_LOCAL_AUTHORIZED, '1');
const { chromium } = createRequire(resolve(process.env.DSH_GUI_BROWSER_DEPS || root + '/browser-tests/package.json'))('playwright');
const browser = await chromium.launch();
const results = [], errors = [];
let candidateLoads = 0;
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', error => errors.push(error.message));
  if (process.env.GUI_NAV_CANDIDATE === '1') {
    await page.route(url => url.pathname === '/plugins/@hyprial/dsh-hyprial-plugin/client.js', route => {
      candidateLoads++;
      return route.fulfill({ contentType: 'text/javascript', path: root + '/static/client.js' });
    });
  }
  await page.goto(endpoint.origin + '/?gui=default');
  await page.locator('[data-gui-action=studio]').waitFor();
  for (let i = 0; i < 10; i++) {
    for (const name of [/^(Continue|继续)$/, /^(Configure later|稍后配置)$/]) {
      const button = page.getByRole('button', { name });
      if (await button.isVisible().catch(() => false)) await button.click();
    }
    await page.waitForTimeout(150);
  }
  if (process.env.GUI_NAV_CANDIDATE === '1') assert.equal(candidateLoads, 1, 'Candidate bundle must actually load');
  async function profile() {
    return page.evaluate(async () => {
      const response = await fetch('/plugins/h2b-talk/rpc', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ method: 'h2b-gui-studio', args: { operation: 'profile' } }) });
      const body = await response.json(); if (!body.ok) throw new Error('Profile query failed'); return body.value;
    });
  }
  const before = await profile();
  async function check(label, feature, heading) {
    const id = 'native:' + feature + (feature === 'h2b.contacts' ? ':detail' : '');
    const surface = page.locator('[data-gui-module-instance="' + id + '"]');
    await surface.waitFor({ state: 'visible' });
    if (heading) await surface.locator('.h2bcontrol-title').filter({ hasText: heading }).waitFor();
    const seat = await page.locator('[data-gui-module-placeholder="' + id + '"]').boundingBox();
    const box = await surface.boundingBox();
    assert.ok(seat?.height > 200 && box?.height > 200, label + ' needs visible content height');
    assert.equal(Math.round(seat.height), Math.round(box.height));
    if (feature === 'h2b.contacts') assert.equal(await surface.locator('.gui-contact-library').count(), 0, 'Native contact details must not duplicate the sidebar directory');
    results.push({ label, feature, height: box.height });
  }
  await page.getByRole('button', { name: '任务', exact: true }).click();
  await check('任务看板', 'h2b.kanban');
  for (const [label, feature] of [['Workflow', 'h2b.workflow'], ['Routine', 'h2b.routine'], ['任务看板', 'h2b.kanban']]) {
    await page.locator('.h2bapps-menu-btn').filter({ hasText: label }).click();
    await check(label, feature);
  }
  await page.getByRole('button', { name: '运维', exact: true }).click();
  for (const label of ['总览', 'Agent 网络', '投递', '日志', '集成', '系统与组织']) {
    await page.locator('.h2bapps-menu-btn').filter({ hasText: label }).click();
    await check(label, 'h2b.operations', label);
  }
  await page.getByRole('button', { name: '通讯录', exact: true }).click();
  await check('通讯录详情', 'h2b.contacts');
  await page.getByRole('button', { name: '消息', exact: true }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('[data-gui-module-instance]')].every(node => node.hidden));
  assert.deepEqual(await profile(), before);
  assert.deepEqual(errors, []);
  const report = { status: 'passed', candidateLoads, results, returnedToMessages: true, profileUnchanged: true, errors };
  await writeFile(process.env.GUI_NAV_REPORT || '/tmp/gui-default-navigation.json', JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally { await browser.close(); }
