import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { closeSync, mkdirSync, openSync } from 'node:fs';
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createConnection } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { browserExecutable, isStaticFavicon } from './support/browser.mjs';
import { startPythonHost } from './support/python-host.ts';
import { startGuiServer } from '../scripts/serve.mjs';

const chromePath = browserExecutable();
const evidenceDir = process.env.HYPRIAL_BROWSER_EVIDENCE_DIR
  ? resolve(process.env.HYPRIAL_BROWSER_EVIDENCE_DIR)
  : null;
const viewports = [
  { width: 375, height: 320 },
  { width: 375, height: 900 },
  { width: 760, height: 900 },
  { width: 1440, height: 900 },
];
const staticTypes = new Set([
  'document',
  'script',
  'stylesheet',
  'image',
  'font',
]);
const repoRoot = resolve(import.meta.dirname, '../..');
const isolatedOwner = 'first-run-contract-owner';
const isolatedNode = 'first-run-contract-node';
const isolatedPrincipal = `user:${isolatedOwner}`;

process.env.HYPRIAL_GUI_HOME = resolve(import.meta.dirname, '..');
process.env.HYPRIAL_BACKEND_ENDPOINT = '';
process.env.HYPRIAL_GUI_AUTH = ''; // This contract exercises the explicitly disabled local host.

function delay(milliseconds) {
  return new Promise(resolveDelay => setTimeout(resolveDelay, milliseconds));
}

function isolatedEnvironment(root) {
  const inherited = Object.fromEntries(
    Object.entries(process.env).filter(
      ([name]) => !name.startsWith('HYPRIAL_') && !name.startsWith('HARNESS_'),
    ),
  );
  return {
    ...inherited,
    HOME: join(root, 'user'),
    HYPRIAL_HOME: join(root, 'home'),
    HARNESS_STATE_DIR: join(root, 'state'),
    HYPRIAL_NETWORK_ISOLATED: '1',
    HYPRIAL_ZENOH_LISTEN: '',
    HYPRIAL_ZENOH_CONNECT: '',
    HYPRIAL_PEER_DISCOVERY: '0',
    HYPRIAL_OWNER: isolatedOwner,
    HYPRIAL_NODE_ID: isolatedNode,
    NO_PROXY: '127.0.0.1,localhost,::1',
    no_proxy: '127.0.0.1,localhost,::1',
    UV_CACHE_DIR: join(root, 'uv-cache'),
    UV_PYTHON_DOWNLOADS: 'never',
  };
}

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

async function waitFor(read, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      return await read();
    } catch (error) {
      lastError = error;
      await delay(100);
    }
  }
  throw lastError instanceof Error ? lastError : new Error('timed out waiting for isolated services');
}

async function logTail(logPath) {
  try {
    return (await readFile(logPath, 'utf8')).slice(-8_000);
  } catch {
    return '(no process log)';
  }
}

async function waitForChildFile(child, path, logPath, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`isolated process exited before ${path}:\n${await logTail(logPath)}`);
    }
    if (await exists(path)) return;
    await delay(100);
  }
  throw new Error(`timed out waiting for ${path}:\n${await logTail(logPath)}`);
}

function startPython(args, env, logPath) {
  const log = openSync(logPath, 'a');
  const child = spawn('uv', ['run', '--no-sync', 'python', ...args], {
    cwd: repoRoot,
    detached: process.platform !== 'win32',
    env,
    stdio: ['ignore', log, log],
  });
  closeSync(log);
  return child;
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  if (process.platform !== 'win32' && child.pid !== undefined) {
    try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill('SIGTERM'); }
  } else {
    child.kill('SIGTERM');
  }
  await Promise.race([once(child, 'exit'), delay(10_000)]);
  if (child.exitCode !== null) return;
  if (process.platform !== 'win32' && child.pid !== undefined) {
    try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
  } else {
    child.kill('SIGKILL');
  }
  await Promise.race([once(child, 'exit'), delay(3_000)]);
}

