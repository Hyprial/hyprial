/* Rebase regression: shared App, explicit frontend preview, synthetic credentials only. */
import { chromium } from 'playwright-core';
import { browserExecutable } from './support/browser.mjs';
import assert from 'node:assert/strict';
const origin = process.argv[2] ?? 'http://127.0.0.1:4180';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname));
const browser = await chromium.launch({ executablePath: browserExecutable(), headless: true });
try {
const page=await browser.newPage();
const errors=[];page.on('pageerror',e=>errors.push(e.message));
for (const id of ['logged-out-init', 'j1=logged-out-init', 'logged-in-init', 'j1=logged-in-init']) {
  await page.goto(origin + '/?hyprialScenario=' + encodeURIComponent(id) + '&hyprialFirstRun=false');
  for (let reload = 0; reload < 2; reload += 1) {
    if (reload) await page.reload();
    await page.getByRole('button', { name: '我的账号与 Agent 设置菜单' }).click();
    await page.getByRole('menuitem', { name: '用户设置', exact: true }).click();
    await page.getByRole('button', { name: id.includes('logged-in') ? '退出本机登录' : '登录 Hyprial', exact: true }).waitFor();
    await page.getByRole('heading', { name: '用户设置', exact: true }).waitFor();
    if (id.includes('logged-in')) {
      const account = page.getByRole('region', { name: '账号登录' });
      await account.getByText('preview@example.invalid', { exact: true }).waitFor();
      assert.equal(await account.getByText(/^(账号状态|账号已登录|已确认账号登录。?|当前账号：.*)$/).count(), 0);
    }
    assert.equal(await page.getByText('预览访客', { exact: true }).count(), 0);
    assert.equal(await page.getByText('preview-union', { exact: true }).count(), 0);
  }
}
await page.goto(origin + '/?hyprialScenario=logged-out-init');
await page.getByRole('button', { name: '我的账号与 Agent 设置菜单' }).click();
await page.getByRole('menuitem', { name: '用户设置', exact: true }).click();
await page.getByRole('button', { name: '登录 Hyprial', exact: true }).click();
await page.getByRole('button', { name: '退出本机登录', exact: true }).waitFor();
await page.getByRole('button', { name: '设备' }).click();
await page.getByRole('button', { name: '初始化当前设备', exact: true }).click();
await page.getByText('已完成', { exact: true }).waitFor();
await page.goto(origin + '/?hyprialScenario=logged-in-init&hyprialFirstRun=false');
await page.getByRole('heading',{name:'开始',exact:true}).waitFor();
await page.getByRole('button',{name:'我的账号与 Agent 设置菜单'}).click();
await page.getByRole('menuitem',{name:'Agent 设置',exact:true}).click();
await page.getByRole('button',{name:'添加 Key',exact:true}).click();
await page.getByLabel('名称',{exact:true}).fill('Journey key');
await page.getByLabel('Key',{exact:true}).fill('synthetic-preview-key');
await page.getByRole('button',{name:'保存',exact:true}).click();
await page.getByText('Journey key',{exact:true}).waitFor();
await page.getByRole('button',{name:'Agent',exact:true}).click();
await page.getByRole('button',{name:'创建 Agent',exact:true}).click();
await page.getByLabel('名称',{exact:true}).fill('journey-check');
await page.getByRole('combobox',{name:'harness',exact:true}).click();
await page.getByRole('option',{name:'claude',exact:true}).click();
await page.locator('form.agent-form').getByRole('button',{name:'创建 Agent',exact:true}).click();
await page.getByTitle('打开 journey-check 的详情',{exact:true}).waitFor();
await page.getByTitle('打开 journey-check 的详情',{exact:true}).click();
await page.getByRole('button',{name:'设置模型…',exact:true}).click();
const startDialog=page.getByRole('dialog',{name:'启动 Agent'});
await startDialog.getByLabel(/^模型/).fill('deepseek-flash');
await startDialog.getByRole('button',{name:'保存并启动',exact:true}).click();
await page.getByTestId('agent-detail-runstate').getByText(/运行中/).waitFor();
await page.getByRole('button',{name:'开始对话',exact:true}).click();


await page.locator('textarea').fill('仅用于隔离预览的旅程验证');
await page.getByRole('button',{name:'发送',exact:true}).click();
await page.getByText('仅用于隔离预览的旅程验证',{exact:true}).waitFor();
await page.getByText('模拟回复：已收到消息；未调用真实 Agent。',{exact:true}).waitFor();
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ verdict: 'PASS', mode: 'isolated-frontend-preview', checks: ['init URL and reload', 'account-only user settings', 'empty identities', 'simulated login and device init', 'shared credentials', 'Agent create and detail', 'requested chat target', 'first message and simulated reply'] }));
} finally {await browser.close();}
