// #2172 uses ChatWorkspaceView for preview and product; message data attributes are shared.
// Decision: gui/docs/shared-ui-entry.zh-CN.md; attribution: docs/evidence/gui-sr1-contracts-2026-10-31.md.
// Browser lookup (2026-11-15, PR 1f/F6): the DEV01/HYPRIAL executable override, otherwise the
// Chromium build pinned by playwright-core in PLAYWRIGHT_BROWSERS_PATH (what CI provisions).
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import { startGuiServer } from '../scripts/serve.mjs';
import { createCjkFontEnvironment } from './dev01-visual-font.mjs';

const VIEWPORT_WIDTHS = [375, 760, 1440];
export function findBrowserExecutable() {
  const configured =
    process.env.DEV01_CHROMIUM_EXECUTABLE ?? process.env.HYPRIAL_BROWSER_EXECUTABLE;
  const candidate = configured ?? chromium.executablePath();
  return existsSync(candidate) ? candidate : null;
}

function activeLabel() {
  const element = document.activeElement;
  if (
    !(element instanceof HTMLElement) ||
    element === document.body ||
    element === document.documentElement
  ) {
    return '';
  }
  const label =
    element.getAttribute('aria-label') ??
    element.labels?.[0]?.textContent ??
    element.textContent ??
    '';
  return label.replace(/\s+/g, ' ').trim();
}

async function probeBrowser(page) {
  return page.evaluate(() => ({
    ...window.__dev01Probe,
    localStorageLength: localStorage.length,
    sessionStorageLength: sessionStorage.length,
  }));
}

async function resetScroll(page) {
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    for (const element of document.querySelectorAll('*')) {
      const style = getComputedStyle(element);
      if (
        (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
        element.scrollTop !== 0
      ) {
        element.scrollTop = 0;
      }
    }
  });
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => resolve());
      }),
  );
}

