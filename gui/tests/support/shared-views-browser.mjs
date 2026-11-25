/** Browser entry shared by the retired page-specific feature contracts (WP6).
 *
 * Decisions recorded here (docs/design/gui-dev-product-parity-2026-11-09.md §4.1, §5 WP6):
 * - The fixture phases run the SAME journeys on both entries under the same
 *   `hyprialScenario` input (development composition and the `dist-parity`
 *   artifact). Org creation is honestly refused by the fixture org source
 *   (组织创建未完成 in the dialog, or the 创建暂不可用。 notification); the journey asserts that refusal.
 * - The Lark channel journey is gone with the channel page it exercised; the
 *   connections page mounts only the credential and platform integration
 *   sections.
 * - Unit suites retain failure/retry, invitation validation, permissions and
 *   real-controller coverage; this file keeps the cross-page browser journeys,
 *   the no-pageerror / no-business-write / no-storage invariants and evidence
 *   screenshots.
 */
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { browserExecutable } from './browser.mjs';
import {
  openParityPage,
  parityUrl,
  startParityProductionServer,
  waitForVisible,
} from './dev-parity-browser.mjs';

const button = (page, name) => page.getByRole('button', { name, exact: true });
async function orgTab(page, name) {
  const tab = page.getByRole('tab', { name, exact: true });
  if (await tab.isVisible()) await tab.click();
  else {
    await page.locator('#org-sub-narrow').click();
    await page.getByRole('option', { name, exact: true }).click();
  }
}
const SCENARIO_NO_TARGET_HINT = 'the linked-session baseline seeds no chat target, so this journey cannot run; owner: gui/src/dev/session/initial-session.ts (WP3)';
async function checkChat(page) {
  await button(page, '聊天').click();
  await page.locator('.chat-workspace').waitFor();
  assert.equal(await page.locator('.chat-workspace').count(), 1);
  const noTarget = await page.getByText('暂无聊天对象').first().isVisible().catch(() => false);
  assert.equal(
    noTarget,
    false,
    `chat journey requires a chat target: ${SCENARIO_NO_TARGET_HINT}`,
  );
  const input = page.getByRole('textbox', { name: '给 Squire 的消息' });
  await input.fill('共享页面草稿 <script>');
  await button(page, '信息').click();
  await button(page, '信息').click();
  assert.equal(await input.inputValue(), '共享页面草稿 <script>');
  await button(page, '开始').click();
  await button(page, '聊天').click();
  assert.equal(await input.inputValue(), '共享页面草稿 <script>');
  assert.equal(await page.locator('.chat-workspace script').count(), 0);
}
async function checkOrg(page, feature) {
  await button(page, '组织').click();
  await page.locator('.org-workspace').waitFor();
  await page.getByRole('button', { name: /^组织切换器/ }).click();
  await page.getByRole('menuitem', { name: '新建组织' }).click();
  const dialog = page.getByRole('dialog', { name: '新建组织' });
  await dialog.getByLabel(/^组织名称/).fill('shared-preview');
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByText(/组织创建未完成|创建暂不可用。/).first().waitFor({ timeout: 5_000 });
  await page.keyboard.press('Escape');
  if (feature === 'j5' || feature === 'linked') {
    await orgTab(page, '成员');
    await page.locator('[data-org-section="members"], .org-members').first().waitFor({ timeout: 5_000 });
  }
  if (feature === 'j6' || feature === 'linked') {
    await orgTab(page, '机器');
    await page.locator('[data-org-section="machines"]').waitFor();
    assert.equal(await page.locator('.org-machines [data-machine-id]').count(), 2);
  }
  if (feature === 'j7' || feature === 'linked') {
    await orgTab(page, '拓扑');
    await page.locator('[data-org-section="topology"]').waitFor();
    assert.equal(await page.locator('.org-topology').getByRole('button', { name: '通信视图' }).count(), 0, 'no communication edges: no communication toggle');
    await page.locator('.org-topology').getByRole('button', { name: 'Monitor' }).click();
    await page.getByRole('dialog', { name: '最近通信记录' }).waitFor();
    await button(page, '关闭 Monitor').click();
  }
}