async function startIsolatedServices() {
  const tempRoot = process.platform === 'win32' ? tmpdir() : '/tmp';
  const root = await mkdtemp(join(tempRoot, 'hyprial-first-run-browser-'));
  const children = [];
  const close = async () => {
    await Promise.allSettled(children.splice(0).reverse().map(stopChild));
    await rm(root, { recursive: true, force: true });
  };
  try {
    for (const name of ['home', 'state', 'user', 'runtime']) {
      await mkdir(join(root, name), { recursive: true, mode: 0o700 });
    }
    await writeFile(join(root, 'home', 'settings.json'), JSON.stringify({ version: 1, owner: isolatedOwner }));
    const configPath = join(root, 'request-http.json');
    await writeFile(configPath, JSON.stringify({
      schemaVersion: 1,
      backend: { kind: 'local-daemon', socket: join(root, 'state', 'daemon.sock') },
      principal: { kind: 'local-operator', owner: isolatedOwner, actor: isolatedPrincipal },
      listen: { host: '127.0.0.1', port: 0 },
      security: {
        allowedOrigins: ['http://127.0.0.1:4173'],
        allowedHosts: ['127.0.0.1', 'localhost'],
      },
      runtime: { dir: join(root, 'runtime') },
    }), { mode: 0o600 });
    const env = isolatedEnvironment(root);
    const daemonLog = join(root, 'daemon.log');
    const daemon = startPython(['-m', 'hyprial.cli', 'daemon', 'run'], env, daemonLog);
    children.push(daemon);
    await waitForChildFile(daemon, join(root, 'state', 'daemon.sock'), daemonLog, 60_000);

    const httpLog = join(root, 'request-http.log');
    const http = startPython([
      '-m', 'hyprial.shell.impl.request_http', 'serve', '--config', configPath,
    ], env, httpLog);
    children.push(http);
    const serverJson = join(root, 'runtime', 'server.json');
    await waitForChildFile(http, serverJson, httpLog, 60_000);
    const document = JSON.parse(await readFile(serverJson, 'utf8'));
    if (typeof document.endpoint !== 'string' || typeof document.token !== 'string') {
      throw new Error('isolated request-http published an incomplete server.json');
    }
    await waitFor(async () => {
      const response = await fetch(`${document.endpoint}/readyz`, {
        headers: { authorization: `Bearer ${document.token}` },
        redirect: 'error',
      });
      if (response.status !== 200) {
        throw new Error(`isolated request-http readiness returned ${response.status}`);
      }
      return true;
    }, 60_000);
    return { endpoint: document.endpoint, token: document.token, close };
  } catch (error) {
    await close();
    throw error;
  }
}

async function assertNoHorizontalOverflow(page, step) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  assert.ok(overflow <= 0, `${step} must not overflow horizontally (${overflow}px)`);
}

async function assertStep(page, title, step) {
  const heading = page.getByRole('heading', { name: title, exact: true });
  await heading.waitFor();
  if (step !== 'account') {
    await page.waitForFunction(name => document.activeElement?.textContent?.trim() === name, title, { timeout: 5000 });
    assert.equal(await heading.evaluate(element => element === document.activeElement), true);
  }
  assert.equal(
    await page.getByRole('navigation', { name: '主导航', exact: true }).count(),
    0,
    `${title}: product navigation must stay hidden`,
  );
  assert.equal(await page.locator('[aria-current="step"]').count(), 1);
  assert.equal(
    await page.locator('[aria-current="step"]').getAttribute('data-step-nav'),
    step,
  );
  await assertNoHorizontalOverflow(page, title);
  assert.deepEqual(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
    [0, 0],
  );
}

async function capture(page, width, height, label) {
  if (!evidenceDir) return;
  mkdirSync(evidenceDir, { recursive: true });
  await page.screenshot({
    path: resolve(evidenceDir, `first-run-${label}-${width}x${height}.png`),
  });
}

async function assertPortClosed(port) {
  await new Promise((resolveClosed, reject) => {
    const socket = createConnection({ host: '127.0.0.1', port });
    socket.setTimeout(1000);
    socket.on('connect', () => {
      socket.destroy();
      reject(new Error(`test port ${port} is still open`));
    });
    socket.on('error', (error) =>
      error.code === 'ECONNREFUSED' ? resolveClosed() : reject(error),
    );
    socket.on('timeout', () => {
      socket.destroy();
      reject(new Error('port cleanup check timed out'));
    });
  });
}

async function openDevTools(page) {
  const toggle = page.getByTestId('dev-tools-fab');
  if (await toggle.getAttribute('aria-expanded') === 'false') {
    await toggle.click();
  }
}

async function selectScenario(page, scenario) {
  await openDevTools(page);
  const selector = page.locator('[data-mock-scenario="j1"]');
  await selector.selectOption(scenario);
  await page.waitForFunction(
    (value) =>
      document.querySelector('[data-mock-scenario="j1"]')?.value === value,
    scenario,
  );
}

