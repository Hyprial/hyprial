/** WP6 dual-entry browser parity gate.
 *
 * Runs the SAME mock inputs, routes, interactions and viewports against the
 * development entry and the `dist-parity` test artifact, then asserts identical
 * normalized `#root` DOM, key layout and route. The firstRun=true matrix walks
 * the first-run wizard in lockstep (its conversation step is deliberately
 * blocked in fixture composition — no mock send), the firstRun=false matrix
 * compares the full product pages and interaction results. Failures are
 * collected per page so one run records every remaining parity problem.
 * Product actions use an authenticated archived fixture. Archive selectors
 * open a separate page and must not change the active empty journey.
 * The account-menu sequence watches the 用户设置 item: Agent 设置 exists only when
 * the composition has a port for one of its sections, which the product
 * scenario of this matrix does not supply.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright-core';
import { browserExecutable } from './support/browser.mjs';
import {
  DEV_TOOLS_LAYER_SELECTOR,
  DEV_TOOLS_PANEL_SELECTOR,
  DEV_TOOLS_TOGGLE_SELECTOR,
  DEFAULT_FIXTURE_SCENARIO,
  assertFloatingToolsAbsent,
  assertNoPersistentDevChromeInProductRoot,
  captureParitySnapshot,
  openParityPage,
  parityUrl,
  probeDevTools,
  snapshotMismatches,
  startParityServers,
  waitForVisible,
} from './support/dev-parity-browser.mjs';

const VIEWPORT_WIDTHS = [375, 760, 1440];
const WIZARD_ENTRY_BUTTON = '登录 Hyprial';
const ACCOUNT_TRIGGER = '我的账号与 Agent 设置菜单';
const PRODUCT_SCENARIO = 'j1=account-ready;j2=m04;j5=m11';

let browser;
let servers;

before(async () => {
  servers = await startParityServers();
  browser = await chromium.launch({ executablePath: browserExecutable(), headless: true });
});

after(async () => {
  await browser?.close();
  await servers?.dev?.close();
  await servers?.production?.close();
});

async function openBothEntries({ width, firstRun, scenario = DEFAULT_FIXTURE_SCENARIO }) {
  const viewport = { width, height: 900 };
  const dev = await openParityPage(browser, {
    url: parityUrl(servers.dev.url, { scenario, firstRun }),
    viewport,
  });
  const production = await openParityPage(browser, {
    url: parityUrl(servers.production.url, { scenario, firstRun }),
    viewport,
  });
  return { dev, production };
}

async function closeEntry(entry) {
  await entry.context.close();
}

async function navigateAndSettle(page, navId, navName) {
  await page.getByRole('button', { name: navName, exact: true }).click({ timeout: 5_000 });
  await page.waitForFunction(
    (id) => document.querySelector(`.nav-item[data-nav="${id}"]`)?.getAttribute('aria-current') === 'page',
    navId,
    { timeout: 5_000 },
  );
}

async function orgTab(page, name) {
  const tab = page.getByRole('tab', { name, exact: true });
  if (await tab.isVisible()) await tab.click();
  else {
    await page.locator('#org-sub-narrow').click();
    await page.getByRole('option', { name, exact: true }).click();
  }
}

async function chatDraftSequence(page) {
  const composer = page.getByRole('textbox', { name: /Squire 的消息/ });
  await composer.fill('等价门草稿 <b>加粗</b>');
  await navigateAndSettle(page, 'start', '开始');
  await navigateAndSettle(page, 'chat', '聊天');
  return composer.inputValue();
}

async function agentCreateSequence(page) {
  await page.getByRole('button', { name: '创建 Agent', exact: true }).click({ timeout: 5_000 });
  await page.getByRole('heading', { level: 2, name: '创建 Agent', exact: true }).waitFor({ timeout: 5_000 });
  await page.getByRole('navigation', { name: '面包屑' }).getByRole('link', { name: 'Agent', exact: true }).click({ timeout: 5_000 });
  await page.getByRole('heading', { level: 2, name: '创建 Agent', exact: true }).waitFor({ state: 'hidden', timeout: 5_000 });
}

async function accountMenuSequence(page) {
  await page.getByRole('button', { name: ACCOUNT_TRIGGER, exact: true }).click({ timeout: 5_000 });
  await page.getByRole('menuitem', { name: '用户设置', exact: true }).waitFor({ timeout: 5_000 });
  await page.keyboard.press('Escape');
  await page.getByRole('menuitem', { name: '用户设置', exact: true }).waitFor({ state: 'hidden', timeout: 5_000 });
}

async function openSettingsPage(page) {
  await page.getByRole('button', { name: ACCOUNT_TRIGGER, exact: true }).click({ timeout: 5_000 });
  await page.getByRole('menuitem', { name: '用户设置', exact: true }).click({ timeout: 5_000 });
  await page.getByRole('menuitem', { name: '用户设置', exact: true }).waitFor({ state: 'hidden', timeout: 5_000 });
}

const PARITY_PAGES = [
  { name: '开始', nav: ['start', '开始'], interact: null },
  { name: '聊天', nav: ['chat', '聊天'], interact: chatDraftSequence },
  { name: 'Agent', nav: ['agents', 'Agent'], interact: agentCreateSequence },
  { name: '组织', nav: ['org', '组织'], interact: (page) => orgTab(page, '成员') },
  { name: '用户设置', nav: null, open: openSettingsPage, interact: accountMenuSequence },
];

async function collectWizardHidden(page, label, failures) {
  const wizardButton = page.getByRole('button', { name: WIZARD_ENTRY_BUTTON, exact: true });
  const visible = await wizardButton.isVisible().catch(() => false);
  if (visible) {
    const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 400));
    failures.push(`${label}: first-run wizard must stay hidden when firstRunWizardEnabled=false, but the wizard entry button is visible\nbody excerpt: ${JSON.stringify(bodyText)}`);
  }
}

async function runProductMatrix({ width, scenario = PRODUCT_SCENARIO }) {
  const label = `width=${width} firstRun=false`;
  const failures = [];
  const { dev, production } = await openBothEntries({ width, firstRun: false, scenario });
  try {
    await collectWizardHidden(dev.page, `dev ${label}`, failures);
    await collectWizardHidden(production.page, `production ${label}`, failures);
    for (const parityPage of PARITY_PAGES) {
      const pageLabel = `${parityPage.name} ${label}`;
      try {
        if (parityPage.open) {
          await parityPage.open(dev.page);
          await parityPage.open(production.page);
        } else {
          await navigateAndSettle(dev.page, parityPage.nav[0], parityPage.nav[1]);
          await navigateAndSettle(production.page, parityPage.nav[0], parityPage.nav[1]);
        }
        const devSnapshot = await captureParitySnapshot(dev.page);
        const productionSnapshot = await captureParitySnapshot(production.page);
        failures.push(...snapshotMismatches(devSnapshot, productionSnapshot, `${pageLabel} initial view`));
        if (!(await dev.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))) {
          failures.push(`${pageLabel}: dev entry overflows horizontally`);
        }
        if (!(await production.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))) {
          failures.push(`${pageLabel}: production entry overflows horizontally`);
        }
        if (parityPage.interact) {
          const devResult = await parityPage.interact(dev.page);
          const productionResult = await parityPage.interact(production.page);
          if (JSON.stringify(productionResult) !== JSON.stringify(devResult)) {
            failures.push(`${pageLabel}: interaction result differs — dev=${JSON.stringify(devResult)} production=${JSON.stringify(productionResult)}`);
          }
          const devAfter = await captureParitySnapshot(dev.page);
          const productionAfter = await captureParitySnapshot(production.page);
          failures.push(...snapshotMismatches(devAfter, productionAfter, `${pageLabel} after interaction`));
        }
      } catch (error) {
        failures.push(`${pageLabel}: ${error.message?.split('\n').slice(0, 3).join(' | ') ?? error}`);
      }
    }
  } finally {
    await closeEntry(dev);
    await closeEntry(production);
  }
  assert.deepEqual(failures, [], `${label}: ${failures.length} parity problems\n${failures.join('\n-----\n')}`);
}

async function waitForDomStable(page, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  let previous = (await captureParitySnapshot(page)).dom;
  while (Date.now() < deadline) {
    await page.waitForTimeout(400);
    const current = (await captureParitySnapshot(page)).dom;
    if (current === previous) return;
    previous = current;
  }
  throw new Error('wizard step DOM did not settle before the parity snapshot');
}

const WIZARD_STEPS = [
  {
    name: '登录',
    marker: '设备',
    act: (page) => page.getByRole('button', { name: '继续到设备', exact: true }).click({ timeout: 8_000 }),
    prepare: async (page) => {
      await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click({ timeout: 8_000 });
    },
  },
  {
    name: '设备',
    marker: '模型服务',
    act: (page) => page.getByRole('button', { name: '继续到运行配置', exact: true }).click({ timeout: 8_000 }),
    prepare: async (page) => {
      await page.getByRole('button', { name: '初始化当前设备', exact: true }).click({ timeout: 8_000 });
    },
  },
  {
    name: '模型服务',
    marker: '首次对话',
    act: async (page) => {
      const key = page.getByLabel('DeepSeek API Key', { exact: false });
      await key.fill('preview-only-key');
      await page.getByRole('button', { name: '保存并继续', exact: true }).click({ timeout: 8_000 });
    },
    settle: (page) => waitForDomStable(page),
  },
];

async function assertRuntimeScroll(page, width) {
  await page.bringToFront();
  await page.setViewportSize({ width, height: 320 });
  const content = page.getByRole('main');
  await content.evaluate(element => { element.scrollTop = 0; });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.getByRole('heading', { name: '模型服务', exact: true }).focus();
  await page.keyboard.press('PageDown');
  await page.waitForFunction(() => {
    const main = document.querySelector('.app-first-run main');
    return main && main.scrollTop > 0;
  }, undefined, { timeout: 3000 });
  await page.keyboard.press('End');
  const submit = page.getByRole('button', { name: '保存并继续', exact: true });
  await page.waitForFunction(() => {
    const main = document.querySelector('.app-first-run main');
    return main && main.scrollTop + main.clientHeight >= main.scrollHeight - 1;
  });
  const bounds = await submit.boundingBox();
  assert.ok(bounds && bounds.y >= 0 && bounds.y + bounds.height <= 320,
    'runtime submission must be reachable by keyboard scrolling');
  await page.setViewportSize({ width, height: 900 });
  await content.evaluate(element => { element.scrollTop = 0; });
}

async function runWizardMatrix({ width, scenario = DEFAULT_FIXTURE_SCENARIO }) {
  const label = `width=${width} firstRun=true`;
  const failures = [];
  const { dev, production } = await openBothEntries({ width, firstRun: true, scenario });
  try {
    for (const entry of [dev, production]) {
      await waitForVisible(
        entry.page,
        entry.page.getByRole('button', { name: WIZARD_ENTRY_BUTTON, exact: true }),
        8_000,
        `${label}: the same first-run wizard must open on both entries when firstRunWizardEnabled=true`,
      );
    }
    for (const step of WIZARD_STEPS) {
      const stepLabel = `wizard ${step.name} ${label}`;
      try {
        for (const entry of [dev, production]) {
          if (step.prepare) await step.prepare(entry.page);
          await step.act(entry.page);
          await entry.page.getByRole('heading', { name: step.marker, exact: true }).waitFor({ timeout: 15_000 });
          if (step.name === '设备') await assertRuntimeScroll(entry.page, width);
          if (step.settle) await step.settle(entry.page);
        }
        failures.push(...snapshotMismatches(
          await captureParitySnapshot(dev.page),
          await captureParitySnapshot(production.page),
          stepLabel,
        ));
      } catch (error) {
        failures.push(`${stepLabel}: ${error.message?.split('\n').slice(0, 3).join(' | ') ?? error}`);
        break;
      }
    }
  } finally {
    await closeEntry(dev);
    await closeEntry(production);
  }
  assert.deepEqual(failures, [], `${label}: ${failures.length} parity problems\n${failures.join('\n-----\n')}`);
}

async function expandTools(page) {
  await page.locator(DEV_TOOLS_TOGGLE_SELECTOR).click({ timeout: 5_000 });
  await page.locator(DEV_TOOLS_PANEL_SELECTOR).waitFor({ state: 'visible', timeout: 5_000 });
}

async function collapseTools(page) {
  await page.locator(DEV_TOOLS_PANEL_SELECTOR).waitFor({ state: 'hidden', timeout: 5_000 });
}

async function productRootRect(page) {
  return page.evaluate(() => {
    const element = document.getElementById('root');
    const rect = element.getBoundingClientRect();
    return [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)];
  });
}

async function floatingToolJourney(page, width) {
  const label = `floating tools width=${width}`;
  const failures = [];
  await page.locator(DEV_TOOLS_TOGGLE_SELECTOR).waitFor({ state: 'visible', timeout: 5_000 });
  await assertNoPersistentDevChromeInProductRoot(page, label);
  const collapsed = await probeDevTools(page);
  if (!collapsed.toolsRootPresent) failures.push(`${label}: dev entry must mount ${DEV_TOOLS_LAYER_SELECTOR}`);
  if (collapsed.toolsOutsideProductRoot !== true) failures.push(`${label}: tools layer must stay outside #root`);
  if (!collapsed.toggleVisible) failures.push(`${label}: floating ball must be visible when collapsed`);
  if (collapsed.panelVisible) failures.push(`${label}: tools panel must be collapsed by default`);
  if (collapsed.toggleAriaExpanded !== 'false') failures.push(`${label}: toggle aria-expanded must be false when collapsed (got ${JSON.stringify(collapsed.toggleAriaExpanded)})`);
  const collapsedProductRect = await productRootRect(page);
  const viewport = page.viewportSize();
  const toggleRect = await page.locator(DEV_TOOLS_TOGGLE_SELECTOR).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return [Math.round(rect.left), Math.round(rect.top), Math.round(rect.right), Math.round(rect.bottom)];
  });
  if (!(toggleRect[0] >= 0 && toggleRect[1] >= 0 && toggleRect[2] <= viewport.width && toggleRect[3] <= viewport.height)) {
    failures.push(`${label}: floating ball must stay inside the viewport (rect ${JSON.stringify(toggleRect)})`);
  }

  await page.locator(DEV_TOOLS_TOGGLE_SELECTOR).focus();
  await page.keyboard.press('Enter');
  await page.locator(DEV_TOOLS_PANEL_SELECTOR).waitFor({ state: 'visible', timeout: 5_000 });
  const expanded = await probeDevTools(page);
  if (!expanded.panelVisible) failures.push(`${label}: keyboard activation must expand the panel`);
  if (expanded.toggleAriaExpanded !== 'true') failures.push(`${label}: toggle aria-expanded must be true when expanded`);
  if (JSON.stringify(await productRootRect(page)) !== JSON.stringify(collapsedProductRect)) {
    failures.push(`${label}: expanding the tools must not move or resize the product #root`);
  }

  await page.keyboard.press('Escape');
  await collapseTools(page);
  const afterEscape = await probeDevTools(page);
  if (afterEscape.panelVisible) failures.push(`${label}: Escape must collapse the panel`);
  if (!afterEscape.toggleFocused) failures.push(`${label}: focus must return to the floating ball after Escape`);

  await expandTools(page);
  await page.mouse.click(5, 5);
  await collapseTools(page);
  const afterOutsideClick = await probeDevTools(page);
  if (afterOutsideClick.panelVisible) failures.push(`${label}: clicking outside must collapse the panel`);
  if (JSON.stringify(await productRootRect(page)) !== JSON.stringify(collapsedProductRect)) {
    failures.push(`${label}: tool expansion cycles must not change the product #root layout`);
  }
  assert.deepEqual(failures, [], `${label}: ${failures.length} problems\n${failures.join('\n')}`);
}

for (const width of VIEWPORT_WIDTHS) {
  test(`dual-entry parity ${width}px firstRun=false product matrix`, async () => {
    await runProductMatrix({ width });
  });
  test(`dual-entry parity ${width}px firstRun=true wizard journey`, async () => {
    await runWizardMatrix({ width });
  });
}

test('floating dev tools default collapsed, expand, Escape, outside click, focus restore at 375px', async () => {
  const dev = await openParityPage(browser, {
    url: parityUrl(servers.dev.url, { firstRun: false }),
    viewport: { width: 375, height: 900 },
  });
  try {
    await floatingToolJourney(dev.page, 375);
  } finally {
    await dev.context.close();
  }
});

test('floating dev tools default collapsed, expand, Escape, outside click, focus restore at 1440px', async () => {
  const dev = await openParityPage(browser, {
    url: parityUrl(servers.dev.url, { firstRun: false }),
    viewport: { width: 1440, height: 900 },
  });
  try {
    await floatingToolJourney(dev.page, 1440);
  } finally {
    await dev.context.close();
  }
});

test('archived tools open the same mock input separately and preserve the active journey', async () => {
  const dev = await openParityPage(browser, {
    url: parityUrl(servers.dev.url, { scenario: 'j1=logged-in-init', firstRun: false }),
    viewport: { width: 1440, height: 900 },
  });
  try {
    await navigateAndSettle(dev.page, 'start', '开始');
    const before = await captureParitySnapshot(dev.page);
    const originalUrl = dev.page.url();
    await expandTools(dev.page);
    await dev.page.getByRole('tab', { name: '归档场景', exact: true }).click();
    const scenarioSelect = dev.page.locator(DEV_TOOLS_PANEL_SELECTOR).getByLabel('J5 场景');
    await waitForVisible(
      dev.page,
      scenarioSelect,
      5_000,
      `floating tools panel must expose the J5 场景 scenario select inside ${DEV_TOOLS_PANEL_SELECTOR}`,
    );
    await scenarioSelect.selectOption('m11');
    const archiveLink = dev.page.getByRole('link', { name: '打开归档场景 ↗', exact: true });
    const archiveUrl = new URL(await archiveLink.getAttribute('href'), originalUrl);
    assert.equal(archiveUrl.searchParams.get('hyprialScenario'), 'j1=account-ready;j5=m11');
    const opened = dev.context.waitForEvent('page');
    await archiveLink.click();
    const archived = await opened;
    await archived.waitForLoadState('networkidle');
    await dev.page.keyboard.press('Escape');
    await collapseTools(dev.page);
    assert.equal(dev.page.url(), originalUrl);
    assert.deepEqual(
      snapshotMismatches(before, await captureParitySnapshot(dev.page), 'active journey after archive selection'),
      [],
      'archived selections must not replace the active journey',
    );
    await navigateAndSettle(archived, 'org', '组织');
    const after = await captureParitySnapshot(archived);
    const productionUrl = new URL(servers.production.url);
    productionUrl.search = archiveUrl.search;
    const production = await openParityPage(browser, {
      url: productionUrl.toString(),
      viewport: { width: 1440, height: 900 },
    });
    try {
      await navigateAndSettle(production.page, 'org', '组织');
      assert.deepEqual(
        snapshotMismatches(after, await captureParitySnapshot(production.page), 'scenario j5=m11 org page'),
        [],
        'the archived dev entry must equal the production entry loaded with the same scenario id',
      );
    } finally {
      await production.context.close();
    }
  } finally {
    await dev.context.close();
  }
});

test('production parity entry honors the canonical hyprialScenario and hyprialFirstRun parameters', async () => {
  const viewport = { width: 1440, height: 900 };
  const production = await openParityPage(browser, {
    url: parityUrl(servers.production.url, { scenario: 'j1=account-ready;j5=m11', firstRun: false }),
    viewport,
  });
  try {
    await waitForVisible(
      production.page,
      production.page.locator('.nav-item[data-nav="org"]'),
      8_000,
      'hyprialScenario/hyprialFirstRun must drive the parity composition on the dist-parity entry; gui/src/main.tsx (WP1) must read hyprialScenario and gate the mock entry behind __HYPRIAL_PARITY__',
    );
    await navigateAndSettle(production.page, 'org', '组织');
    const orgModel = await production.page.evaluate(() => document.querySelector('.org-workspace')?.textContent?.includes('演示组织') ?? false);
    assert.equal(
      orgModel,
      true,
      'hyprialScenario=j5=m11 must select the seeded org model on the parity entry (WP1 scenario derivation)',
    );
    const wizardVisible = [];
    await collectWizardHidden(production.page, 'canonical param firstRun=false', wizardVisible);
    assert.deepEqual(wizardVisible, [], 'hyprialFirstRun=false must hide the wizard on the parity entry');
    const bootstrapVisible = await production.page.locator('.bootstrap-page').isVisible().catch(() => false);
    assert.equal(bootstrapVisible, false, 'the parity entry must not render the bootstrap failure page');
  } finally {
    await production.context.close();
  }
});

test('dist-parity production entry mounts no development tools', async () => {
  const production = await openParityPage(browser, {
    url: parityUrl(servers.production.url, { firstRun: false }),
    viewport: { width: 1440, height: 900 },
  });
  try {
    await assertFloatingToolsAbsent(production.page, 'dist-parity 1440px');
  } finally {
    await production.context.close();
  }
});

test('persistent dev chrome is removed from the product tree on both entries', async () => {
  const { dev, production } = await openBothEntries({ width: 1440, firstRun: false });
  try {
    await assertNoPersistentDevChromeInProductRoot(dev.page, 'dev entry');
    await assertNoPersistentDevChromeInProductRoot(production.page, 'dist-parity entry');
    const devProbe = await probeDevTools(dev.page);
    assert.equal(devProbe.toolsRootPresent, true, 'dev entry must keep the floating tools layer outside #root');
  } finally {
    await closeEntry(dev);
    await closeEntry(production);
  }
});

for (const width of [375, 1440]) {
  test(`dev tools ball stays the topmost clickable element over a shown toast at ${width}px`, async () => {
    const dev = await openParityPage(browser, {
      url: parityUrl(servers.dev.url, { firstRun: false }),
      viewport: { width, height: 900 },
    });
    try {
      await dev.page.evaluate(() => {
        const host = document.createElement('div');
        host.className = 'toast-host';
        host.setAttribute('data-test-toast-host', '');
        host.innerHTML = '<div class="toast toast-warning"><span class="toast-text">导出暂不可用。</span></div>'.repeat(4);
        (document.querySelector('.app') ?? document.getElementById('root')).appendChild(host);
      });
      const geometry = await dev.page.evaluate(({ ball, host }) => {
        const fab = document.querySelector(ball).getBoundingClientRect();
        const toasts = document.querySelector(host).getBoundingClientRect();
        const x = fab.left + fab.width / 2;
        const y = fab.top + fab.height / 2;
        const top = document.elementFromPoint(x, y);
        return {
          overlaps: fab.left < toasts.right && fab.right > toasts.left && fab.top < toasts.bottom && fab.bottom > toasts.top,
          topIsBall: top?.closest(ball) !== null,
          topDescription: top ? `${top.tagName}.${top.className}` : null,
        };
      }, { ball: DEV_TOOLS_TOGGLE_SELECTOR, host: '[data-test-toast-host]' });
      assert.equal(geometry.overlaps, true, 'the toast must cover the ball position for this check to mean anything');
      assert.equal(geometry.topIsBall, true, `the ball must be topmost at its centre, found ${geometry.topDescription}`);
      await dev.page.locator(DEV_TOOLS_TOGGLE_SELECTOR).click({ timeout: 2_000 });
      await dev.page.locator(DEV_TOOLS_PANEL_SELECTOR).waitFor({ state: 'visible', timeout: 5_000 });
    } finally {
      await dev.context.close();
    }
  });
}
