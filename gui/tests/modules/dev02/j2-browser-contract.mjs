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

const staticTypes = new Set([
  'document',
  'script',
  'stylesheet',
  'image',
  'font',
]);

process.env.HYPRIAL_GUI_HOME = resolve(import.meta.dirname, '../../..');
process.env.HYPRIAL_BACKEND_ENDPOINT = '';
process.env.HYPRIAL_DEV_MODULE_LABELS = '1';
process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';

async function openDevTools(page) {
  const toggle = page.getByTestId('dev-tools-fab');
  if (await toggle.getAttribute('aria-expanded') === 'false') {
    await toggle.click();
  }
}

async function selectScenario(page, key, value) {
  await openDevTools(page);
  await page.locator(`[data-mock-scenario="${key}"]`).selectOption(value);
  await page.waitForFunction(
    ({ selector, expected }) =>
      document.querySelector(selector)?.value === expected,
    { selector: `[data-mock-scenario="${key}"]`, expected: value },
  );
  await page.keyboard.press('Escape');
}

async function openAgents(page) {
  await page.getByRole('button', { name: 'Agent', exact: true }).click({
    timeout: 2_000,
  });
  await page.locator('[data-page="contacts"]').waitFor();
}

async function assertNoRootOverflow(page, label) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  assert.ok(overflow <= 0, `${label}: root must not overflow (${overflow}px)`);
}

async function assertDevLayersOutsideApp(page, label) {
  await openDevTools(page);
  const result = await page.evaluate(() => {
    const toolsHost = document.querySelector('.dev-tools-host');
    const index = document.querySelector('.dev-module-index');
    if (!(toolsHost instanceof HTMLElement) || !(index instanceof HTMLElement)) return null;
    return {
      hostOutsideApp: toolsHost.closest('.app') === null,
      devModuleInApp: document.querySelector('.app .dev-module-index') !== null,
    };
  });
  assert.notEqual(result, null, `${label}: floating tools must render`);
  assert.equal(result.hostOutsideApp, true, `${label}: dev tools stay outside the product shell`);
  assert.equal(
    result.devModuleInApp,
    false,
    `${label}: module index must not enter the product`,
  );
  await page.keyboard.press('Escape');
}

/** Every card action must be fully visible and clickable at its center. */
async function assertCardsNotClipped(page, label) {
  const cards = page.locator('[data-testid^="contact-agent-"]');
  const count = await cards.count();
  assert.ok(count > 0, `${label}: cards must exist`);
  for (let index = 0; index < count; index += 1) {
    const card = cards.nth(index);
    await card.evaluate((element) =>
      element.scrollIntoView({ block: 'center', inline: 'center' }),
    );
    const metrics = await card.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const center = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      );
      return {
        width: rect.width,
        height: rect.height,
        insideViewportX: rect.left >= 0 && rect.right <= window.innerWidth,
        centerHits: center !== null && element.contains(center),
      };
    });
    assert.ok(metrics.width > 0, `${label}: card ${index} width`);
    assert.ok(metrics.height > 0, `${label}: card ${index} height`);
    assert.equal(metrics.insideViewportX, true, `${label}: card ${index} clip`);
    assert.equal(metrics.centerHits, true, `${label}: card ${index} center`);
  }
}

/** The list title must be the active element after list re-mounts. */
async function assertListHeadingFocused(page, label) {
  await page.waitForFunction(
    () => {
      const active = document.activeElement;
      return (
        active !== null &&
        active.tagName === 'H2' &&
        active.textContent?.trim() === 'Agent'
      );
    },
    undefined,
    { timeout: 800 },
  );
}

async function assertNoStorageWrites(page, label) {
  const storage = await page.evaluate(() => [
    localStorage.length,
    sessionStorage.length,
  ]);
  assert.deepEqual(storage, [0, 0], `${label}: no storage writes`);
}

async function fillCreateForm(page, name) {
  await page.getByLabel('名称', { exact: true }).fill(name);
  await page.getByLabel('职责', { exact: true }).fill('端到端测试');
}