async function assertFailureStep(page, title, step) {
  assert.equal(
    await page.getByRole('heading', { name: title, exact: true }).count(),
    1,
    `${title}: failed action must stay on the current step`,
  );
  assert.equal(
    await page.locator('[aria-current="step"]').getAttribute('data-step-nav'),
    step,
  );
  await assertNoHorizontalOverflow(page, `${title} failure`);
  assert.deepEqual(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
    [0, 0],
  );
}

async function signIn(page) {
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
  await page.getByRole('button', { name: '继续到设备', exact: true }).click();
  await page.getByRole('button', { name: '初始化当前设备', exact: true }).click();
  await page.getByRole('button', { name: '继续到运行配置', exact: true }).click();
  await assertStep(page, '模型服务', 'runtime');
}
async function chooseApiKeyModel(page) {
  await page.getByLabel('DeepSeek API Key', { exact: false }).fill('preview-only-key');
}

async function assertStartConnectionCenterClick(page, width) {
  if (width !== 760) return;

  const button = page.getByRole('button', { name: '查看绑定', exact: true });
  const bounds = await button.boundingBox();
  assert.notEqual(bounds, null, '开始: 查看绑定 button must be visible');
  const center = {
    x: bounds.x + bounds.width / 2,
    y: bounds.y + bounds.height / 2,
  };
  const hit = await page.evaluate(
    ({ x, y }) => {
      const buttonElement = [...document.querySelectorAll('button')].find(
        (candidate) => candidate.textContent?.trim() === '查看绑定',
      );
      const centerElement = document.elementFromPoint(x, y);
      return {
        buttonReceivedCenter:
          buttonElement !== undefined &&
          centerElement !== null &&
          buttonElement.contains(centerElement),
        centerElement: centerElement
          ? `${centerElement.tagName.toLowerCase()}.${String(centerElement.className)}`
          : 'none',
      };
    },
    center,
  );
  assert.equal(
    hit.buttonReceivedCenter,
    true,
    `开始: 查看绑定 center must hit the button, got ${hit.centerElement}`,
  );

  await page.mouse.click(center.x, center.y);
  await page
    .getByRole('heading', { name: 'Agent 设置', level: 1, exact: true })
    .waitFor();
  await page.getByRole('button', { name: '开始', exact: true }).click();
  await page
    .getByRole('heading', { name: '开始', level: 1, exact: true })
    .waitFor();
}

async function assertAccountTabOrderSkipsModuleIndex(page) {
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).focus();
  for (let i = 0; i < 4; i++) {
    assert.equal(await page.evaluate(() => !!document.activeElement?.closest('.dev-module-index')), false);
    await page.keyboard.press('Tab');
  }
}

