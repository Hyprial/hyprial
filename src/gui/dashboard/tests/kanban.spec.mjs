import { test, expect } from '@playwright/test';

test('Kanban displays the provider frame, enables filters in isolation and refreshes without writes', async ({ page }, info) => {
  const requests = [];
  page.on('request', r => { if (r.url().includes('/api/kanban/')) requests.push({ url: r.url(), method: r.method() }); });
  await page.goto('/#kanban');
  await expect(page.getByRole('heading', { name: '任务看板', exact: true })).toBeVisible();
  const frame = page.frameLocator('iframe[title="Kanban 看板"]');
  await expect(frame.getByRole('heading', { name: '共享任务示例' })).toBeVisible();
  await frame.getByRole('button', { name: '筛选负责人' }).click();
  await expect(frame.getByRole('button', { name: '筛选已应用' })).toBeVisible();
  await expect(frame.getByText('已隔离')).toBeVisible();
  await expect(page.getByText('2 张卡片')).toBeVisible();
  await expect(page.getByRole('link', { name: '前往工作台操作' })).toHaveAttribute('href', 'http://127.0.0.1:3180/');
  await page.screenshot({ path: info.outputPath('kanban-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: '刷新本机看板' }).click();
  await expect.poll(() => requests.filter(r => r.url.endsWith('/api/kanban/board')).length).toBe(2);
  expect(requests.every(r => r.method === 'GET')).toBe(true);
  expect(requests.every(r => !r.url.includes('sync'))).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('kanban-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('a failed refresh preserves the last snapshot and labels it stale', async ({ page }) => {
  await page.goto('/#kanban');
  await expect(page.getByText('2 张卡片')).toBeVisible();
  await page.route('**/api/kanban/board', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '读取失败示例' } }) }));
  await page.getByRole('button', { name: '刷新本机看板' }).click();
  await expect(page.getByRole('alert')).toContainText('当前展示上次快照');
  await expect(page.frameLocator('iframe').getByRole('heading', { name: '共享任务示例' })).toBeVisible();
});

test('unconfigured Kanban reports setup state and does not request a board', async ({ page }) => {
  let reads = 0;
  await page.route('**/api/kanban/status', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ state: 'unconfigured', message: '尚未连接本机 Kanban' }) }));
  page.on('request', r => { if (r.url().endsWith('/api/kanban/board')) reads++; });
  await page.goto('/#kanban');
  await expect(page.getByRole('alert')).toHaveText('尚未连接本机 Kanban');
  await expect(page.locator('iframe')).toHaveCount(0);
  expect(reads).toBe(0);
});
