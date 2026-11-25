/** Development-entry overlay interaction contract (WP6).
 *
 * Decisions recorded here (docs/design/gui-dev-product-parity-2026-11-09.md §2.5–2.6, §5 WP6):
 * - The old assertions on `.dev-preview-frame`, the persistent data-source notice
 *   and the module index are replaced by the independent floating tools contract
 *   (WP2 implementation hooks): the tools layer must live outside `#root`, stay
 *   collapsed by default, keep the ball inside the viewport and must not cover or
 *   move product controls; persistent dev chrome must be gone from the product tree.
 * - The interactive-control reachability sweep keeps the historical business
 *   coverage (all pages, account menu pages, combobox/field/click semantics and
 *   the Radix focus-restore behavior) on the development entry, entered with
 *   `hyprialFirstRun=false` because the fixture-composition wizard conversation
 *   step is deliberately blocked (no mock send). Controls that sit entirely
 *   outside the viewport by design are keyboard-first (the skip link): the
 *   sweep checks pointer reachability only for in-viewport controls and the
 *   skip link gets an explicit Tab/Enter keyboard-flow assertion instead.
 * - The sweep enters with `j1=account-success;j2=m04;j5=m11`: a destination exists
 *   only when the composition has a source for it, and this scenario supplies an
 *   Agent seed, an org source and the Agent 设置 section ports, so every page of
 *   the sweep and the 管理 Agent entry of the chat page are present.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright-core';
import { browserExecutable } from './support/browser.mjs';
import {
  DEV_TOOLS_LAYER_SELECTOR,
  DEV_TOOLS_TOGGLE_SELECTOR,
  assertNoPersistentDevChromeInProductRoot,
  openParityPage,
  parityUrl,
  probeDevTools,
  startParityDevServer,
} from './support/dev-parity-browser.mjs';

// The dev-overlay composition is part of this gate's contract; it is enabled here
// so the gate never depends on an operator shell exporting HYPRIAL_GUI_DEV_PREVIEW.
process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';
process.env.HYPRIAL_DEV_MODULE_LABELS = '1';

const CHROME_PATH = browserExecutable();
const VIEWPORT_WIDTHS = [375, 760, 800, 900, 1440];
const ACCOUNT_TRIGGER = '我的账号与 Agent 设置菜单';
const INTERACTIVE_SELECTOR = [
  'button:not(:disabled)',
  'a[href]',
  'input:not(:disabled)',
  'select:not(:disabled)',
  'textarea:not(:disabled)',
  '[role="button"]:not([aria-disabled="true"])',
  '[role="menuitem"]:not([aria-disabled="true"])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

let browser;
let server;

before(async () => {
  server = await startParityDevServer();
  browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: true,
  });
});

after(async () => {
  await browser?.close();
  await server?.close();
});

async function openAccountPage(page, name) {
  await page
    .getByRole('button', { name: ACCOUNT_TRIGGER, exact: true })
    .click({ timeout: 2_000 });
  await page.getByRole('menuitem', { name, exact: true }).click({ timeout: 2_000 });
}

const PAGES = [
  {
    name: '开始',
    open: async () => undefined,
  },
  {
    name: '聊天',
    open: (page) => page.getByRole('button', { name: '聊天', exact: true }).click({ timeout: 2_000 }),
  },
  {
    name: 'Agent',
    open: (page) => page.getByRole('button', { name: 'Agent', exact: true }).click({ timeout: 2_000 }),
  },
  {
    name: '组织',
    open: (page) => page.getByRole('button', { name: '组织', exact: true }).click({ timeout: 2_000 }),
  },
  {
    name: '设备',
    open: (page) => page.getByRole('button', { name: '设备', exact: true }).click({ timeout: 2_000 }),
  },
  {
    name: 'Agent 设置',
    open: (page) => openAccountPage(page, 'Agent 设置'),
  },
  {
    name: '用户设置',
    open: (page) => openAccountPage(page, '用户设置'),
  },
];

async function probeControl(control) {
  return control.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    for (const yRatio of [0.2, 0.5, 0.8]) {
      for (const xRatio of [0.2, 0.5, 0.8]) {
        const x = bounds.width * xRatio;
        const y = bounds.height * yRatio;
        const candidate = document.elementFromPoint(
          bounds.left + x,
          bounds.top + y,
        );
        if (candidate !== null && element.contains(candidate)) {
          return { point: { x, y } };
        }
      }
    }
    const overlays = [...document.querySelectorAll('*')]
      .filter((node) => {
        if (!(node instanceof HTMLElement)) return false;
        if (node.matches('nav.nav') || node.contains(element) || element.contains(node)) return false;
        const rect = node.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        const intersects =
          rect.left < bounds.right &&
          rect.right > bounds.left &&
          rect.top < bounds.bottom &&
          rect.bottom > bounds.top;
        if (!intersects) return false;
        const style = getComputedStyle(node);
        return (
          style.position === 'fixed' ||
          style.position === 'absolute'
        );
      })
      .map((node) => {
        const rect = node.getBoundingClientRect();
        return `${node.tagName.toLowerCase()}.${String(node.className)}[${Math.round(rect.left)},${Math.round(rect.top)} ${Math.round(rect.width)}x${Math.round(rect.height)} ${getComputedStyle(node).position}]`;
      });
    const center = document.elementFromPoint(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
    );
    return {
      failure: {
        control: `${element.tagName.toLowerCase()} ${
          element.getAttribute('aria-label') ??
          element.textContent?.trim().slice(0, 40) ??
          ''
        }`,
        rect: `${Math.round(bounds.left)},${Math.round(bounds.top)} ${Math.round(bounds.width)}x${Math.round(bounds.height)}`,
        center: center
          ? `${center.tagName.toLowerCase()}.${String(center.className)}`
          : 'none',
        overlays,
      },
    };
  });
}

async function assertControlOperable(page, control, width, pageName, index) {
  if (await control.evaluate((element) => element.classList.contains('skip-link'))) {
    await page.getByRole('button', { name: '开始', exact: true }).focus();
    await page.keyboard.press('Shift+Tab');
  }
  const probe = await probeControl(control);
  assert.equal(
    probe.failure,
    undefined,
    `${pageName} control ${index} is covered at ${width}px: ${JSON.stringify(probe.failure)}`,
  );

  const tagName = await control.evaluate((element) =>
    element.tagName.toLowerCase(),
  );
  const role = await control.getAttribute('role');
  if (role === 'combobox') {
    const trigger = await control.elementHandle();
    await trigger.click({ position: probe.point, timeout: 2_000 });
    await page.getByRole('listbox').waitFor({ state: 'visible' });
    assert.equal(await trigger.getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Escape');
    await page.getByRole('listbox').waitFor({ state: 'hidden' });
    assert.equal(await control.getAttribute('aria-expanded'), 'false');
    await page.waitForFunction((element) => document.activeElement === element, trigger, { timeout: 2_000 });
    assert.equal(await trigger.evaluate((element) => document.activeElement === element), true);
    await trigger.dispose();
    return;
  }
  const isField =
    tagName === 'input' || tagName === 'select' || tagName === 'textarea';

  if (isField) {
    await control.click({ position: probe.point, timeout: 2_000 });
    const focused = await control.evaluate(
      (element) => document.activeElement === element,
    );
    assert.equal(
      focused,
      true,
      `${pageName} control ${index} did not receive focus at ${width}px`,
    );
    if (tagName === 'select') {
      await page.keyboard.press('Escape');
    }
    return;
  }

  const isClickControl =
    tagName === 'button' ||
    tagName === 'a' ||
    role === 'button' ||
    role === 'menuitem';
  if (isClickControl) {
    await control.evaluate((element) => {
      element.dataset.probeClicked = 'false';
      element.addEventListener(
        'click',
        (event) => {
          event.preventDefault();
          event.stopImmediatePropagation();
          element.dataset.probeClicked = 'true';
        },
        { capture: true, once: true },
      );
    });
    await control.click({ position: probe.point, timeout: 2_000 });
    const received = await control.evaluate(
      (element) => element.dataset.probeClicked === 'true',
    );
    assert.equal(
      received,
      true,
      `${pageName} control ${index} did not receive the click at ${width}px`,
    );
    return;
  }

  await control.evaluate((element) => element.focus());
  const focused = await control.evaluate(
    (element) => document.activeElement === element,
  );
  assert.equal(
    focused,
    true,
    `${pageName} control ${index} is not keyboard focusable at ${width}px`,
  );
}

async function assertEveryControlReachable(page, width, pageName) {
  const viewport = page.viewportSize();
  const controls = page.locator(`#root ${INTERACTIVE_SELECTOR}`);
  const count = await controls.count();
  for (let index = 0; index < count; index += 1) {
    const control = controls.nth(index);
    if (!(await control.isVisible())) {
      continue;
    }
    const box = await control.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right };
    });
    if (box.bottom <= 0 || box.top >= viewport.height || box.right <= 0 || box.left >= viewport.width) {
      continue;
    }
    await control.evaluate((element) =>
      element.scrollIntoView({ block: 'center', inline: 'center' }),
    );
    await assertControlOperable(page, control, width, pageName, index);
  }
}

async function assertSkipLinkKeyboardFlow(page, width, pageName) {
  await page.keyboard.press('Tab');
  const info = await page.evaluate(() => {
    const active = document.activeElement;
    const isSkip = active instanceof HTMLAnchorElement && active.className.includes('skip-link');
    const rect = active.getBoundingClientRect();
    return {
      isSkip,
      top: Math.round(rect.top),
      inViewport: rect.top >= 0 && rect.bottom <= window.innerHeight,
    };
  });
  assert.equal(
    info.isSkip,
    true,
    `${pageName}: first Tab must focus the skip link at ${width}px`,
  );
  assert.equal(
    info.inViewport,
    true,
    `${pageName}: skip link must become visible inside the viewport when focused at ${width}px (top=${info.top})`,
  );
  await page.keyboard.press('Enter');
  const jumped = await page.evaluate(() =>
    document.activeElement?.id === 'main-content' || location.hash === '#main-content');
  assert.equal(
    jumped,
    true,
    `${pageName}: activating the skip link must move focus to main content at ${width}px`,
  );
}

async function assertFloatingBallContract(page, width, pageName) {
  await assertNoPersistentDevChromeInProductRoot(page, `${pageName} ${width}px`);
  const probe = await probeDevTools(page);
  assert.equal(
    probe.toolsRootPresent,
    true,
    `${pageName}: dev entry must mount the floating tools layer ${DEV_TOOLS_LAYER_SELECTOR} at ${width}px`,
  );
  assert.equal(
    probe.toolsOutsideProductRoot,
    true,
    `${pageName}: floating tools layer must stay outside #root at ${width}px`,
  );
  assert.equal(
    probe.toggleVisible,
    true,
    `${pageName}: floating ball must stay visible at ${width}px`,
  );
  assert.equal(
    probe.panelVisible,
    false,
    `${pageName}: tools panel must stay collapsed by default at ${width}px`,
  );
  assert.equal(
    probe.toggleAriaExpanded,
    'false',
    `${pageName}: floating ball must report aria-expanded=false when collapsed at ${width}px`,
  );
  const toggleRect = await page.locator(DEV_TOOLS_TOGGLE_SELECTOR).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
    };
  });
  const viewport = page.viewportSize();
  assert.equal(
    toggleRect.left >= 0 && toggleRect.top >= 0 &&
      toggleRect.right <= viewport.width && toggleRect.bottom <= viewport.height,
    true,
    `${pageName}: floating ball must stay inside the viewport at ${width}px (rect ${JSON.stringify(toggleRect)})`,
  );
}

for (const width of VIEWPORT_WIDTHS) {
  test(
    `${width}px development entry keeps product controls reachable with floating tools`,
    { concurrency: true },
    async () => {
      const page = await browser.newPage({
        viewport: { width, height: 900 },
        deviceScaleFactor: 1,
      });
      await page.goto(parityUrl(server.url, { scenario: 'j1=account-success;j2=m04;j5=m11', firstRun: false }), { waitUntil: 'networkidle' });
      await page.locator('.nav-item[data-nav="start"]').waitFor({ timeout: 15_000 });

      for (const entry of PAGES) {
        await entry.open(page);
        await assertFloatingBallContract(page, width, entry.name);
        if (entry.name === '开始') {
          await assertSkipLinkKeyboardFlow(page, width, entry.name);
        }
        await assertEveryControlReachable(page, width, entry.name);

        if (entry.name === '聊天') {
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
        }
      }

      await page.close();
    },
  );
}
