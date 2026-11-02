/** Built-app acceptance: ephemeral loopback stub + Chromium; no daemon state. */
import assert from 'node:assert/strict';
import { mkdir, readFile, mkdtemp, rm } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import { build, preview } from 'vite';
import { tmpdir } from 'node:os';
import { createStubServer, closeServer } from './stub-backend.mjs';

const guiDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const host = process.env.SHADCN_SMOKE_HOST ?? '127.0.0.1';
const screenshotDir = process.env.SHADCN_SCREENSHOT_DIR;
const outputDir = await mkdtemp(resolve(tmpdir(), 'hyprial-shadcn-smoke-'));
const previousEndpoint = process.env.HYPRIAL_BACKEND_ENDPOINT;
const stub = await createStubServer({ host, port: 0 });
let server;
let browser;

try {
  process.env.HYPRIAL_BACKEND_ENDPOINT = stub.endpoint;
  // The product build intentionally stops at unavailable first-run setup.
  // Exercise the start-page example in an isolated development build.
  await build({ root: guiDir, mode: 'development', build: { outDir: outputDir, emptyOutDir: true } });
  server = await preview({
    mode: 'development',
    build: { outDir: outputDir },
    root: guiDir,
    configFile: resolve(guiDir, 'vite.config.ts'),
    preview: { host, port: 0, strictPort: true, open: false },
  });
  const address = server.httpServer.address();
  assert(address && typeof address === 'object');
  const url = `http://${host.includes(':') ? `[${host}]` : host}:${address.port}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.goto(`${url}/#j1=complete`);
  await page.getByRole('heading', { level: 1, name: '开始' }).waitFor();
  const card = page.getByRole('region', { name: '给下一件事，找一位帮手' });
  const button = card.getByRole('button');
  assert.equal(await button.innerText(), '创建 Agent');
  assert.equal(await card.getAttribute('data-slot'), 'card');
  assert.equal(await button.getAttribute('data-slot'), 'button');
  const styles = await button.evaluate((element) => {
    const s = getComputedStyle(element);
    return { background: s.backgroundColor, color: s.color, minHeight: s.minHeight, radius: s.borderRadius };
  });
  assert.deepEqual(styles, { background: 'rgb(0, 72, 168)', color: 'rgb(255, 255, 255)', minHeight: '40px', radius: '12px' });

  if (process.env.SHADCN_BASELINE_SNAPSHOT) {
    const baseline = JSON.parse(await readFile(process.env.SHADCN_BASELINE_SNAPSHOT, 'utf8'));
    const actual = await page.evaluate((selectors) => Object.fromEntries(selectors.map((selector) => {
      const element = document.querySelector(selector);
      if (!element) return [selector, null];
      const s = getComputedStyle(element);
      return [selector, Object.fromEntries(['fontSize', 'fontFamily', 'color', 'backgroundColor', 'borderRadius', 'padding', 'display'].map((key) => [key, s[key]]))];
    })), Object.keys(baseline));
    assert.deepEqual(actual, baseline, 'unmigrated surfaces must retain their computed styles');
  }

  await button.focus();
  const focus = await button.evaluate((element) => {
    const s = getComputedStyle(element);
    return { width: s.outlineWidth, style: s.outlineStyle };
  });
  assert.deepEqual(focus, { width: '2px', style: 'solid' });
  await page.keyboard.press('Enter');
  await page.getByRole('heading', { level: 2, name: '创建 Agent' }).waitFor();
  assert(await page.getByLabel('名称', { exact: true }).isVisible());
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '开始', exact: true }).click();

  // Opening the local form does not invoke a backend. Verify the HTTP boundary separately.
  const response = await page.request.post(`${url}/api/gui/v1/not-wired`, {
    data: { command: 'agent.create', params: { surface: 'start' } },
  });
  assert.equal(response.status(), 501);
  assert.equal((await response.json()).code, 'BACKEND_NOT_WIRED');

  if (screenshotDir) await mkdir(screenshotDir, { recursive: true });
  for (const width of [1280, 360, 390, 430]) {
    await page.setViewportSize({ width, height: 900 });
    assert(await button.isVisible());
    const bounds = await button.boundingBox();
    assert(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width, `button exceeds ${width}px viewport`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `horizontal overflow at ${width}px`);
    if (width < 979) assert.equal(await card.evaluate((element) => getComputedStyle(element, '::after').display), 'none');
    if (screenshotDir) await page.screenshot({ path: resolve(screenshotDir, `start-${width}.png`), fullPage: true });
    // Long Chinese content must reflow rather than overflow the migrated button.
    await button.locator('.ui-button__label').evaluate((element) => { element.textContent = '创建一位专注于下一件重要工作的协作助手并继续设置'; });
    assert(await button.evaluate((element) => element.scrollWidth <= element.clientWidth));
    await button.locator('.ui-button__label').evaluate((element) => { element.textContent = '创建 Agent'; });
  }

  // Exercise existing consumers of the same adapters.
  await page.getByRole('button', { name: '我的账号与连接菜单' }).click();
  await page.getByRole('menuitem', { name: '设置', exact: true }).click();
  assert(await page.getByRole('heading', { level: 1, name: '设置' }).isVisible());
  assert(await page.locator('[data-slot="card"]').count() > 0);
  assert(await page.locator('[data-slot="button"]').count() > 0);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const duration = await page.locator('[data-slot="button"]').first().evaluate((element) => getComputedStyle(element).transitionDuration);
  assert.equal(duration, '0s');
  assert.deepEqual(pageErrors, []);
  console.log('PASS: built shadcn Button/Card, keyboard, focus, local creation form, real HTTP 501, 1280/360/390/430px, long Chinese, existing consumers, reduced motion.');
} finally {
  await browser?.close();
  if (server) await new Promise((done, reject) => server.httpServer.close((error) => error ? reject(error) : done()));
  await closeServer(stub.server);
  await rm(outputDir, { recursive: true, force: true });
  if (previousEndpoint === undefined) delete process.env.HYPRIAL_BACKEND_ENDPOINT;
  else process.env.HYPRIAL_BACKEND_ENDPOINT = previousEndpoint;
}
