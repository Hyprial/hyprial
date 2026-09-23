// Real DSH smoke against an explicitly isolated loopback server. Never submits a prompt.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const endpoint = process.env.GUI_STUDIO_DSH_URL || 'http://127.0.0.1:3198';
const target = new URL(endpoint);
if (!['127.0.0.1', 'localhost'].includes(target.hostname) || target.port === '3080') throw new Error('Use an isolated loopback DSH port, never the production workspace');
let deps;
for (const base of [process.env.DSH_GUI_BROWSER_DEPS, join(repo, 'browser-tests/package.json'), join(homedir(), '.h2b/apps/gui/source/browser-tests/package.json')].filter(Boolean)) {
  try { const candidate = createRequire(resolve(base)); candidate.resolve('playwright'); deps = candidate; break; } catch {}
}
if (!deps) throw new Error('Install Playwright in browser-tests or set DSH_GUI_BROWSER_DEPS');
const output = resolve(process.env.GUI_STUDIO_ARTIFACTS || '/tmp/gui-workflow-dsh-smoke');
await mkdir(output, { recursive: true });
const { chromium } = deps('playwright');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [], requests = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (request.method() === 'POST') requests.push({ url: new URL(request.url()).pathname, body: request.postData() || '' }); });
const button = name => page.getByRole('button', { name, exact: true });
async function dismissOnboarding() {
  // Each screen appears asynchronously after a fresh browser handshake.
  for (let i = 0; i < 12; i++) {
    for (const name of ['Continue', 'Configure later']) if (await button(name).isVisible().catch(() => false)) await button(name).click();
    await page.waitForTimeout(250);
  }
}
async function rpc(args) {
  return page.evaluate(async args => {
    const response = await fetch('/plugins/h2b-talk/rpc', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ method: 'h2b-gui-studio', args }) });
    const body = await response.json(); if (!body.ok) throw new Error(body.error.message); return body.value;
  }, args);
}