async function assertLayout(page, width) {
  // 指针移出按钮，避免 hover 干扰；再轮询到过渡结束后的精确色（不靠固定 sleep）。
  await page.mouse.move(1, 1);
  await page.waitForFunction(
    () => {
      const node = document.querySelector('[data-step] .ui-button--primary');
      return !!node && getComputedStyle(node).backgroundColor === 'rgb(23, 19, 31)';
    },
    undefined,
    { timeout: 5000 },
  );
  const layout = await page.evaluate(() => {
    const rectOf = (element) => {
      if (!(element instanceof HTMLElement)) {
        return null;
      }
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
      };
    };
    const root = document.querySelector('.dev01-v17-onboarding');
    const grid = document.querySelector('.first-run-grid');
    const cards = Array.from(document.querySelectorAll('.first-run-card'));
    const logo = document.querySelector('.dev01-v17-brand-mark');
    const navItems = Array.from(
      document.querySelectorAll('[data-step-nav]'),
    );
    const currentStep = document.querySelector(
      '[data-step-nav][aria-current="step"]',
    );
    const heading = document.querySelector('[data-step] h2');
    const primary = document.querySelector(
      '[data-step] .ui-button--primary',
    );
    const secondary = document.querySelector(
      '[data-step] .ui-button--secondary',
    );
    const cardStyle = cards[0] ? getComputedStyle(cards[0]) : null;
    const currentStepStyle = currentStep
      ? getComputedStyle(currentStep)
      : null;
    const headingStyle = heading ? getComputedStyle(heading) : null;
    const primaryStyle = primary ? getComputedStyle(primary) : null;
    const secondaryStyle = secondary ? getComputedStyle(secondary) : null;
    return {
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      root: rectOf(root),
      card: rectOf(cards[0]),
      cardCount: cards.length,
      activeCardCount: document.querySelectorAll(
        '[data-step][data-state="active"] .first-run-card',
      ).length,
      gridCount: grid ? 1 : 0,
      navLabels: navItems.map(
        (item) =>
          item.querySelector('.dev01-v17-step__label')?.textContent?.trim() ??
          '',
      ),
      navCurrentCount: navItems.filter(
        (item) => item.getAttribute('aria-current') === 'step',
      ).length,
      activeStep: root?.getAttribute('data-active-step') ?? null,
      // B-FIX-003 A：品牌区改为文字字标，不再是 <img>。
      logo: {
        alt: logo?.getAttribute('alt') ?? '',
        width: rectOf(logo)?.width ?? 0,
      },
      styles: {
        stepRadius: currentStepStyle?.borderRadius ?? null,
        stepBackground: currentStepStyle?.backgroundColor ?? null,
        stepWeight: currentStepStyle?.fontWeight ?? null,
        stepFontSize: currentStepStyle?.fontSize ?? null,
        cardRadius: cardStyle?.borderRadius ?? null,
        cardBackground: cardStyle?.backgroundColor ?? null,
        cardPadding: cardStyle?.padding ?? null,
        titleWeight: headingStyle?.fontWeight ?? null,
        titleSize: headingStyle?.fontSize ?? null,
        primaryBackground: primaryStyle?.backgroundColor ?? null,
        primaryRadius: primaryStyle?.borderRadius ?? null,
        primaryMinHeight: primaryStyle?.minHeight ?? null,
        secondaryBackground: secondaryStyle?.backgroundColor ?? null,
      },
    };
  });

  assert.equal(layout.innerWidth, width, `viewport width at ${width}`);
  assert.ok(
    layout.scrollWidth <= width + 1,
    `horizontal overflow at ${width}: ${layout.scrollWidth} > ${width}`,
  );
  assert.ok(layout.root, `missing onboarding root at ${width}`);
  assert.ok(layout.card, `missing active step card at ${width}`);
  assert.equal(
    layout.cardCount,
    1,
    `expected one rendered step card at ${width}`,
  );
  assert.equal(
    layout.activeCardCount,
    1,
    `expected one active step card at ${width}`,
  );
  assert.equal(layout.gridCount, 0, `four-card grid returned at ${width}`);
  assert.deepEqual(
    layout.navLabels,
    ['1 · 登录', '2 · 设备', '3 · 运行配置', '4 · 对话'],
    `step navigation at ${width}`,
  );
  assert.equal(
    layout.navCurrentCount,
    1,
    `expected one current step at ${width}`,
  );
  assert.equal(layout.activeStep, 'account', `active step at ${width}`);
  assert.ok(
    layout.card.width <= 721,
    `step card wider than v17 column at ${width}: ${layout.card.width}`,
  );
  assert.ok(
    layout.card.left >= -1 && layout.card.right <= width + 1,
    `step card escapes viewport at ${width}: ${JSON.stringify(layout.card)}`,
  );
  assert.ok(
    Math.abs(
      (layout.root.left + layout.root.right) / 2 - width / 2,
    ) <= 1.5,
    `onboarding root not centered at ${width}: ${JSON.stringify(layout.root)}`,
  );
  assert.ok(
    Math.abs(
      (layout.card.left + layout.card.right) / 2 -
        (layout.root.left + layout.root.right) / 2,
    ) <= 1.5,
    `step card not centered in onboarding column at ${width}`,
  );
  assert.equal(layout.logo.alt, 'Hyprial', `brand mark alt at ${width}`);
  assert.equal(layout.logo.width, 40, `brand mark width at ${width}`);
  assert.equal(
    layout.styles.stepRadius,
    '999px',
    `v5 current step pill radius at ${width}`,
  );
  assert.equal(
    layout.styles.stepBackground,
    'rgb(255, 255, 255)',
    `v5 current step surface at ${width}`,
  );
  assert.equal(
    layout.styles.stepWeight,
    '600',
    `v5 current step weight at ${width}`,
  );
  assert.equal(
    layout.styles.stepFontSize,
    width <= 640 ? '12px' : '13px',
    `v5 step label size at ${width}`,
  );
  assert.equal(
    layout.styles.cardRadius,
    width <= 640 ? '20px' : '28px',
    `v13 card radius at ${width}`,
  );
  assert.equal(
    layout.styles.cardBackground,
    'rgb(255, 255, 255)',
    `v5 card surface at ${width}`,
  );
  assert.equal(
    layout.styles.cardPadding,
    width <= 640 ? '20px' : '32px',
    `v17 card padding at ${width}`,
  );
  assert.equal(
    layout.styles.titleWeight,
    '600',
    `v5 title weight at ${width}`,
  );
  assert.equal(
    layout.styles.titleSize,
    width <= 640 ? '24px' : '30px',
    `v13 title size at ${width}: ${layout.styles.titleSize}`,
  );
  // BUTTON-005：恢复精确常态色断言（不接受 hover 色），测量前已移开指针。
  assert.equal(
    layout.styles.primaryBackground,
    'rgb(23, 19, 31)',
    `v13 primary button exact ink at ${width}: ${layout.styles.primaryBackground}`,
  );
  assert.equal(
    layout.styles.primaryRadius,
    '999px',
    `v17 primary button radius at ${width}`,
  );
  // 按钮高度不再作为本脚本断言（几何不在本脚本验收范围）
  assert.equal(
    layout.styles.secondaryBackground,
    null,
    `no obsolete registration mode control at ${width}`,
  );

  return layout;
}