async function runDevelopmentViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const businessRequests = [];
  const errors = [];
  const label = `${width}x${height}`;

  page.on('request', (request) => {
    if (isStaticFavicon(request, new URL(server.url).origin)) return;
    if (
      request.method() !== 'GET' ||
      !staticTypes.has(request.resourceType()) ||
      new URL(request.url()).origin !== new URL(server.url).origin
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
    if (width >= 700) {
      await openDevTools(page);
      const j1Width = await page.locator('[data-mock-scenario="j1"]').evaluate((element) =>
        element.getBoundingClientRect().width,
      );
      assert.ok(j1Width >= 140, `${label}: J1 selected value must remain readable, got ${j1Width}px`);
      await page.keyboard.press('Escape');
    }
    await selectScenario(page, 'j1', 'complete');
    await selectScenario(page, 'j2', 'm04');
    await page
      .getByRole('heading', { level: 1, name: '开始', exact: true })
      .waitFor();

    await openAgents(page);
    await assertNoRootOverflow(page, `${label} M-04`);
    await assertDevLayersOutsideApp(page, `${label} M-04`);

    await assertCardsNotClipped(page, `${label} M-04`);
    assert.equal(
      await page.locator('[data-testid^="contact-agent-"]').count(),
      3,
      `${label}: M-04 card count`,
    );
    assert.equal(
      /显示 \d+ \/ \d+ 个 Agent/u.test(
        (await page.locator('[data-page="contacts"]').textContent()) ?? '',
      ),
      false,
      `${label}: M-04 has no raw count line`,
    );

    // M-06: both unconnected states, and creation stays unavailable.
    await selectScenario(page, 'j2', 'm06');
    await openAgents(page);
    const cardTextM06 =
      (await page.locator('[data-testid="contact-agent-unconnected"]').textContent()) ?? '';
    assert.ok(cardTextM06.includes('需绑定模型凭据'), `${label}: M-06 card`);
    await page.locator('[data-testid="contact-agent-unconnected"]').click({
      timeout: 2_000,
    });
    const detailText =
      (await page.locator('[data-selected="true"]').textContent()) ?? '';
    assert.ok(
      detailText.includes('服务商凭据授权状态未确认'),
      `${label}: M-06 credential section`,
    );
    assert.ok(detailText.includes('未连接渠道'), `${label}: M-06 channel`);
    await page.getByRole('link', { name: 'Agent' }).click({
      timeout: 2_000,
    });
    await page.getByRole('button', { name: '创建 Agent' }).first().click({
      timeout: 2_000,
    });
    // Creation is no longer gated by model credentials: the form stays
    // enabled and points granting to the detail page.
    assert.equal(
      await page.getByRole('button', { name: '创建 Agent' }).last().isDisabled(),
      false,
      `${label}: create enabled without bound credentials`,
    );
    const formTextM06 =
      (await page.locator('.agent-panel').first().textContent()) ?? '';
    assert.ok(
      formTextM06.includes('模型凭据不在此处配置'),
      `${label}: M-06 grant-after-create hint`,
    );
    await page.getByRole('button', { name: '取消' }).click({
      timeout: 2_000,
    });

    // M-05 empty state at the phone viewport only.
    if (height === 900 && width === 375) {
      await selectScenario(page, 'j2', 'm05');
      await openAgents(page);
      const text = (await page.locator('[data-page="contacts"]').textContent()) ?? '';
      assert.ok(text.includes('还没有 Agent'), `${label}: M-05 empty state`);
      assert.equal(
        text.includes('找到 0 个，共 0 个'),
        false,
        `${label}: M-05 has no zero count`,
      );
      if (evidenceDir) {
        mkdirSync(evidenceDir, { recursive: true });
        await page.screenshot({ path: resolve(evidenceDir, `j2-m05-${label}.png`) });
      }
    }

    // Full local CRUD on M-04 at the two review viewports.
    if ((width === 1440 || width === 375) && height === 900) {
      await selectScenario(page, 'j2', 'm04');
      await openAgents(page);
      await page.getByRole('button', { name: '创建 Agent' }).first().click({
        timeout: 2_000,
      });
      await fillCreateForm(page, '端到端 Agent');
      await page.getByRole('button', { name: '创建 Agent' }).click({
        timeout: 2_000,
      });
      await page.waitForFunction(
        () => document.querySelectorAll('[data-testid^="contact-agent-"]').length === 4,
        undefined,
        { timeout: 5_000 },
      );
      await assertListHeadingFocused(page, `${label} after create`);
      const createdText =
        (await page.locator('[data-page="contacts"]').textContent()) ?? '';
      assert.equal(/显示 \d+ \/ \d+ 个 Agent/u.test(createdText), false, `${label}: created has no raw count`);

      const created = page
        .locator('[data-testid^="contact-agent-"]')
        .filter({ hasText: '端到端 Agent' });
      await created.click({ timeout: 2_000 });
      await page.getByRole('button', { name: '编辑' }).click({ timeout: 2_000 });
      await page.getByLabel('名称', { exact: true }).fill('改名 Agent');
      await page.getByRole('button', { name: '保存修改' }).click({
        timeout: 2_000,
      });
      await page.waitForFunction(
        () =>
          document
            .querySelector('[data-selected="true"]')
            ?.textContent?.includes('改名 Agent') === true,
        undefined,
        { timeout: 5_000 },
      );

      assert.equal(await page.getByRole('button', { name: '删除', exact: true }).count(), 0,
        `${label}: legacy fixtures cannot bypass the daemon confirmation pair`);
      await page.getByRole('link', { name: 'Agent', exact: true }).click();

      if (evidenceDir) {
        mkdirSync(evidenceDir, { recursive: true });
        await page.screenshot({ path: resolve(evidenceDir, `j2-list-${label}.png`) });
      }
    }

    // Form layout checks at the remaining viewports.
    if (
      (width === 375 && height === 320) ||
      (width === 800 && height === 900)
    ) {
      await selectScenario(page, 'j2', 'm04');
      await openAgents(page);
      await page.getByRole('button', { name: '创建 Agent' }).first().click({
        timeout: 2_000,
      });
      await assertNoRootOverflow(page, `${label} create form`);
      await page.getByLabel('名称', { exact: true }).fill('长名称 Agent');
      await page
        .getByLabel('职责', { exact: true })
        .fill('很长的职责描述，用来检查表单在窄屏下的换行与滚动是否正常');
      await page.getByLabel('名称', { exact: true }).evaluate((element) =>
        element.scrollIntoView({ block: 'center' }),
      );
      if (evidenceDir) {
        mkdirSync(evidenceDir, { recursive: true });
        await page.screenshot({ path: resolve(evidenceDir, `j2-form-${label}.png`) });
      }
      await page.getByRole('button', { name: '取消' }).click({
        timeout: 2_000,
      });
    }

    if (width === 900 && height === 900) {
      await selectScenario(page, 'j2', 'm04');
      await openAgents(page);
      await page.locator('[data-testid="contact-agent-squire"]').click({
        timeout: 2_000,
      });
      await page.getByRole('button', { name: '编辑' }).click({ timeout: 2_000 });
      assert.equal(
        await page.getByLabel('名称', { exact: true }).inputValue(),
        'Squire',
        `${label}: edit prefill`,
      );
      await page.getByRole('button', { name: '取消' }).click({
        timeout: 2_000,
      });
    }

    // Not-found: the chat still lists a fixture Agent while the J2 seed is empty.
    if (width === 1440 || width === 375) {
      await selectScenario(page, 'j2', 'm05');
      await selectScenario(page, 'j3', 'm23');
      await page.getByRole('button', { name: '聊天', exact: true }).click({
        timeout: 2_000,
      });
      const infoToggle = page.getByTestId('chat-info-toggle');
      if (await infoToggle.getAttribute('aria-expanded') === 'false') {
        await infoToggle.click();
      }
      const manageAgent = page.getByRole('button', { name: '管理 Agent' });
      await manageAgent.scrollIntoViewIfNeeded();
      if (evidenceDir) {
        mkdirSync(evidenceDir, { recursive: true });
        await page.screenshot({ path: resolve(evidenceDir, `j2-chat-management-${label}.png`) });
      }
      await manageAgent.click({
        timeout: 2_000,
      });
      await page.locator('[data-page="contacts"]').waitFor();
      const status = page.locator('[data-page="contacts"] [role="status"]');
      await status.waitFor({ timeout: 5_000 });
      assert.ok(
        ((await status.textContent()) ?? '').includes('找不到该 Agent'),
        `${label}: not-found status copy`,
      );
      await page.waitForFunction(
        () =>
          document.activeElement?.textContent?.trim() === '找不到该 Agent',
        undefined,
        { timeout: 800 },
      );
      await selectScenario(page, 'j3', 'baseline');
    }

    if (height === 320) {
      const chrome = await page.evaluate(() => {
        const layer = document.querySelector('.dev-tools-layer');
        return {
          chromeHeight: Math.round(layer?.getBoundingClientRect().height ?? 0),
          appHeight: Math.round(
            document.querySelector('.app')?.getBoundingClientRect().height ?? 0,
          ),
          selectors: document.querySelectorAll('[data-mock-scenario]').length,
        };
      });
      assert.ok(
        chrome.chromeHeight <= 80,
        `${label}: collapsed development tools must stay <= 80px, got ${chrome.chromeHeight}`,
      );
      assert.ok(
        chrome.appHeight >= 240,
        `${label}: app must keep >= 240px, got ${chrome.appHeight}`,
      );
      assert.equal(chrome.selectors, 0, `${label}: selectors stay collapsed by default`);
      await openDevTools(page);
      assert.equal(
        await page.locator('[data-mock-scenario]').count(),
        6,
        `${label}: six J scenario selectors are available after expanding the tools`,
      );
      assert.equal(
        await page.locator('[data-org-dev-data]').count(),
        1,
        `${label}: the organization data selector is available after expanding the tools`,
      );
      await page.keyboard.press('Escape');
      await selectScenario(page, 'j2', 'm06');
      await selectScenario(page, 'j2', 'm04');
    }

    await page.waitForLoadState('networkidle');
    await assertNoStorageWrites(page, label);
    assert.deepEqual(businessRequests, [], `${label}: no requests`);
    assert.deepEqual(errors, [], `${label}: console clean`);

    return { mode: 'development', width, height, passed: true };
  } finally {
    await context.close();
  }
}

async function runProductionViewport(browser, server, viewport) {
  const { width, height } = viewport;
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const businessRequests = [];
  const errors = [];
  page.on('request', (request) => {
    if (isStaticFavicon(request, new URL(server.url).origin)) return;
    if (
      request.method() !== 'GET' ||
      !staticTypes.has(request.resourceType()) ||
      new URL(request.url()).origin !== new URL(server.url).origin
    ) {
      businessRequests.push(`${request.method()} ${request.resourceType()}`);
    }
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });

  try {
    await page.goto(server.url, { waitUntil: 'networkidle' });
    const label = `${width}x${height}`;
    assert.equal(
      await page.locator('[data-mock-scenario="j2"]').count(),
      0,
      `${label}: no J2 selector in production`,
    );
    assert.equal(
      await page.locator('.dev-data-source, .dev-preview-frame').count(),
      0,
      `${label}: no development chrome`,
    );
    assert.equal(
      await page.locator('[data-page="contacts"]').count(),
      0,
      `${label}: Agent page unreachable before setup`,
    );
    if (width === 1440) {
      const bundle = await page.evaluate(async () => {
        const script = document.querySelector('script[type="module"]');
        const src = script instanceof HTMLScriptElement ? script.src : '';
        if (!src) return '';
        return fetch(src).then((response) => response.text());
      });
      for (const copy of [
        'Agent 创建暂不可用。',
        'Agent 更新暂不可用。',
        'Agent 删除暂不可用。',
      ]) {
        assert.equal(
          bundle.includes(copy),
          false,
          `${label}: production bundle must not ship static copy "${copy}"`,
        );
      }
      assert.equal(
        bundle.includes('M-04'),
        false,
        `${label}: production bundle must not ship J2 scene names`,
      );
      assert.equal(
        bundle.includes('接口未接通'),
        false,
        `${label}: production bundle must not ship interface reasons`,
      );
      // Reading the bundle is this check's own request, not a business call.
      businessRequests.length = 0;
    }
    const text = await page.evaluate(() => document.body.textContent ?? '');
    for (const forbidden of ['本地测试失败', 'M-04', '显示 3 / 3']) {
      assert.equal(
        text.includes(forbidden),
        false,
        `${label}: production must not show ${forbidden}`,
      );
    }
    await page.waitForLoadState('networkidle');
    assert.deepEqual(businessRequests, [], `${label}: no requests`);
    assert.deepEqual(errors, [], `${label}: console clean`);
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
        url: parityUrl(parityServer.url, { scenario: 'default', firstRun: true }),
      };
    } else {
      server = await startGuiServer({
        mode,
        host: '127.0.0.1',
        port: 0,
        strictPort: true,
      });
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

const development = await runMode('development');
const production = await runMode('production');
console.log(
  JSON.stringify({
    contract: 'j2-agents',
    development,
    production,
    passed: true,
  }),
);
