import { completeFirstRun } from '../../support/first-run-browser-journey.mjs';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { startGuiServer } from '../../../scripts/serve.mjs';
import { browserExecutable, isStaticFavicon } from '../../support/browser.mjs';
import { parityUrl, startParityProductionServer } from '../../support/dev-parity-browser.mjs';

const chromePath = browserExecutable();
const evidenceDir = process.env.HYPRIAL_BROWSER_EVIDENCE_DIR
  ? resolve(process.env.HYPRIAL_BROWSER_EVIDENCE_DIR)
  : null;
const viewports = [
  { width: 375, height: 320 },
  { width: 375, height: 900 },
  { width: 760, height: 900 },
  { width: 800, height: 900 },
  { width: 900, height: 900 },
  { width: 1440, height: 900 },
];
const staticTypes = new Set(['document', 'script', 'stylesheet', 'image', 'font']);
const referenceEntryPath = '/tests/reference/first-run-product-entry.html';
process.env.HYPRIAL_GUI_HOME = resolve(import.meta.dirname, '..', '..', '..');
process.env.HYPRIAL_BACKEND_ENDPOINT = '';
process.env.HYPRIAL_DEV_MODULE_LABELS = '1';
process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';


async function openConnections(page) {
  await page.getByRole('button', { name: '我的账号与 Agent 设置菜单' }).click();
  await page.getByRole('menuitem', { name: '连接' }).click();
  await page.locator('[data-page="connections"]').waitFor();
}


async function assertNoHorizontalOverflow(page, step) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  assert.ok(overflow <= 0, `${step} must not overflow horizontally (${overflow}px)`);
}

async function capture(page, label) {
  if (!evidenceDir) return;
  mkdirSync(evidenceDir, { recursive: true });
  await page.screenshot({ path: resolve(evidenceDir, `j10-${label}.png`) });
}

async function openReferenceFirstRun(page, server, label, query) {
  await page.goto(`${server.url}${referenceEntryPath}${query}`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-page="settings"]').waitFor();
  assert.equal(
    new URL(page.url()).pathname,
    referenceEntryPath,
    `${label}: first-run journey runs on the test-only product entry`,
  );
  assert.equal(
    await page.locator('[data-page="first-run"]').count(),
    0,
    `${label}: first-run must not mount before the reference event`,
  );
  assert.equal(
    await page.locator('.dev-data-source, .dev-module-index, [data-dev-label]').count(),
    0,
    `${label}: reference entry must keep development chrome out`,
  );
  await assertNoHorizontalOverflow(page, `reference entry settings ${label}`);

  await page.evaluate(() => window.dispatchEvent(new Event('hyprial-test-open-first-run')));
  await page.locator('[data-page="first-run"]').waitFor();
  await page.getByRole('heading', { name: '登录', exact: true }).waitFor();
  assert.equal(
    await page.locator('[aria-current="step"]').getAttribute('data-step-nav'),
    'account',
    `${label}: reference entry opens the account/device step`,
  );
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).waitFor();
  await page.getByText('尚未登录', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('访问令牌', { exact: true }).count(), 0);
  assert.equal(
    await page.getByRole('button', { name: '开始', exact: true }).count(),
    0,
    `${label}: first-run must hide the rail start button`,
  );
  await assertNoHorizontalOverflow(page, `reference entry first-run ${label}`);
}

async function runReferenceEntryViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const label = `${width}x${height}`;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });

  try {
    await openReferenceFirstRun(page, server, label, '');

    await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
    await page.getByText('账号已登录', { exact: true }).waitFor();
    await page.getByText('当前账号：fixture-owner', { exact: true }).waitFor();
    assert.equal(
      await page.getByText('登录结果未知', { exact: true }).count(),
      0,
      `${label}: fixture login resolves to a definite fixture state, not unknown`,
    );
    assert.equal(
      await page.getByText('账号服务不可用', { exact: true }).count(),
      0,
      `${label}: fixture login is not an unavailable result`,
    );
    assert.equal(
      await page.locator('[aria-current="step"]').getAttribute('data-step-nav'),
      'account',
      `${label}: fixture login must not advance the step order`,
    );
    assert.equal(
      await page.getByRole('button', { name: '继续到设备', exact: true }).count(),
      1,
      `${label}: fixture login exposes the device step`,
    );
    assert.equal(
      await page.getByRole('button', { name: '初始化当前设备', exact: true }).count(),
      0,
      `${label}: device initialization lives on the device step`,
    );
    await assertNoHorizontalOverflow(page, `reference entry account ${label}`);

    await page.getByRole('button', { name: '返回设置', exact: true }).click();
    await page.getByRole('heading', { name: '用户设置', exact: true }).waitFor();
    assert.equal(
      await page.locator('.app-first-run').count(),
      0,
      `${label}: leaving first-run must unmount the wizard`,
    );

    assert.deepEqual(errors, [], `${label}: console clean`);
    await capture(page, `reference-entry-${label}`);
    return { mode: 'reference-entry', width, height, passed: true };
  } finally {
    await context.close();
  }
}

async function runReferenceEntryUnknownViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const label = `${width}x${height}`;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });

  try {
    await openReferenceFirstRun(page, server, label, '?account=unknown');

    await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
    await page.getByText('登录结果未知', { exact: true }).waitFor();
    await page.getByText('无法联系账号服务', { exact: true }).waitFor();
    assert.equal(
      await page.getByText('账号已登录', { exact: true }).count(),
      0,
      `${label}: an unknown login result must never render as logged in`,
    );
    assert.equal(
      await page.getByText('当前账号：fixture-owner', { exact: true }).count(),
      0,
      `${label}: the unknown path must not write the fixture login state`,
    );
    assert.equal(
      await page.locator('[aria-current="step"]').getAttribute('data-step-nav'),
      'account',
      `${label}: an unknown login result must stay on the account step`,
    );
    assert.equal(
      await page.getByRole('button', { name: '继续到设备', exact: true }).count(),
      0,
      `${label}: an unknown login result must not unlock the model step`,
    );
    assert.equal(
      await page.getByRole('button', { name: '初始化当前设备', exact: true }).count(),
      0,
      `${label}: an unknown login result must not offer device initialization`,
    );
    assert.equal(
      await page.getByRole('button', { name: '登录 Hyprial', exact: true }).count(),
      1,
      `${label}: the account step keeps the retry entry available`,
    );
    await assertNoHorizontalOverflow(page, `reference entry unknown ${label}`);

    assert.deepEqual(errors, [], `${label}: console clean`);
    await capture(page, `reference-entry-unknown-${label}`);
    return { mode: 'reference-entry-unknown', width, height, passed: true };
  } finally {
    await context.close();
  }
}