async function workflow(args) {
  return page.evaluate(async args => {
    const response = await fetch('/plugins/h2b-talk/rpc', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ method: 'h2b-workflow-workbench', args }) });
    const body = await response.json(); if (!body.ok) throw new Error(body.error.message); return body.value;
  }, args);
}
async function plan(panel) {
  const tab = panel.getByRole('button', { name: '方案', exact: true });
  await tab.waitFor(); if (await tab.isEnabled()) await tab.click();
  if (!await panel.getByRole('textbox', { name: 'Workflow YAML' }).isVisible()) await panel.getByText('YAML · 查看、导入与精确编辑', { exact: true }).click();
}
try {
  await page.goto(endpoint); await dismissOnboarding();
  const seed = (await rpc({ operation: 'list' })).drafts.find(draft => draft.sessionId);
  assert.ok(seed, 'First run verify-gui-studio-dsh.mjs to create an actual isolated DSH design session');
  const stamp = Date.now();
  const flows = [];
  for (const label of ['Alpha', 'Beta']) flows.push(await workflow({ operation: 'create', sessionId: seed.sessionId, name: label + ' isolated ' + stamp, yaml: 'name: ' + label.toLowerCase() + '\n' }));
  const doc = { schemaVersion: 2, id: 'real-multi-' + stamp, name: 'Real multiple Workflows ' + stamp, kind: 'shell', layout: { navigation: 'top' }, navigation: [{ id: 'home', label: '双工作流', pageId: 'home' }, { id: 'notes', label: '说明页', pageId: 'notes' }, { id: 'agent', label: 'Agent 会话', feature: 'dsh.conversation' }], pages: [{ id: 'home', title: 'Two real workflows', layout: { type: 'Grid', id: 'grid', columns: 2, gap: 12, children: flows.map((flow, index) => ({ type: 'Feature', id: 'node-' + index, instanceId: 'flow-' + index, feature: 'h2b.workflow', view: 'default', context: { workflowId: flow.id } })) } }, { id: 'notes', title: 'Notes', layout: { type: 'Text', id: 'notes-text', text: 'Switch away and return without losing drafts.' } }] };
  const draft = await rpc({ operation: 'create', sessionId: seed.sessionId, document: doc });
  const release = await rpc({ operation: 'publish', id: draft.id, revision: draft.revision });
  const profile = await rpc({ operation: 'profile' });
  await rpc({ operation: 'apply', releaseId: release.id, baseRevision: profile.revision });
  await page.reload(); await dismissOnboarding();
  const nav = page.locator('.gui-top-navigation');
  await nav.getByRole('button', { name: '双工作流', exact: true }).click();
  const panels = flows.map((_, index) => page.locator('[data-workflow-instance="auth:' + doc.id + ':flow-' + index + '"]'));
  const [a, b] = panels;
  await plan(a); await plan(b);
  const yamlA = a.getByRole('textbox', { name: 'Workflow YAML' });
  const yamlB = b.getByRole('textbox', { name: 'Workflow YAML' });
  const textA = 'name: unsaved isolated A', textB = 'name: unsaved isolated B';
  await yamlA.fill(textA); await yamlB.fill(textB);
  await b.locator('.wb-library button').filter({ hasText: flows[0].name }).click(); await plan(b);
  await yamlB.fill('name: same workflow independent B');
  assert.equal(await yamlA.inputValue(), textA);
  await b.locator('.wb-library button').filter({ hasText: flows[1].name }).click(); await plan(b);
  assert.equal(await yamlB.inputValue(), textB);
  const handles = await Promise.all(panels.map(panel => panel.elementHandle()));
  await nav.getByRole('button', { name: '说明页', exact: true }).click();
  await a.waitFor({ state: 'hidden' });
  await nav.getByRole('button', { name: '双工作流', exact: true }).click();
  await a.waitFor({ state: 'visible' });
  assert.equal(await yamlA.inputValue(), textA); assert.equal(await yamlB.inputValue(), textB);
  // Change the layout through actual Studio; the module host must preserve both
  // existing React controllers across publishing a reordered document.
  await page.locator('[data-gui-action=workspace-menu]').click();
  await page.locator('[data-gui-action=studio]').click();
  await page.getByRole('dialog', { name: 'GUI 设计工作台' }).waitFor();
  await page.locator('.gui-studio-library').getByRole('button', { name: new RegExp('^' + doc.name + ' · v') }).first().click();
  await page.getByText('界面定义与精确编辑', { exact: true }).click();
  const reordered = structuredClone(doc); reordered.pages[0].layout.children.reverse();
  await page.getByRole('textbox', { name: 'GUI JSON', exact: true }).fill(JSON.stringify(reordered, null, 2));
  await button('保存草稿').click();
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === '保存草稿' && button.disabled));
  await button('发布并启用').click(); await page.locator('.gui-use-notice').filter({hasText:/已启用 .+，下次正常打开继续使用。/}).waitFor(); await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  await nav.getByRole('button', { name: '双工作流', exact: true }).click();
  await a.waitFor({ state: 'visible' });
  assert.equal(await yamlA.inputValue(), textA); assert.equal(await yamlB.inputValue(), textB);
  for (let index = 0; index < panels.length; index++) assert.equal(await panels[index].evaluate((element, old) => element === old, handles[index]), true, 'Actual Workflow controller DOM must survive reordering');
  assert.equal(await page.locator('[data-workflow-instance^="auth:' + doc.id + ':"]').count(), 2);
  const visibleRects = await Promise.all(panels.map(panel => panel.boundingBox()));
  assert.ok(visibleRects.every(rect => rect && rect.width > 100 && rect.height > 100), 'Both real modules must have usable placement');
  assert.ok(visibleRects[1].x < visibleRects[0].x, 'Reordered B must be placed to the left of A');
  const responsive = await Promise.all(panels.map(panel => panel.evaluate(element => {
    const surface = element.closest('[data-gui-module-instance]');
    const layout = element.querySelector('.wb-layout'), library = element.querySelector('.wb-library'), detail = element.querySelector('.wb-detail');
    return { width: surface.clientWidth, columns: getComputedStyle(layout).gridTemplateColumns, libraryMaxHeight: getComputedStyle(library).maxHeight, libraryOverflow: getComputedStyle(library).overflowY, detailPadding: getComputedStyle(detail).padding, libraryBottom: library.getBoundingClientRect().bottom, detailTop: detail.getBoundingClientRect().top };
  })));
  for (const metrics of responsive) {
    assert.ok(metrics.width <= 680, 'This fixture must exercise the narrow module container rule');
    assert.equal(metrics.columns.split(' ').length, 1, 'Narrow Workflow layout must use one column');
    assert.equal(metrics.libraryMaxHeight, '220px'); assert.equal(metrics.libraryOverflow, 'auto');
    assert.equal(metrics.detailPadding, '12px');
    assert.ok(metrics.detailTop >= metrics.libraryBottom - 1, 'Workflow detail must follow the library vertically');
  }
  await page.screenshot({ path: join(output, 'real-two-workflows.png'), fullPage: true });
  // Reset only scroll positions for an overview screenshot; all dirty editors
  // and module identity assertions above are kept intact.
  await page.evaluate(() => {
    for (const element of document.querySelectorAll('[data-gui-module-instance],.wb-library,.gui-personal-page')) element.scrollTop = 0;
  });
  await page.waitForTimeout(150);
  assert.equal(await yamlA.inputValue(), textA); assert.equal(await yamlB.inputValue(), textB);
  await page.screenshot({ path: join(output, 'real-two-workflows-top.png'), fullPage: true });
  const operations = requests.map(item => { try { const body = JSON.parse(item.body); return body.method === 'h2b-workflow-workbench' ? body.args?.operation : null; } catch { return null; } }).filter(Boolean);
  assert.ok(operations.every(op => ['create', 'get', 'list', 'inspect'].includes(op)), 'Only local draft creation and reads are allowed: ' + operations.join(','));
  assert.equal(requests.filter(item => /"(?:method|operation)"\s*:\s*"(?:prompt|session\.prompt|agent\.run|send)"/.test(item.body)).length, 0);
  assert.deepEqual(errors, []);
  const report = { endpoint, actualDsh: true, actualWorkflowWorkbench: true, count: 2, independentYaml: true, independentSelection: true, pageRoundTrip: true, reorderPreservedControllers: true, reorderedPlacement: true, responsiveSingleColumn: true, responsive, operations: [...new Set(operations)], pageErrors: errors };
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report));
} catch (error) {
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  await writeFile(join(output, 'failure.txt'), `${error.stack}\n${await page.locator('body').innerText().catch(() => '')}`); throw error;
} finally { await browser.close(); }
