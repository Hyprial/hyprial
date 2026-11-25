// C1/C5 浏览器组合探针（PR #2579 测试切片，docs/design/gui-testing-architecture-handoff-2026-11-08.md §2.1/§5）：
// 通过 Vite dev server 在随机 loopback 端口加载 tests/forms/dialog-select-entry.html（真实
// createRoot + Dialog/Field/Select/Button），不连接 daemon、账号或外部服务。逐项记录 portal
// 挂载、computed z-index、elementFromPoint、真实鼠标点击与 Esc 分层/焦点证据。约定：
// elementFromPoint 命中选项不等于视觉无遮挡，也不等于鼠标点击可用；遮挡结论以 computed
// z-index 的绘制顺序、隐藏 backdrop 前后的局部像素对比与证据截图为准：像素变化说明
// backdrop 子树在菜单之上贡献了可见像素（遮挡）；像素不变说明菜单绘制在其上。任一契约
// 检查失败即以非零退出码报告；无法启动浏览器或服务时记 BLOCKED，不降级为 jsdom、
// 不 skip。基线实测：菜单打开期间 Radix scroll-lock 会把 Dialog backdrop/panel 置为
// pointer-events:none，elementFromPoint 与真实鼠标点击因此可穿透到视觉上被遮挡的选项，
// 命中结果不构成无遮挡证据。证据文件只在 HYPRIAL_BROWSER_EVIDENCE_DIR 指向仓库外
// 目录时写入。
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { browserExecutable } from '../support/browser.mjs';

process.env.HYPRIAL_GUI_HOME = resolve(import.meta.dirname, '../..');
process.env.HYPRIAL_BACKEND_ENDPOINT = '';
delete process.env.HYPRIAL_GUI_AUTH;
delete process.env.HYPRIAL_GUI_SETUP;
delete process.env.HYPRIAL_GUI_SETUP_CLI;

const evidenceDir = process.env.HYPRIAL_BROWSER_EVIDENCE_DIR
  ? resolve(process.env.HYPRIAL_BROWSER_EVIDENCE_DIR)
  : null;

const checks = [];
const evidence = {
  contract: 'dialog-select-browser',
  viewport: { width: 1440, height: 900 },
  steps: [],
};

function record(name, pass, detail) {
  checks.push({ name, pass, detail: detail ?? null });
}

async function fullState(page) {
  return page.evaluate(() => {
    const describe = (node) => {
      if (!node) return null;
      return {
        tag: node.tagName.toLowerCase(),
        role: node.getAttribute('role'),
        contract: node.getAttribute('data-contract'),
        id: node.id || null,
        text: (node.textContent ?? '').trim().slice(0, 24) || null,
      };
    };
    const combobox = document.querySelector('[role="combobox"]');
    const active = document.activeElement;
    const read = (testId) =>
      document.querySelector(`[data-testid="${testId}"]`)?.textContent ?? null;
    return {
      listboxCount: document.querySelectorAll('[role="listbox"]').length,
      dialogCount: document.querySelectorAll('[role="dialog"]').length,
      value: read('value'),
      changeCalls: read('change-calls'),
      lastChange: read('last-change'),
      closeCalls: read('close-calls'),
      activeElement: describe(active),
      activeIsCombobox: active !== null && active === combobox,
      activeIsOpenButton:
        active instanceof Element &&
        active.getAttribute('data-contract') === 'open-button',
    };
  });
}