async function runDevelopmentViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const businessRequests = [];
  const afterInitialRequests = [];
  const errors = [];
  let loaded = false;

  page.on('request', (request) => {
    if (isStaticFavicon(request, server.url)) return;
    const url = new URL(request.url());
    // Shared Connections reads only the explicit test Contract Mock here.
    if (url.origin === server.url && request.method() === 'POST' && url.search === '' &&
        /^\/api\/reference\/mock-success\/daemon\/request\/(agent\.list|agent\.provider\.default)$/.test(url.pathname)) {
      // default also admits writes: only its empty-parameter read is permitted.
      assert.deepEqual(request.postDataJSON(), {});
      return;
    }
    if (loaded) afterInitialRequests.push(request.resourceType());
    if (
      request.method() !== 'GET' ||
      !staticTypes.has(request.resourceType()) ||
      new URL(request.url()).origin !== server.url
    ) {
      businessRequests.push(`${request.method()} ${request.resourceType()}`);
    }
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('requestfailed', () => errors.push('request failed'));
  page.on('response', (response) => {
    if (response.status() >= 400) errors.push(`HTTP ${response.status()}`);
  });

  try {
    await page.goto(server.url, { waitUntil: 'networkidle' });
    loaded = true;
    await openDevTools(page);

    const notice = page.getByRole('note', {
      name: '开发预览数据来源',
      exact: true,
    });
    assert.equal(await notice.count(), 1);
    assert.equal(
      await notice.evaluate((element) => element.closest('.app') === null),
      true,
      'development notice must stay outside the product shell',
    );
    const moduleIndex = page.getByRole('list', {
      name: '开发模块索引',
      exact: true,
    });
    assert.equal(await moduleIndex.count(), 1);
    assert.equal(
      await moduleIndex.evaluate((element) => element.closest('.app') === null),
      true,
      'development module index must stay outside the product shell',
    );
    const scenarioSelector = page.locator('[data-mock-scenario="j1"]');
    assert.equal(await scenarioSelector.count(), 1);
    assert.equal(
      await scenarioSelector.evaluate((element) => element.closest('.app') === null),
      true,
      'development scenario selector must stay outside the product shell',
    );
    assert.deepEqual(
      await scenarioSelector.locator('option').evaluateAll((options) =>
        options.slice(0, 6).map((option) => option.value),
      ),
      [
        'simulate',
        'account-failure',
        'model-failure',
        'conversation-failure',
        'model-unavailable',
        'complete',
      ],
    );
    assert.deepEqual(
      await scenarioSelector.locator('option').evaluateAll((options) =>
        options.slice(6).map((option) => option.value),
      ),
      [
        'provider-success', 'provider-empty', 'provider-error', 'provider-unconfigured',
        'account-success', 'account-empty', 'account-error', 'account-disabled', 'account-ready',
        'logged-out-init', 'logged-in-init',
      ],
    );
    await page.keyboard.press('Escape');

    await assertStep(page, '登录', 'account');
    assert.equal(await page.getByLabel('访问令牌', { exact: true }).count(), 0);
    await assertAccountTabOrderSkipsModuleIndex(page);
    await capture(page, width, height, 'account');
    await signIn(page);
    await capture(page, width, height, 'model');
    await chooseApiKeyModel(page);
    await page.getByRole('button', { name: '保存并继续', exact: true }).click();
    await assertStep(page, '首次对话', 'conversation');
    await page.getByText('隔离数据来源', { exact: true }).waitFor();
    const composer = page.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    await composer.fill('你好，Squire，请安排今天的工作');
    await capture(page, width, height, 'conversation-typing');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByRole('navigation', { name: '主导航', exact: true }).waitFor();
    await page.getByRole('heading', { name: '开始', level: 1, exact: true }).waitFor();
    await capture(page, width, height, 'conversation-sent');
    await page.getByRole('button', { name: '开始', exact: true }).click();
    await assertStartConnectionCenterClick(page, width);

    if (width === 1440) {
      await selectScenario(page, 'conversation-failure');
      await assertStep(page, '登录', 'account');
      await signIn(page);
      await chooseApiKeyModel(page);
      await page.getByRole('button', { name: '保存并继续', exact: true }).click();
      await assertStep(page, '首次对话', 'conversation');
      const failingComposer = page.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
      await failingComposer.fill('这条消息应当发送失败');
      await page.getByRole('button', { name: '发送', exact: true }).click();
      const alert = page.getByRole('alert');
      await alert.waitFor();
      assert.match(await alert.textContent(), /发送失败/);
      assert.equal(await failingComposer.inputValue(), '这条消息应当发送失败');
      assert.equal(await page.getByRole('navigation', { name: '主导航', exact: true }).count(), 0);
      await capture(page, width, height, 'conversation-send-failure');
    }

    await assertNoHorizontalOverflow(page, '开始');
    await capture(page, width, height, 'start');

    await page.waitForLoadState('networkidle');
    const storage = await page.evaluate(() => ({
      local: localStorage.length,
      session: sessionStorage.length,
    }));
    assert.deepEqual(storage, { local: 0, session: 0 });
    assert.deepEqual(businessRequests, []);
    assert.deepEqual(afterInitialRequests.filter((type) => !staticTypes.has(type)), []);
    assert.deepEqual(errors, []);

    return {
      mode: 'development',
      width,
      height,
      businessRequests: 0,
      afterInitialRequests: afterInitialRequests.length,
      storage,
      passed: true,
    };
  } finally {
    await context.close();
  }
}


async function assertSettingsUserOrgSurface(page, width, height) {
  await page.getByRole('heading', { name: '用户设置', level: 1, exact: true }).waitFor();
  await page.getByRole('region', { name: '账号登录', exact: true }).waitFor();
  await page.getByRole('button', { name: '设备', exact: true }).waitFor();
  await page.getByRole('button', { name: '组织', exact: true }).waitFor();
  assert.equal(await page.getByRole('region', { name: '模型服务', exact: true }).count(), 0);
  assert.equal(await page.getByLabel('DeepSeek API Key', { exact: false }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '打开首次设置', exact: true }).count(), 0);
  assert.equal(await page.getByRole('heading', { name: '当前设备', exact: true }).count(), 0);
  const storage = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
    values: [
      ...Object.keys(localStorage).map(name => localStorage.getItem(name)),
      ...Object.keys(sessionStorage).map(name => sessionStorage.getItem(name)),
    ],
  }));
  assert.deepEqual(storage.local, [], 'Settings: no credential or account value may enter local storage');
  assert.deepEqual(storage.session, ['hyprial.gui.session-view']);
  assert.equal(storage.values.some(value => value?.includes('settings-contract-key')), false);
  await assertNoHorizontalOverflow(page, 'production settings user/org surface');
  await capture(page, width, height, 'production-settings-user-org');
}

