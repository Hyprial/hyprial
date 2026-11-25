// #2168 shares standalone V17 onboarding; exit through settings before using the rail.
// Decision: gui/docs/single-view-composition.zh-CN.md; attribution: docs/evidence/gui-sr1-contracts-2026-10-31.md.
// Settings convergence removed the settings first-run entry; the browser contract asserts the converged
// settings surface (账号登录) and keeps the wizard unmounted after completion.
import { completeFirstRun } from './support/first-run-browser-journey.mjs';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { startGuiServer } from '../scripts/serve.mjs';
import { browserExecutable } from './support/browser.mjs';

const chromePath = browserExecutable();
const evidenceDir = process.env.HYPRIAL_BROWSER_EVIDENCE_DIR
  ? resolve(process.env.HYPRIAL_BROWSER_EVIDENCE_DIR)
  : null;
const VIEWPORT_WIDTHS = [375, 760, 800, 900, 1440];
const NAV_ITEMS = ['开始', '聊天', 'Agent', '组织'];

process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';

const server = await startGuiServer({
  mode: 'development',
  host: '127.0.0.1',
  port: 0,
  strictPort: true,
});
const browser = await chromium.launch({ executablePath: chromePath });

async function openAccountPage(page, name) {
  await page
    .getByRole('button', { name: '我的账号与 Agent 设置菜单' })
    .click({ timeout: 2_000 });
  await page.getByRole('menuitem', { name }).click({ timeout: 2_000 });
}


async function openDevTools(page) {
  const toggle = page.getByTestId('dev-tools-fab');
  if (await toggle.getAttribute('aria-expanded') === 'false') {
    await toggle.click();
  }
}

async function assertLabels(page, width) {
  await openDevTools(page);
  assert.equal(
    await page.locator('[data-dev-label]').count(),
    0,
    `product navigation must not contain development labels at ${width}px`,
  );
  assert.equal(
    await page.locator('.dev-module-entry').count(),
    6,
    `the floating panel should contain six module entries at ${width}px`,
  );
  await page.keyboard.press('Escape');
}

async function assertFrame(page, width, pageName) {
  await openDevTools(page);
  const frame = await page.evaluate(() => {
    const notice = document.querySelector('.dev-data-source');
    const index = document.querySelector('.dev-module-index');
    const toolsHost = document.querySelector('.dev-tools-host');
    if (
      !(notice instanceof HTMLElement) ||
      !(index instanceof HTMLElement) ||
      !(toolsHost instanceof HTMLElement)
    ) {
      return null;
    }
    return {
      hostOutsideApp: toolsHost.closest('.app') === null,
      noticeOutsideApp: notice.closest('.app') === null,
      indexOutsideApp: index.closest('.app') === null,
      noticePointerEvents: getComputedStyle(notice).pointerEvents,
      rootOverflows: document.documentElement.scrollWidth > window.innerWidth,
    };
  });
  assert.notEqual(frame, null, `${pageName} should render the floating development tools`);
  assert.equal(frame.hostOutsideApp, true, `${pageName} tools must stay outside the product shell`);
  assert.equal(frame.noticeOutsideApp, true, `${pageName} notice must stay outside the product shell`);
  assert.equal(frame.indexOutsideApp, true, `${pageName} module index must stay outside the product shell`);
  assert.equal(
    frame.noticePointerEvents,
    'none',
    `${pageName} notice should ignore pointer input at ${width}px`,
  );
  assert.equal(
    frame.rootOverflows,
    false,
    `${pageName} should not overflow horizontally at ${width}px`,
  );
  await page.keyboard.press('Escape');
}

try {
  for (const width of VIEWPORT_WIDTHS) {
    const page = await browser.newPage({
      viewport: { width, height: 900 },
      deviceScaleFactor: 1,
    });
    const consoleErrors = [];
    const pageErrors = [];
    const failedRequests = [];
    const badResponses = [];

    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    page.on('pageerror', (error) => pageErrors.push(error.message));
    page.on('requestfailed', (request) => {
      failedRequests.push(`${request.method()} ${request.url()}`);
    });
    page.on('response', (response) => {
      if (response.status() >= 400) {
        badResponses.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto(server.url, { waitUntil: 'networkidle' });
    await completeFirstRun(page);
    await assertLabels(page, width);
    await assertFrame(page, width, '开始');

    for (const name of NAV_ITEMS.slice(1)) {
      await page
        .getByRole('button', { name, exact: true })
        .click({ timeout: 2_000 });
      await assertFrame(page, width, name);
    }

    await openAccountPage(page, 'Agent 设置');
    await assertFrame(page, width, '连接');
    await openAccountPage(page, '用户设置');
    await assertFrame(page, width, '用户设置');
    await page.getByRole('region', { name: '账号登录', exact: true }).waitFor();
    await page.getByRole('button', { name: '设备', exact: true }).waitFor();
    await page.getByRole('button', { name: '组织', exact: true }).waitFor();
    assert.equal(
      await page.getByRole('button', { name: '打开首次设置', exact: true }).count(),
      0,
      'converged settings must not offer the first-run wizard entry',
    );
    assert.equal(
      await page.getByRole('region', { name: '模型服务', exact: true }).count(),
      0,
      'converged settings must not embed the model service panel',
    );
    assert.equal(
      await page.locator('.app-first-run').count(),
      0,
      'completed first-run must stay unmounted on settings',
    );
    await assertLabels(page, width);
    await assertFrame(page, width, '用户设置');
    await page.getByRole('button', { name: '开始', exact: true }).click();
    await page.getByRole('heading', { name: '开始', exact: true }).waitFor();
    await assertFrame(page, width, '返回开始');

    await page.getByRole('button', { name: '聊天', exact: true }).click({
      timeout: 2_000,
    });
    const manage = page.getByRole('button', { name: '管理 Agent' });
    await manage.scrollIntoViewIfNeeded();
    assert.equal(
      await manage.isVisible(),
      true,
      `管理 Agent should be visible at ${width}px`,
    );
    await manage.evaluate((element) => {
      element.addEventListener(
        'click',
        (event) => {
          event.preventDefault();
          event.stopImmediatePropagation();
        },
        { capture: true, once: true },
      );
    });
    await manage.click({ timeout: 2_000 });

    if (evidenceDir) {
      mkdirSync(evidenceDir, { recursive: true });
      await page.screenshot({
        path: resolve(evidenceDir, `overview-${width}.png`),
      });
    }

    assert.deepEqual(consoleErrors, [], `console errors at ${width}px`);
    assert.deepEqual(pageErrors, [], `page errors at ${width}px`);
    assert.deepEqual(failedRequests, [], `failed requests at ${width}px`);
    assert.deepEqual(badResponses, [], `HTTP errors at ${width}px`);

    await page.close();
  }

  console.log('safe R1 browser contract passed');
} finally {
  await browser.close();
  await server.close();
}