async function runDevelopmentViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const requests = [];
  const errors = [];
  const label = `${width}x${height}`;
  page.on('request', (request) => {
    if (isStaticFavicon(request, new URL(server.url).origin)) return;
    if (
      request.method() !== 'GET' ||
      !staticTypes.has(request.resourceType()) ||
      new URL(request.url()).origin !== new URL(server.url).origin
    ) {
      requests.push(`${request.method()} ${request.resourceType()}`);
    }
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });

  try {
    await page.goto(server.url, { waitUntil: 'networkidle' });
    if (width === 375 && height === 900) {
      await page.emulateMedia({ reducedMotion: 'reduce' });
    }
    await completeFirstRun(page);
    await openConnections(page);

    const pageText = (await page.locator('[data-page="connections"]').textContent()) ?? '';
    assert.ok(pageText.includes('模型服务 A'), `${label}: linked service`);
    assert.ok(pageText.includes('已连接'), `${label}: connected`);
    assert.ok(pageText.includes('默认'), `${label}: default tag`);
    assert.ok(/\b1\b/.test(pageText), `${label}: agent count`);

    // Add a second service through the two-method form.
    await page.getByRole('button', { name: '添加模型服务', exact: true }).click();
    await page.getByLabel('服务名称', { exact: true }).fill('模型服务 B');
    await page.getByRole('radio', { name: 'API Key', exact: true }).check();
    await page.getByLabel('API Key 凭据', { exact: true }).fill('qa-fake-key');
    await page.getByRole('button', { name: '添加', exact: true }).click();
    const rows = page.locator('[aria-label="模型服务列表"] [data-service-id]');
    await rows.first().waitFor();

    const zoom = await page.evaluate(() => {
      document.documentElement.style.zoom = '2';
      const width = document.documentElement.scrollWidth - window.innerWidth;
      const zoomed = width;
      document.documentElement.style.zoom = '';
      return zoomed;
    });
    assert.ok(zoom <= 0, `${label}: 200% zoom must not overflow (${zoom}px)`);

    if (width === 375 && height === 900) {
      const motion = await page.evaluate(() => {
        const node = document.querySelector('.model-service-section');
        if (!node) return null;
        const style = getComputedStyle(node);
        return {
          animationName: style.animationName,
          transitionDuration: style.transitionDuration,
        };
      });
      assert.notEqual(motion, null, `${label}: section must render`);
      assert.equal(motion.animationName, 'none', `${label}: no animation`);
      assert.equal(motion.transitionDuration, '0s', `${label}: no transition`);
      // Functionality still works under reduced motion.
      await page.getByRole('button', { name: '添加模型服务', exact: true }).click();
      await page.getByLabel('服务名称', { exact: true }).fill('模型服务 C');
      await page.getByRole('button', { name: '取消', exact: true }).click();
      await page.getByRole('button', { name: '丢弃并返回', exact: true }).click();
    }

    const storage = await page.evaluate(async () => ({
      local: localStorage.length,
      session: sessionStorage.length,
      databases: (await indexedDB.databases()).length,
      cookies: document.cookie.length,
    }));
    assert.deepEqual(storage, { local: 0, session: 0, databases: 0, cookies: 0 }, `${label}: storage`);
    assert.equal(page.url().includes('qa-fake-key'), false, `${label}: no secret in URL`);
    assert.deepEqual(requests, [], `${label}: no business requests`);
    assert.deepEqual(errors, [], `${label}: console clean`);

    // V1: deterministic empty-model dev scenario.
    if (width === 1440) {
      await page.goto(`${server.url}#j10=empty`, { waitUntil: 'networkidle' });
      // A hash-only navigation keeps the old session, so reload the document.
      await page.reload({ waitUntil: 'networkidle' });
      await completeFirstRun(page);
      await openConnections(page);
      const emptyText =
        (await page.locator('[data-page="connections"]').textContent()) ?? '';
      assert.ok(emptyText.includes('还没有模型服务'), `${label}: empty state`);
      assert.equal(
        await page.getByRole('button', { name: '添加模型服务', exact: true }).count(),
        1,
        `${label}: add entry in the empty state`,
      );
      assert.equal(
        emptyText.includes('0 个模型服务'),
        false,
        `${label}: no zero counter`,
      );
      await page.goto(server.url, { waitUntil: 'networkidle' });
      await completeFirstRun(page);
      await openConnections(page);
    }

    if (evidenceDir) {
      mkdirSync(evidenceDir, { recursive: true });
      await page.screenshot({ path: resolve(evidenceDir, `j10-${label}.png`) });
    }
    return { mode: 'development', width, height, passed: true };
  } finally {
    await context.close();
  }
}