async function assertDebugPlacement(page) {
  const placement = await page.evaluate(() => {
    const stage = document.querySelector('.dev01-v17-stage');
    const debug = document.querySelector('.dev01-v17-debug');
    const card = document.querySelector('[data-step]');
    const returnButton = debug
      ? Array.from(debug.querySelectorAll('button')).find(
          (button) => button.textContent?.trim() === '返回设置',
        ) ?? null
      : null;
    const afterStage =
      stage && debug
        ? Boolean(
            stage.compareDocumentPosition(debug) &
              Node.DOCUMENT_POSITION_FOLLOWING,
          )
        : false;
    return {
      afterStage,
      outsideCard: Boolean(debug && card && !card.contains(debug)),
      hasReturn: returnButton !== null,
      tipLabel: debug?.querySelector('button[aria-label="返回设置的说明"]') ? '返回设置的说明' : '',
      note: debug?.querySelector('[role="tooltip"]')?.textContent?.trim() ?? '',
    };
  });

  assert.equal(placement.afterStage, true, 'diagnostics must follow the stage');
  assert.equal(placement.outsideCard, true, 'diagnostics must sit outside the card');
  assert.equal(placement.hasReturn, true, 'missing debug-area return action');
  assert.equal(placement.tipLabel, '返回设置的说明', 'return-action info tip');
  assert.match(placement.note, /不会保存/);
  assert.match(placement.note, /从账号步骤重新开始/);

  return placement;
}

async function assertKeyboardOrder(page) {
  await page.evaluate(() => {
    document.body.tabIndex = -1;
    document.body.focus();
  });

  const labels = [];
  for (let index = 0; index < 14; index += 1) {
    await page.keyboard.press('Tab');
    labels.push(await page.evaluate(activeLabel));
  }

  const returnIndex = labels.findIndex((label) => label === '返回设置');
  const loginIndex = labels.findIndex((label) => label === '登录 Hyprial');
  assert.ok(loginIndex >= 0, `missing login action: ${labels.join(' | ')}`);
  if (returnIndex >= 0) {
    assert.ok(returnIndex > loginIndex, `exit precedes login: ${labels.join(' | ')}`);
  }
  assert.ok(
    !labels.some(
      (label) =>
        label.includes('创建 Squire') ||
        label.includes('初始化当前设备') ||
        label.includes('继续到设备'),
    ),
    `locked-step control received focus: ${labels.join(' | ')}`,
  );

  return labels;
}