async function layeringState(page) {
  return page.evaluate(() => {
    const describe = (node) => {
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return {
        tag: node.tagName.toLowerCase(),
        slot: node.getAttribute('data-slot'),
        cls: node.getAttribute('class')?.slice(0, 100) ?? null,
        zIndex: getComputedStyle(node).zIndex,
        position: getComputedStyle(node).position,
        pointerEvents: getComputedStyle(node).pointerEvents,
        rect: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        },
      };
    };
    const content = document.querySelector('[data-slot="select-content"]');
    const backdrop = document.querySelector('.ui-dialog-backdrop');
    const panel = document.querySelector('.ui-dialog');
    const option =
      Array.from(document.querySelectorAll('[role="option"]')).find((node) =>
        node.textContent?.trim().includes('codex'),
      ) ?? null;
    if (!content || !backdrop || !option) {
      return { missing: true, content: describe(content), backdrop: describe(backdrop) };
    }
    const optionRect = option.getBoundingClientRect();
    const point = {
      x: optionRect.x + optionRect.width / 2,
      y: optionRect.y + optionRect.height / 2,
    };
    const hit = document.elementFromPoint(point.x, point.y);
    const controlPoint = {
      x: window.innerWidth / 2,
      y: window.innerHeight - 50,
    };
    const controlHit = document.elementFromPoint(controlPoint.x, controlPoint.y);
    const numeric = (value) => (value === 'auto' ? null : Number(value));
    const contentZ = numeric(getComputedStyle(content).zIndex);
    const backdropZ = numeric(getComputedStyle(backdrop).zIndex);
    const ancestors = (node) => {
      const chain = [];
      let current = node.parentElement;
      while (current && current !== document.body) {
        const style = getComputedStyle(current);
        chain.push({
          tag: current.tagName.toLowerCase(),
          cls: current.getAttribute('class')?.slice(0, 60) ?? null,
          zIndex: style.zIndex,
          position: style.position,
        });
        current = current.parentElement;
      }
      return chain;
    };
    let menuPaintsAboveDialog;
    if (contentZ !== null && backdropZ !== null) {
      menuPaintsAboveDialog = contentZ > backdropZ;
    } else if (contentZ !== null) {
      menuPaintsAboveDialog = contentZ > 0;
    } else if (backdropZ !== null) {
      menuPaintsAboveDialog = false;
    } else {
      menuPaintsAboveDialog = Boolean(
        backdrop.compareDocumentPosition(content) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }
    return {
      missing: false,
      content: describe(content),
      backdrop: describe(backdrop),
      panel: describe(panel),
      option: describe(option),
      contentAncestors: ancestors(content),
      backdropAncestors: ancestors(backdrop),
      hitTest: {
        point,
        hit: describe(hit),
        hitIsOptionOrInsideMenuContent:
          hit !== null && (hit === option || content.contains(hit)),
        controlPoint,
        controlHit: describe(controlHit),
      },
      menuPaintsAboveDialog,
    };
  });
}

async function visualOcclusionState(page, optionRect) {
  const clip = {
    x: optionRect.x,
    y: optionRect.y,
    width: optionRect.w,
    height: optionRect.h,
  };
  await page.evaluate(() => window.scrollTo(0, 0));
  const withBackdrop = await page.screenshot({ clip });
  await page.evaluate(() => {
    const backdrop = document.querySelector('.ui-dialog-backdrop');
    if (backdrop instanceof HTMLElement) backdrop.style.visibility = 'hidden';
  });
  const withoutBackdrop = await page.screenshot({ clip });
  await page.evaluate(() => {
    const backdrop = document.querySelector('.ui-dialog-backdrop');
    if (backdrop instanceof HTMLElement) backdrop.style.visibility = '';
  });
  return {
    clip,
    hidingBackdropChangedPixels:
      Buffer.compare(withBackdrop, withoutBackdrop) !== 0,
  };
}

async function saveShot(page, name) {
  if (!evidenceDir) return;
  mkdirSync(evidenceDir, { recursive: true });
  await page.screenshot({ path: resolve(evidenceDir, `${name}.png`) });
}

let server;
let browser;
try {
  const executable = browserExecutable();
  const { startGuiServer } = await import('../../scripts/serve.mjs');
  server = await startGuiServer({
    mode: 'development',
    host: '127.0.0.1',
    port: 0,
    strictPort: true,
  });
  browser = await chromium.launch({
    executablePath: executable,
    headless: true,
    timeout: 15_000,
  });
} catch (error) {
  console.log(
    JSON.stringify(
      {
        contract: 'dialog-select-browser',
        status: 'BLOCKED',
        reason: error instanceof Error ? error.message : String(error),
      },
      null,
      2,
    ),
  );
  process.exit(1);
}

const context = await browser.newContext({
  viewport: { width: evidence.viewport.width, height: evidence.viewport.height },
});
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', (error) => pageErrors.push(error.message));