async function runProductionViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  const businessRequests = [];
  page.on('request', (request) => {
    if (isStaticFavicon(request, new URL(server.url).origin)) return;
    if (request.method() !== 'GET' || !staticTypes.has(request.resourceType()) ||
        new URL(request.url()).origin !== new URL(server.url).origin) {
      businessRequests.push(`${request.method()} ${request.resourceType()}`);
    }
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  try {
    await page.goto(server.url, { waitUntil: 'networkidle' });
    // Production starts at the product page, then opens the real first-run entry.
    assert.equal(
      await page.getByRole('heading', { level: 1, name: '开始', exact: true }).count(),
      1,
      `${width}x${height}: production starts on the product page`,
    );
    assert.equal(
      await page.getByText('登录或注册', { exact: true }).count(),
      0,
      `${width}x${height}: no fixture first-run in production`,
    );
    assert.ok(
      await page.getByRole('heading', { name: '连接状态', exact: true }).count() >= 1,
      `${width}x${height}: production shows connection status`,
    );
    await page.getByRole('button', { name: '我的账号与 Agent 设置菜单', exact: true }).click();
    await page.getByRole('menuitem', { name: '用户设置', exact: true }).click();
    assert.equal(
      await page.getByRole('button', { name: '打开首次设置', exact: true }).count(),
      0,
      `${width}x${height}: firstRunWizardEnabled=false hides the entry on both entries`,
    );
    await page.getByRole('region', { name: '账号登录', exact: true }).waitFor();
    await page.getByRole('button', { name: '设备', exact: true }).waitFor();
    await page.getByRole('button', { name: '组织', exact: true }).waitFor();
    assert.equal(
      await page.getByRole('region', { name: '模型服务', exact: true }).count(),
      0,
      `${width}x${height}: settings must not embed the model service panel`,
    );
    assert.equal(await page.getByRole('heading', { name: '登录', exact: true }).count(), 0);
    assert.equal(
      await page.getByRole('heading', { name: '开始', exact: true }).count(),
      0,
      `${width}x${height}: settings must not show the start page`,
    );
    assert.equal(await page.getByLabel('访问令牌', { exact: true }).count(), 0);
    assert.equal(await page.getByRole('navigation', { name: '主导航', exact: true }).count(), 1);
    await page.evaluate(() => window.dispatchEvent(new Event('hyprial-test-open-first-run')));
    await page.waitForTimeout(250);
    assert.equal(
      await page.locator('[data-page="first-run"]').count(),
      0,
      `${width}x${height}: the test-only first-run event must not act on the product page`,
    );
    await assertNoHorizontalOverflow(page, `production settings ${width}x${height}`);
    const text = (await page.evaluate(() => document.body.textContent ?? '')) ?? '';
    assert.equal(text.includes('模型服务 B'), false, `${width}x${height}: no dev seed`);
    assert.equal(text.includes('add-failure'), false, `${width}x${height}: no dev scenario`);
    assert.deepEqual(businessRequests, [], `${width}x${height}: no business requests`);
    assert.deepEqual(errors, [], `${width}x${height}: console clean`);
    return { mode: 'production', width, height, passed: true };
  } finally {
    await context.close();
  }
}

async function runMode(mode) {
  let server;
  let browser;
  try {
    if (mode === 'production') {
      const { server: parityServer } = await startParityProductionServer();
      server = {
        ...parityServer,
        url: parityUrl(parityServer.url, { scenario: 'default', firstRun: false }),
      };
    } else {
      server = await startGuiServer({ mode, host: '127.0.0.1', port: 0, strictPort: true });
    }
    browser = await chromium.launch({ executablePath: chromePath });
    const results = [];
    for (const viewport of viewports) {
      results.push(
        mode === 'development'
          ? await runDevelopmentViewport(browser, server, viewport)
          : await runProductionViewport(browser, server, viewport),
      );
    }
    return results;
  } finally {
    await Promise.allSettled([browser?.close(), server?.close()]);
  }
}

async function runReferenceEntryMode(viewportRunner) {
  let server;
  let browser;
  try {
    server = await startGuiServer({ mode: 'development', host: '127.0.0.1', port: 0, strictPort: true });
    assert.equal(server.backendConfigured, false, 'the fixture composition must run without a backend');
    browser = await chromium.launch({ executablePath: chromePath });
    const results = [];
    for (const viewport of viewports) {
      results.push(await viewportRunner(browser, server, viewport));
    }
    return results;
  } finally {
    await Promise.allSettled([browser?.close(), server?.close()]);
  }
}

const development = await runMode('development');
const production = await runMode('production');
const referenceEntry = await runReferenceEntryMode(runReferenceEntryViewport);
const referenceEntryUnknown = await runReferenceEntryMode(runReferenceEntryUnknownViewport);
console.log(JSON.stringify({
  contract: 'j10-model-services',
  development,
  production,
  referenceEntry,
  referenceEntryUnknown,
  dataSources: {
    development: 'explicit dev-preview fixture (HYPRIAL_GUI_DEV_PREVIEW=1), isolated local callbacks only',
    production: 'dev/product parity artifact with scenario default and firstRun=false',
    referenceEntry: 'test-only product entry composition (fixture first-run actions)',
    referenceEntryUnknown: 'same entry with ?account=unknown: authenticateAccount resolves unknown and never writes the login state',
  },
  loginEvidence: 'reference-entry login outcomes are UI state acceptance only; the unknown path (?account=unknown) executes for real and shows 登录结果未知 without advancing; neither path proves or claims a real account login',
  passed: true,
}));