async function runProductionViewport(browser, server, viewport, grant) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const businessRequests = [];
  const hostRequests = [];
  const sessionResponses = [];
  const errors = [];
  let loaded = false;

  const isHostSession = (url) => {
    const parsed = new URL(url);
    return parsed.origin === server.url && parsed.pathname === '/daemon/session' && parsed.search === '';
  };
  const isHostRequest = (url) => {
    const parsed = new URL(url);
    return parsed.origin === server.url && (
      parsed.pathname === '/daemon/request' || parsed.pathname.startsWith('/daemon/request/')
    );
  };
  page.on('response', (response) => {
    if (isHostSession(response.url())) sessionResponses.push(response);
  });
  page.on('request', (request) => {
    if (isStaticFavicon(request, server.url)) return;
    const url = new URL(request.url());
    if (isHostSession(request.url())) {
      hostRequests.push(`${request.method()} ${url.pathname}`);
      return;
    }
    if (isHostRequest(request.url())) {
      hostRequests.push(`${request.method()} ${url.pathname}`);
      return;
    }
    if (
      request.method() !== 'GET' ||
      !staticTypes.has(request.resourceType()) ||
      url.origin !== server.url
    ) {
      businessRequests.push(`${request.method()} ${request.resourceType()} ${url.pathname}`);
    }
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('requestfailed', () => errors.push('request failed'));
  page.on('response', (response) => {
    if (response.status() >= 400) errors.push(`HTTP ${response.status()} ${new URL(response.url()).pathname}`);
  });

  try {
    const launch = new URL(server.url);
    launch.hash = `grant=${grant}`;
    await page.goto(launch.toString(), { waitUntil: 'networkidle' });
    loaded = true;
    assert.equal(new URL(page.url()).hash, '', 'production must consume the launch grant');
    assert.equal(
      await page.getByRole('heading', { name: '需要可信启动入口', exact: true }).count(),
      0,
      'production must establish a trusted same-origin session instead of BootstrapUnavailable',
    );
    assert.equal(sessionResponses.length, 1, 'production must establish exactly one same-origin session');
    assert.equal(sessionResponses[0].status(), 200);
    const session = await sessionResponses[0].json();
    assert.equal(session.schemaVersion, 1);
    assert.equal(session.ok, true);
    assert.equal(session.driver, 'daemon');
    assert.equal(session.phase, 'ready');
    assert.equal(session.principal.owner, isolatedOwner);
    assert.equal(session.principal.node, isolatedNode);
    assert.equal(session.principal.actor, isolatedPrincipal);
    assert.equal(typeof session.requestId, 'string');
    assert.ok(session.requestId.length > 0);
    assert.equal(typeof session.view, 'string');
    assert.ok(session.view.length > 0);
    assert.equal(typeof session.csrf, 'string');
    assert.ok(session.csrf.length > 0);
    assert.ok(session.catalog.methodCount > 0, 'production must receive the daemon method catalog');
    assert.equal(
      await page.getByRole('note', { name: '开发预览数据来源' }).count(),
      0,
    );
    assert.equal(
      await page.getByRole('list', { name: '开发模块索引' }).count(),
      0,
    );
    assert.equal(
      await page.locator('.dev-data-source, .dev-module-index').count(),
      0,
    );
    await page.getByRole('button', { name: '我的账号与 Agent 设置菜单', exact: true }).click();
    await page.getByRole('menuitem', { name: '用户设置', exact: true }).click();
    await assertSettingsUserOrgSurface(page, width, height);
    assert.equal(await page.getByRole('heading', { name: '登录', exact: true }).count(), 0);
    assert.equal(await page.getByLabel('账号', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('访问令牌', { exact: true }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '打开首次设置', exact: true }).count(), 0);
    assert.equal(await page.getByRole('heading', { name: '开始', exact: true }).count(), 0);
    assert.equal(await page.getByRole('navigation', { name: '主导航', exact: true }).count(), 1);
    await assertNoHorizontalOverflow(page, 'production account/device');
    const storage = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      values: [
        ...Object.keys(localStorage).map(name => localStorage.getItem(name)),
        ...Object.keys(sessionStorage).map(name => sessionStorage.getItem(name)),
      ],
    }));
    assert.deepEqual(
      storage.local,
      [],
      'production account flow must not write local storage',
    );
    assert.deepEqual(
      storage.session,
      ['hyprial.gui.session-view'],
      'production account flow may only retain the connected-session view proof',
    );
    assert.equal(
      storage.values.some(value => value?.includes('settings-contract-key')),
      false,
      'production account flow must not persist test credentials',
    );
    await capture(page, width, height, 'production-account-unavailable');

    await page.waitForLoadState('networkidle');
    assert.deepEqual(businessRequests, []);
    for (const request of hostRequests) {
      assert.match(
        request,
        /^(?:GET|POST) \/daemon\/(?:session|request(?:\/.*)?)$/,
        `unexpected same-origin host request: ${request}`,
      );
    }
    assert.ok(hostRequests.includes('POST /daemon/session'), 'production must establish its session through the host');
    assert.equal(sessionResponses.length, 1);
    assert.deepEqual(errors, []);
    return {
      mode: 'production',
      width,
      height,
      businessRequests: 0,
      passed: true,
    };
  } finally {
    await context.close();
  }
}

