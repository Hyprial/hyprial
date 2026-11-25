// @vitest-environment node
// implS2 (2026-11-02, second submission)：真实浏览器 + Contract Mock 驱动 Agent 详情页签的接线态，1440×900 截图留证。
// 2026-11-15（PR 1e-3）：改走正式入口——tests/support/contract-mock-gui.mjs 启动 Contract Mock（success）与
// 会话宿主，页面经 launchUrl() 取得会话；不再使用 tests/reference/entry.html 与其 slice=agents 方法切片。
// 正式入口声明全部 mock 方法，所以详情页签以目录实际声明为准：概览、平台绑定（adapter.list 已声明）、凭据；
// 运行与所属组织页签仍然不渲染，也没有组织与工作流关系区。
// 删除的断言：“默认切片没有 Agent 入口”。它依赖一个不声明 agent.list 的方法切片，正式入口下
// 没有任何已声明的 mock 用例能产生它；该外壳行为由 tests/features/agents/agents-refusals.test.tsx
// 与 tests/app/page-availability-gating.test.tsx 覆盖。
// 运行：npm run verify:agents-second-browser（需要 DEV01_CHROMIUM_EXECUTABLE 或已安装的 Chromium）。
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { chromium } from 'playwright-core';

import { openContractMockView, startContractMock } from './support/contract-mock-gui.mjs';
import { findBrowserExecutable } from './dev01-onboarding-browser.mjs';

const executablePath = findBrowserExecutable();

describe('agents second submission browser (contract mock, 1440x900)', () => {
  it('keeps long Agent badges inside cards and drives the populated detail tabs', async () => {
    if (!executablePath) throw new Error('BLOCKED: set DEV01_CHROMIUM_EXECUTABLE');
    let mock;
    let view;
    let browser;
    try {
      mock = await startContractMock({ scenario: 'success' });
      view = await openContractMockView(mock);
      browser = await chromium.launch({ executablePath, headless: true, args: ['--disable-dev-shm-usage'] });
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      const screenshotDir = process.env.AGENTS_SECOND_SCREENSHOT_DIR;
      const screenshot = async name => {
        if (!screenshotDir) return;
        await mkdir(screenshotDir, { recursive: true });
        await page.screenshot({ path: resolve(screenshotDir, `${name}.png`), fullPage: true });
      };
      const failures = [];
      const methods = [];
      page.on('pageerror', error => failures.push(error.message));
      page.on('response', response => {
        if (response.url().includes('/daemon/request/')) methods.push(response.url().split('/').pop());
      });
      page.setDefaultTimeout(8000);
      await page.goto(view.launchUrl());
      await page.getByRole('heading', { name: '开始', exact: true }).waitFor();

      await page.getByRole('button', { name: 'Agent', exact: true }).first().click();
      const card = page.getByTestId('contact-worker');
      const ownerBadge = card.locator('.contact-badges .badge').first();
      await ownerBadge.evaluate(element => {
        element.textContent = 'tester-crh@example.com · gui-prod-manual-20261116';
      });
      const overflow = await card.evaluate(element => {
        const badge = element.querySelector('.contact-badges .badge');
        if (!badge) return null;
        const cardRight = element.getBoundingClientRect().right;
        return Math.max(
          badge.getBoundingClientRect().right - cardRight,
          cardRight - element.parentElement.getBoundingClientRect().right,
        );
      });
      expect(overflow).not.toBeNull();
      expect(overflow).toBeLessThanOrEqual(0);
      await page.getByTestId('contact-worker').click();
      const detail = page.getByTestId('agent-detail-panel');
      await detail.waitFor();

      expect(await detail.getByRole('tab').allTextContents()).toEqual(['概览', '平台绑定', '凭据']);
      await expect(detail.getByRole('tab', { name: '运行' }).count()).resolves.toBe(0);
      await expect(detail.getByRole('tab', { name: '所属组织' }).count()).resolves.toBe(0);
      await expect(page.getByTestId('relations-org').count()).resolves.toBe(0);
      await expect(page.getByTestId('relations-workflow-rows').count()).resolves.toBe(0);
      await screenshot('overview-tab');

      expect(failures).toEqual([]);
      expect(methods).not.toContain('link.list');
      expect(methods).not.toContain('agent.start');
      expect(methods).not.toContain('routine.list');
      expect(methods).not.toContain('agent.home-census');
      expect(methods).not.toContain('workflow.list');
      console.log(JSON.stringify({ verdict: 'PASS', methods: [...new Set(methods)].sort() }));
    } finally {
      await browser?.close();
      await view?.close();
      await mock?.close();
    }
  }, 90000);
});