function scenarioFor(feature) {
  if (feature === 'j6') return 'j5=m11;j6=m14';
  if (feature === 'j7') return 'j5=m11;j7=m17';
  if (feature === 'linked') return 'j5=m11;j6=m14;j7=m17';
  if (feature === 'j5') return 'j5=m11';
  return 'default';
}

export async function verifySharedViews(feature) {
  const guiDir = resolve(import.meta.dirname, '../..');
  process.env.HYPRIAL_GUI_HOME = guiDir;
  process.env.HYPRIAL_BACKEND_ENDPOINT = '';
  delete process.env.HYPRIAL_GUI_AUTH;
  delete process.env.HYPRIAL_GUI_SETUP;
  const { startGuiServer } = await import('../../scripts/serve.mjs');
  const previousDevPreview = process.env.HYPRIAL_GUI_DEV_PREVIEW;
  process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';
  let devServer;
  try {
    devServer = await startGuiServer({ mode: 'development', host: '127.0.0.1', port: 0, strictPort: true });
  } finally {
    if (previousDevPreview === undefined) delete process.env.HYPRIAL_GUI_DEV_PREVIEW;
    else process.env.HYPRIAL_GUI_DEV_PREVIEW = previousDevPreview;
  }
  const { server: productionServer } = await startParityProductionServer();
  const browser = await chromium.launch({ executablePath: browserExecutable(), timeout: 15000 });
  const evidence = process.env.HYPRIAL_BROWSER_EVIDENCE_DIR;
  const results = [];
  try {
    const scenario = scenarioFor(feature);
    const phases = [
      { mode: 'development', url: parityUrl(devServer.url, { scenario, firstRun: false }), external: process.env.HYPRIAL_TEST_DEV_URL, journeys: ['chat', 'org'] },
      { mode: 'production', url: parityUrl(productionServer.url, { scenario, firstRun: false }), external: process.env.HYPRIAL_TEST_PRODUCT_URL, journeys: ['chat', 'org'] },
    ];
    for (const phase of phases) {
      for (const viewport of [{ width: 375, height: 320 }, { width: 375, height: 900 }, { width: 760, height: 900 }, { width: 1440, height: 900 }]) {
        const errors = [], writes = [];
        const { context, page } = await openParityPage(browser, {
          url: phase.external ?? phase.url,
          viewport,
          beforeNavigate: (fresh) => {
            fresh.on('pageerror', error => errors.push(error.message));
            fresh.on('request', request => { if (request.method() !== 'GET') writes.push(request.url()); });
          },
        });
        try {
          await waitForVisible(
            page,
            page.locator('.nav-item[data-nav="start"]'),
            8_000,
            `${feature} ${phase.mode} ${viewport.width}x${viewport.height}: the entry must render the product navigation`,
          );
          if (phase.journeys.includes('chat') && (feature === 'j3' || feature === 'linked')) await checkChat(page);
          if (phase.journeys.includes('org') && ['j5', 'j6', 'j7', 'linked'].includes(feature)) await checkOrg(page, feature);
          assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no root horizontal overflow');
          assert.deepEqual(errors, []);
          assert.deepEqual(writes, [], 'fixture composition must not perform real business writes');
          assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
          if (evidence) {
            mkdirSync(evidence, { recursive: true });
            await page.screenshot({ path: resolve(evidence, `${feature}-${phase.mode}-${viewport.width}x${viewport.height}.png`), fullPage: true });
          }
          results.push({ mode: phase.mode, ...viewport, passed: true });
        } finally { await context.close(); }
      }
    }
  } finally { await browser.close(); await devServer.close(); await productionServer.close(); }
  console.log(JSON.stringify({ contract: `shared-views-${feature}`, results, passed: true }));
}