async function runMode(mode) {
  let server;
  let browser;
  let services;
  const previousModuleLabels = process.env.HYPRIAL_DEV_MODULE_LABELS;
  const previousDevPreview = process.env.HYPRIAL_GUI_DEV_PREVIEW;
  if (mode === 'development') {
    process.env.HYPRIAL_DEV_MODULE_LABELS = '1';
    process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';
  }
  try {
    if (mode === 'production') {
      services = await startIsolatedServices();
      browser = await chromium.launch({ executablePath: chromePath });
      const results = [];
      for (const viewport of viewports) {
        const grant = randomBytes(32).toString('hex');
        server = await startPythonHost({
          driver: 'daemon',
          endpoint: services.endpoint,
          upstreamToken: services.token,
          principal: { owner: isolatedOwner, node: isolatedNode, actor: isolatedPrincipal },
          launchGrant: grant,
          root: resolve(import.meta.dirname, '..', 'dist'),
        });
        assert.equal(server.backendConfigured, true);
        try {
          results.push(await runProductionViewport(browser, server, viewport, grant));
        } finally {
          const started = server;
          server = undefined;
          await started.close();
          await assertPortClosed(started.port);
        }
      }
      return results;
    }

    server = await startGuiServer({
      mode,
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    });
    assert.equal(server.backendConfigured, false);
    browser = await chromium.launch({ executablePath: chromePath });
    const results = [];
    for (const viewport of viewports) {
      results.push(await runDevelopmentViewport(browser, server, viewport));
    }
    return results;
  } finally {
    const cleanup = await Promise.allSettled([
      browser?.close(),
      server?.close(),
      services?.close(),
    ]);
    const failures = cleanup
      .filter((result) => result.status === 'rejected')
      .map((result) => result.reason);
    if (failures.length) {
      throw new AggregateError(failures, 'browser contract cleanup failed');
    }
    if (server) {
      await assertPortClosed(server.port);
    }
    if (previousModuleLabels === undefined) {
      delete process.env.HYPRIAL_DEV_MODULE_LABELS;
    } else {
      process.env.HYPRIAL_DEV_MODULE_LABELS = previousModuleLabels;
    }
    if (previousDevPreview === undefined) {
      delete process.env.HYPRIAL_GUI_DEV_PREVIEW;
    } else {
      process.env.HYPRIAL_GUI_DEV_PREVIEW = previousDevPreview;
    }
  }
}

const development = await runMode('development');
const production = await runMode('production');
console.log(
  JSON.stringify({
    contract: 'first-run-j1',
    development,
    production,
    passed: true,
  }),
);
