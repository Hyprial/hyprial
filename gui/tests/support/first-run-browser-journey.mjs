/**
 * Shared first-run journey for the fixture preview composition. The dev
 * preview mounts the scenario selector inside the floating tools layer, which
 * defaults to a collapsed ball, so the journey expands the tools through the
 * FAB, selects the scenario, and restores the collapsed state before touching
 * the product tree (same interaction as the j2/first-run browser contracts).
 * The selector is only driven when its value differs: any scenario control
 * fires a change that the floating tools mirror into the URL as
 * hyprialScenario, and a present scenario id switches the composition to the
 * pure URL-derived tree without the linked session (losing session-derived
 * content such as the j10 model services). Playwright's selectOption would
 * fire that change even for a same-value reselection. The scenario selector
 * wait must outlast a cold vite dev-server compile, which under a sequential
 * verify chain can exceed Playwright's 30s default; later steps run against
 * the already-mounted app and keep the default timeouts.
 */
export async function completeFirstRun(page) {
  const toggle = page.getByTestId('dev-tools-fab');
  if (await toggle.getAttribute('aria-expanded') === 'false') {
    await toggle.click();
  }
  const scenario = page.locator('select[data-mock-scenario="j1"]');
  if ((await scenario.inputValue({ timeout: 120_000 })) !== 'simulate') {
    await scenario.selectOption('simulate', { timeout: 120_000 });
  }
  await page.keyboard.press('Escape');
  await page.getByRole('heading', { name: '登录', exact: true }).waitFor();
  await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
  await page.getByRole('button', { name: '继续到设备', exact: true }).click();
  await page.getByRole('button', { name: '初始化当前设备', exact: true }).click();
  await page.getByRole('button', { name: '继续到运行配置', exact: true }).click();
  await page.getByLabel('DeepSeek API Key', { exact: false }).fill('preview-only-key');
  await page.getByRole('button', { name: '保存并继续', exact: true }).click();
  await page.getByRole('heading', { name: '首次对话', exact: true }).waitFor();
  await page.getByRole('textbox', { name: '给 Fixture Squire 的消息' }).fill('你好，Squire');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.getByRole('navigation', { name: '主导航', exact: true }).waitFor();
  await page.getByRole('button', { name: '开始', exact: true }).click();
  await page.getByRole('heading', { level: 1, name: '开始', exact: true }).waitFor();
}