async function runWidth(browser, server, width, screenshotDir) {
  const context = await browser.newContext({
    viewport: { width, height: 900 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  await page.addInitScript(() => {
    const probe = { fetch: 0, xhr: 0, storageWrites: 0 };
    window.__dev01Probe = probe;
    const originalFetch = window.fetch.bind(window);
    window.fetch = (...args) => {
      probe.fetch += 1;
      return originalFetch(...args);
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (...args) {
      probe.xhr += 1;
      return originalOpen.apply(this, args);
    };
    const originalSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (...args) {
      probe.storageWrites += 1;
      return originalSetItem.apply(this, args);
    };
  });

  try {
    await page.goto(
      `${server.url}/src/dev/onboarding-preview/preview.html?chat=success`,
      { waitUntil: 'domcontentloaded' },
    );
    await page.getByRole('heading', { name: '登录' }).waitFor();
    await page.getByText('隔离预览场景', { exact: true }).waitFor();

    const initialLayout = await assertLayout(page, width);
    const debugPlacement = await assertDebugPlacement(page);
    const keyboardLabels = await assertKeyboardOrder(page);

    const screenshots = [];
    if (screenshotDir) {
      await resetScroll(page);
      const path = resolve(screenshotDir, `${width}-account.png`);
      await page.screenshot({ path });
      screenshots.push(path);
    }

    const account = page.locator('[data-step="account"]');
    const device = page.locator('[data-step="device"]');
    await account.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
    await account.getByText('账号已登录').waitFor();
    await account.getByRole('button', { name: '继续到设备' }).click();
    await device.getByRole('button', { name: '返回上一步' }).click();
    await account.getByText('账号已登录').waitFor();
    await account.getByRole('button', { name: '继续到设备' }).click();
    await device.getByRole('button', { name: '返回上一步' }).click();
    await page.reload();
    await page.getByRole('heading', { name: '登录' }).waitFor();
    assert.equal(
      await page.getByRole('button', { name: '继续到设备' }).count(),
      0,
      `${width}: refresh must not restore a fixture-only login`,
    );
    assert.deepEqual(
      await page.evaluate(() => ({
        local: localStorage.length,
        session: sessionStorage.length,
      })),
      { local: 0, session: 0 },
      `${width}: refresh must not persist profile state`,
    );

    await account.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
    await account.getByText('账号已登录').waitFor();
    await account.getByRole('button', { name: '继续到设备' }).click();

    await device.getByRole('button', { name: '初始化当前设备' }).click();
    await device.getByText('设备已就绪').waitFor();
    if (screenshotDir) {
      const path = resolve(screenshotDir, `${width}-device.png`);
      await page.screenshot({ path });
      screenshots.push(path);
    }
    await device.getByRole('button', { name: '继续到运行配置' }).click();
    await page.waitForFunction(
      () =>
        document.activeElement?.tagName === 'H2' &&
        document.activeElement.textContent === '运行配置',
    );

    const model = page.locator('[data-step="model"]');
    await model.getByRole('heading', { name: '创建 Agent' }).waitFor();
    await model.getByLabel('名称').fill('squire-preview');
    await model.getByLabel('Key').click();
    await page.getByRole('option', { name: /DeepSeek/ }).click();
    await model.getByLabel('模型').click();
    await page.getByRole('option', { name: 'deepseek-flash' }).click();
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    });
    await resetScroll(page);
    if (screenshotDir) {
      const path = resolve(screenshotDir, `${width}-model.png`);
      await page.screenshot({ path, fullPage: true });
      screenshots.push(path);
    }
    await model.getByRole('button', { name: '创建 Agent' }).click();
    await page.waitForFunction(
      () =>
        document.activeElement?.tagName === 'H2' &&
        document.activeElement.textContent === '首次对话',
    );

    const conversation = page.locator('[data-step="conversation"]');
    await conversation.getByText('隔离数据来源').waitFor();
    const composer = conversation.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    await composer.fill('预览第一条消息，走契约模拟发送');
    await conversation.getByRole('button', { name: '发送', exact: true }).click();
    const log = conversation.getByRole('log', { name: '与 Fixture Squire 的消息' });
    await log.locator('[data-author="user"]', { hasText: '预览第一条消息，走契约模拟发送' }).first().waitFor();
    assert.equal(await log.locator('[data-author="user"]').count(), 2);
    assert.equal(await log.locator('[data-author="assistant"]').count(), 2);
    await conversation.getByText('消息已送达，正在进入开始页。').waitFor();
    if (screenshotDir) {
      const path = resolve(screenshotDir, `${width}-conversation.png`);
      await page.screenshot({ path });
      screenshots.push(path);
    }

    const probe = await probeBrowser(page);
    assert.equal(probe.fetch, 0, `fetch called at ${width}`);
    assert.equal(probe.xhr, 0, `XHR opened at ${width}`);
    assert.equal(probe.storageWrites, 0, `storage write at ${width}`);
    assert.equal(probe.localStorageLength, 0, `localStorage at ${width}`);
    assert.equal(probe.sessionStorageLength, 0, `sessionStorage at ${width}`);
    assert.deepEqual(pageErrors, [], `page errors at ${width}`);

    return {
      width,
      layout: initialLayout,
      debugPlacement,
      keyboardOrder: keyboardLabels,
      interactions: [
        'login',
        'device',
        'back-to-account',
        'refresh-to-account',
        'initialize',
        'runtime',
        'configure-agent',
        'conversation',
      ],
      probe,
      screenshots,
    };
  } finally {
    await context.close();
  }
}

export async function runBrowserAcceptance({
  screenshotDir: requestedScreenshotDir = null,
  widths = VIEWPORT_WIDTHS,
} = {}) {
  const executablePath = findBrowserExecutable();
  if (!executablePath) {
    throw new Error(
      `No Playwright Chromium executable found. Checked: ${BROWSER_CANDIDATES.join(', ')}`,
    );
  }
  const screenshotDir = requestedScreenshotDir
    ? resolve(requestedScreenshotDir)
    : null;
  if (screenshotDir) {
    await mkdir(screenshotDir, { recursive: true });
  }

  const font = await createCjkFontEnvironment();
  let server;
  let browser;
  try {
    server = await startGuiServer({
      mode: 'development',
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    });
    browser = await chromium.launch({
      executablePath,
      headless: true,
      args: ['--disable-dev-shm-usage'],
      env: font.env,
    });

    const results = [];
    for (const width of widths) {
      results.push(await runWidth(browser, server, width, screenshotDir));
    }

    const report = {
      executablePath,
      browserVersion: browser.version(),
      font: {
        isolated: font.isolated,
        fontPath: font.fontPath,
        fontConfigPath: font.fontConfigPath,
      },
      viewports: results,
      screenshotDir,
    };
    if (screenshotDir) {
      await writeFile(
        resolve(screenshotDir, 'report.json'),
        `${JSON.stringify(report, null, 2)}\n`,
        'utf8',
      );
    }
    return report;
  } finally {
    await browser?.close();
    await server?.close();
  }
}

const invokedDirectly =
  process.argv[1] !== undefined &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (invokedDirectly) {
  runBrowserAcceptance({
    screenshotDir: process.env.DEV01_SCREENSHOT_DIR ?? null,
  })
    .then((report) => {
      console.log(JSON.stringify(report, null, 2));
    })
    .catch((error) => {
      console.error(error);
      process.exitCode = 1;
    });
}