try {
  await page.goto(`${server.url}/tests/forms/dialog-select-entry.html`, {
    waitUntil: 'networkidle',
  });

  const openButton = page.getByRole('button', { name: '打开表单' });
  await openButton.click();
  const dialog = page.getByRole('dialog', { name: '接入设置' });
  await dialog.waitFor();
  evidence.steps.push({
    step: 'dialog-opened',
    state: await fullState(page),
  });

  const trigger = page.locator('#contract-harness');
  await trigger.click();
  await page.getByRole('listbox').waitFor();
  const expanded = await trigger.getAttribute('aria-expanded');
  record('select-menu-opens', expanded === 'true', { ariaExpanded: expanded });

  const portal = await page.evaluate(() => {
    const content = document.querySelector('[data-slot="select-content"]');
    const dialogNode = document.querySelector('[role="dialog"]');
    if (!content) return null;
    const rect = content.getBoundingClientRect();
    return {
      inBody: document.body.contains(content),
      insideDialogSubtree: dialogNode ? dialogNode.contains(content) : false,
      width: rect.width,
      height: rect.height,
    };
  });
  record(
    'select-menu-renders-in-body-portal',
    portal !== null &&
      portal.inBody &&
      !portal.insideDialogSubtree &&
      portal.width > 0 &&
      portal.height > 0,
    portal,
  );

  const layering = await layeringState(page);
  const occlusion =
    layering.missing === false
      ? await visualOcclusionState(page, layering.option.rect)
      : null;
  evidence.steps.push({
    step: 'layering-while-menu-open',
    layering,
    occlusion,
  });
  await saveShot(page, 'dialog-select-menu-open');
  record(
    'c5-menu-paints-above-dialog-backdrop',
    occlusion !== null && occlusion.hidingBackdropChangedPixels === false,
    {
      missing: layering.missing ?? null,
      contentZIndex: layering.content?.zIndex ?? null,
      backdropZIndex: layering.backdrop?.zIndex ?? null,
      zIndexComparisonSaysAbove:
        layering.missing === false ? layering.menuPaintsAboveDialog : null,
      hitTest: layering.hitTest ?? null,
      occlusion,
    },
  );

  let realClick = { ok: false };
  try {
    await page.getByRole('option', { name: 'codex' }).click({ timeout: 4_000 });
    realClick.ok = true;
  } catch (error) {
    realClick.error =
      error instanceof Error ? error.message.split('\n').slice(0, 4).join(' | ') : String(error);
  }
  if (realClick.ok) {
    await page
      .waitForFunction(
        () => document.querySelector('[data-testid="value"]')?.textContent === 'codex',
        undefined,
        { timeout: 2_000 },
      )
      .catch(() => {});
    const state = await fullState(page);
    record(
      'c5-option-real-mouse-click-switches-to-codex',
      state.value === 'codex' && state.changeCalls === '1',
      state,
    );
  } else {
    record('c5-option-real-mouse-click-switches-to-codex', false, realClick);
  }

  if ((await page.locator('[role="listbox"]').count()) === 0) {
    await trigger.click();
    await page.getByRole('listbox').waitFor();
  }

  await page.keyboard.press('Escape');
  await page
    .waitForFunction(
      () => document.querySelectorAll('[role="listbox"]').length === 0,
      undefined,
      { timeout: 2_000 },
    )
    .catch(() => {});
  const firstEsc = await fullState(page);
  evidence.steps.push({ step: 'after-first-escape', state: firstEsc });
  await saveShot(page, 'dialog-select-after-first-escape');
  record(
    'c1-first-esc-closes-menu-only',
    firstEsc.listboxCount === 0 &&
      firstEsc.dialogCount === 1 &&
      firstEsc.closeCalls === '0',
    firstEsc,
  );
  record(
    'c1-first-esc-focus-returns-to-select-trigger',
    firstEsc.dialogCount === 1 && firstEsc.activeIsCombobox === true,
    { activeElement: firstEsc.activeElement },
  );

  if (firstEsc.dialogCount === 1) {
    await page.keyboard.press('Escape');
    await page
      .waitForFunction(
        () => document.querySelectorAll('[role="dialog"]').length === 0,
        undefined,
        { timeout: 2_000 },
      )
      .catch(() => {});
    const secondEsc = await fullState(page);
    evidence.steps.push({ step: 'after-second-escape', state: secondEsc });
    record(
      'c1-second-esc-closes-dialog-once-focus-to-open-button',
      secondEsc.dialogCount === 0 &&
        secondEsc.closeCalls === '1' &&
        secondEsc.activeIsOpenButton === true,
      secondEsc,
    );
  } else {
    record(
      'c1-second-esc-closes-dialog-once-focus-to-open-button',
      false,
      {
        status: 'not-reached',
        reason: '第一次 Esc 已关闭父 Dialog，第二次 Esc 无从验证（C1）',
      },
    );
  }

  record('no-page-errors', pageErrors.length === 0, pageErrors);
} finally {
  await context.close();
  await browser.close();
  await server.close();
}

const failed = checks.filter((check) => !check.pass);
const report = {
  ...evidence,
  checks,
  passed: failed.length === 0,
  failures: failed.map((check) => check.name),
};
console.log(JSON.stringify(report, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
