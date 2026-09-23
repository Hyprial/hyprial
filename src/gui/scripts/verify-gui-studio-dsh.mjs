// Real DSH smoke against an explicitly isolated loopback server. Never submits a prompt.
import assert from 'node:assert/strict';
import { GUI_AGENT_LABELS } from './verify-gui-agent-dsh.mjs';
import { createRequire } from 'node:module';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
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
const output = resolve(process.env.GUI_STUDIO_ARTIFACTS || '/tmp/gui-studio-dsh-smoke');
await mkdir(output, { recursive: true });
const { chromium } = deps('playwright');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.setDefaultTimeout(20000);
const errors = [], requests = [];
const objectPanelChecks={subagent:'not exercised: isolated profile contains no historical child session',contactData:'two read-only directory fixtures'};
const contactFixtures=['object-panel-alpha','object-panel-beta'].map(actor=>({targetKind:'agent',targetUri:'agent:isolated:object-panel-fixture:'+actor,actor,status:'online',nodeId:'object-panel-fixture'}));
await page.route('**/plugins/h2b-talk/rpc',async route=>{
  const args=route.request().postDataJSON();
  if(args?.method==='h2b-targets') return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,value:{ok:true,targets:contactFixtures}})});
  return route.continue();
});
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (request.method() === 'POST') requests.push({ url: new URL(request.url()).pathname, body: request.postData() || '' }); });
const button = name => page.getByRole('button', { name: GUI_AGENT_LABELS[name] || name, exact: true });
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
let draftName = null;
async function studio() {
  await page.locator('[data-gui-action=workspace-menu]').click();
  await page.locator('[data-gui-action=studio]').click();
  await page.getByRole('dialog', { name: 'GUI 设计工作台' }).waitFor();
  if (draftName) { const matches = page.locator('.gui-studio-library').getByRole('button', { name: new RegExp('^' + draftName + ' · v') }); await (draftName === '我的工作空间' ? matches.last() : matches.first()).click(); }
}
async function edit(document) {
  await page.getByText('界面定义与精确编辑', { exact: true }).click();
  await page.getByRole('textbox', { name: 'GUI JSON', exact: true }).fill(JSON.stringify(document, null, 2));
  await button('保存草稿').click();
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === '保存草稿' && button.disabled));
  await button('发布并启用').click();
  await page.locator('.gui-use-notice').filter({hasText:/已启用 .+，下次正常打开继续使用。/}).waitFor();
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
}
const unsent = 'DSH GUI smoke unsent draft — do not send';
try {
  await page.goto(endpoint); await dismissOnboarding();
  const ownWorkspace=await mkdtemp(join(tmpdir(),'gui-studio-ux-smoke-'));
  await button('New session').first().click();await button('Choose workspace').click();
  await page.getByText('Add workspace…',{exact:true}).click();await button('Edit path').click();
  await page.getByRole('textbox',{name:'Edit path',exact:true}).fill(ownWorkspace);
  await page.getByRole('textbox',{name:'Edit path',exact:true}).press('Enter');await button('Open').click();
  await page.waitForFunction(label=>[...document.querySelectorAll('button[aria-label="Choose workspace"]')].some(node=>node.textContent.includes(label)),ownWorkspace.split('/').at(-1));
  // Reset only the isolated GUI preference; business sessions remain untouched.
  let profile = await rpc({ operation: 'profile' });
  await rpc({ operation: 'restore', baseRevision: profile.revision });
  await page.reload(); await dismissOnboarding();
  await studio(); await button('新建界面').click(); draftName = '我的工作空间';
  await page.locator('[data-gui-view=edit]').waitFor();
  const more = page.locator('details').filter({has:page.getByRole('button',{name:'打开设计会话',exact:true,includeHidden:true})}).first();
  if (await more.count()) await more.locator('summary').click();
  await button('打开设计会话').click();
  const composer = page.locator('[data-native-conversation] [data-composer-input], [data-native-conversation] textarea[placeholder="Describe what you want to build"]');
  await composer.waitFor(); await composer.fill(unsent);
  const nativeHandle = await page.locator('[data-native-conversation]').elementHandle();
  const composerHandle = await composer.elementHandle();
  await studio();
  await page.locator('[data-gui-action=agent]').click();
  await page.locator('.gui-agent-seat').waitFor();
  await page.waitForFunction(() => {
    const seat=document.querySelector('.gui-agent-seat')?.getBoundingClientRect();
    const native=document.querySelector('[data-native-conversation]')?.getBoundingClientRect();
    return seat && native && ['x','y','width','height'].every(key=>Math.abs(seat[key]-native[key])<2);
  });
  assert.ok(await composer.isVisible());
  await composer.fill(unsent + ' dock'); await composer.fill(unsent);
  assert.equal(await composerHandle.evaluate(node=>node===document.querySelector('[data-composer-input], textarea[placeholder="Describe what you want to build"]')),true);
  await page.screenshot({path:join(output,'native-agent-dock.png'),fullPage:true});
  await page.locator('[data-gui-action=agent]').click();
  await page.locator('[data-gui-action=exit]').click();
  assert.equal(await nativeHandle.evaluate(node=>node===document.querySelector('[data-native-conversation]')),true);
  assert.equal(await composerHandle.evaluate(node=>node===document.querySelector('[data-composer-input], textarea[placeholder="Describe what you want to build"]')),true);
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText),unsent);
  await studio();
  await page.getByText('界面定义与精确编辑', { exact: true }).click();
  const initial = JSON.parse(await page.getByRole('textbox', { name: 'GUI JSON', exact: true }).inputValue());
  await page.getByText('界面定义与精确编辑', { exact: true }).click();
  const custom = { ...initial, name: 'Real DSH smoke workspace ' + Date.now(), layout: { ...initial.layout, navigation: 'top' }, theme: { mode: 'dark', preset: 'ocean', density: 'compact', typography: { font: 'sans', size: 16 }, radius: 16, shadow: 'soft' } };
  await edit(custom); draftName = custom.name;
  await page.locator('.gui-top-navigation').waitFor();
  assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-styled'), 'true');
  assert.equal(await page.evaluate(() => getComputedStyle(document.body).getPropertyValue('--gui-style-background').trim()), '#0c202c');
  assert.equal(await nativeHandle.evaluate(node=>node===document.querySelector('[data-native-conversation]')),true);
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText), unsent);
  assert.equal(await page.locator('[data-composer-input], textarea[placeholder="Describe what you want to build"]').count(), 1);
  if (await composer.evaluate(node => node.tagName === 'TEXTAREA')) assert.equal(await composer.evaluate(node => getComputedStyle(node).backgroundColor), 'rgba(0, 0, 0, 0)', 'Native syntax-overlay input must stay transparent so its painted text is visible');
  assert.equal(await page.locator('.gui-system-bar button').count(),1);
  await page.locator('[data-gui-action=workspace-menu]').click();
  await page.locator('[data-gui-action=edit-current]').click();
  await page.locator('.gui-studio-heading strong').filter({hasText:custom.name}).waitFor();
  assert.equal(await page.locator('.gui-top-navigation').isVisible(),false);
  await page.locator('[data-gui-action=preview]').click();
  await page.locator('[data-gui-action=back-to-editor]').click();
  await page.locator('[data-gui-action=exit]').click();
  await page.locator('.gui-top-navigation').waitFor();
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText),unsent);
  // A top navigation keeps the native business object list, without the global app rail.
  const sidebar=page.locator('[data-native-sidebar]');
  const objects=page.locator('[data-gui-action=objects]');
  await page.locator('[data-native-sidebar][data-gui-object-panel=inline]').waitFor();
  const sessionList=sidebar.locator('.fess');
  await sessionList.waitFor();
  assert.equal(await sidebar.getByRole('navigation',{name:'DSH 应用',exact:true}).isVisible(),false);
  assert.equal(await sessionList.count(),1);
  const listHandle=await sessionList.elementHandle();
  const sessionSearch=sidebar.getByPlaceholder('搜索会话',{exact:true});
  const searchHandle=await sessionSearch.elementHandle();
  await sessionSearch.fill('object-panel-search-preserved');
  await objects.click();
  await page.locator('[data-native-sidebar][data-gui-object-panel=closed]').waitFor({state:'attached'});
  assert.equal(await sessionList.isVisible(),false);
  await objects.click();await sessionList.waitFor();
  assert.equal(await listHandle.evaluate(node=>node===document.querySelector('[data-native-sidebar] .fess')),true,'Collapsing object list preserves its native instance');
  assert.equal(await searchHandle.evaluate(node=>node===document.querySelector('[data-native-sidebar] input[placeholder="搜索会话"]')),true,'Collapsing objects preserves native search input DOM');
  assert.equal(await sessionSearch.inputValue(),'object-panel-search-preserved');
  await sessionSearch.fill('');
  assert.equal(await composer.evaluate(node=>'value' in node?node.value:node.innerText),unsent);
  await page.setViewportSize({width:390,height:1000});
  await page.locator('[data-native-sidebar][data-gui-object-panel=closed]').waitFor({state:'attached'});
  await objects.click();
  await page.locator('[data-native-sidebar][data-gui-object-panel=drawer]').waitFor();
  const drawerBox=await sidebar.boundingBox();
  assert.ok(drawerBox&&drawerBox.width>=180&&drawerBox.x>=0&&drawerBox.x+drawerBox.width<=390,'Object drawer fits the narrow viewport');
  assert.equal(await listHandle.evaluate(node=>node===document.querySelector('[data-native-sidebar] .fess')),true,'Responsive drawer keeps the same list instance');
  await page.screenshot({path:join(output,'object-panel-390.png'),fullPage:true});
  const backdrop=page.getByRole('button',{name:'关闭对象列表',exact:true});
  const backdropBox=await backdrop.boundingBox();
  assert.ok(backdropBox&&backdropBox.x>=drawerBox.x+drawerBox.width-1,'Dismiss backdrop must not cover the interactive drawer');
  await backdrop.click({position:{x:Math.min(12,backdropBox.width/2),y:120}});
  await page.locator('[data-native-sidebar][data-gui-object-panel=closed]').waitFor({state:'attached'});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-native-sidebar][data-gui-object-panel=inline]').waitFor();
  objectPanelChecks.inlineDrawerCollapsePreserveNativeList=true;
  // Contact selection uses the native directory list and the full detail owner.
  await page.locator('[data-gui-action=workspace-menu]').click();await button('全部功能').click();
  await page.getByRole('navigation',{name:'全部功能',exact:true}).getByRole('button',{name:'通讯录',exact:true}).click();
  const contactDetail=page.locator('[data-gui-module-instance="native:h2b.contacts:detail"]');
  await contactDetail.waitFor();
  await page.locator('[data-native-sidebar][data-gui-object-panel=inline]').waitFor();
  for(const contact of contactFixtures){
    await sidebar.locator('button[title="'+contact.targetUri+'"]').click();
    await contactDetail.locator('.h2bcontact-card-uri').filter({hasText:contact.targetUri}).waitFor();
  }
  assert.equal(await contactDetail.locator('.gui-contact-library').count(),0,'Native contact details never duplicate the sidebar list');
  assert.equal(await sidebar.getByRole('navigation',{name:'DSH 应用',exact:true}).isVisible(),false);
  await page.screenshot({path:join(output,'object-panel-contacts.png'),fullPage:true});
  await page.setViewportSize({width:390,height:1000});
  await page.locator('[data-native-sidebar][data-gui-object-panel=closed]').waitFor({state:'attached'});
  await objects.click();
  await page.locator('[data-native-sidebar][data-gui-object-panel=drawer]').waitFor();
  await sidebar.locator('button[title="'+contactFixtures[0].targetUri+'"]').click();
  await page.locator('[data-native-sidebar][data-gui-object-panel=closed]').waitFor({state:'attached'});
  await contactDetail.locator('.h2bcontact-card-uri').filter({hasText:contactFixtures[0].targetUri}).waitFor();
  await page.setViewportSize({width:1440,height:1000});
  objectPanelChecks.drawerSelectionClosesAndKeepsDetail=true;

  await page.locator('.gui-top-navigation').getByRole('button',{name:'Agent 会话',exact:true}).click();
  await composer.waitFor();
  assert.equal(await composer.evaluate(node=>'value' in node?node.value:node.innerText),unsent,'Switching through the real directory carrier session preserves the design session draft');
  objectPanelChecks.contactSelectionAndRealSessionRoundTrip=true;
  await page.screenshot({ path: join(output, 'applied-top.png'), fullPage: true });
  await page.locator('.gui-top-navigation').getByRole('button', { name: 'Workflow', exact: true }).click();
  await page.waitForTimeout(500);
  await page.locator('.gui-top-navigation').getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await composer.waitFor();
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText), unsent);
  assert.equal(await page.locator('[data-composer-input], textarea[placeholder="Describe what you want to build"]').count(), 1);
  await page.locator('[data-gui-action=workspace-menu]').click();
  await button('全部功能').click();
  await page.getByRole('navigation', { name: '全部功能', exact: true }).waitFor();
  await page.locator('[data-gui-action=workspace-menu]').click();
  await page.getByText('故障恢复',{exact:true}).click();
  assert.ok(await page.getByRole('link', { name: '安全打开原生界面', exact: true }).isVisible());
  await page.locator('[data-gui-action=native-navigation]').click();
  await button('Settings').waitFor();
  const nativeRail = page.getByRole('navigation', { name: 'DSH 应用', exact: true });
  for (const label of ['消息', '通讯录', '任务', '运维']) await nativeRail.getByRole('button', { name: label, exact: true }).waitFor();
  await nativeRail.getByRole('button', { name: '任务', exact: true }).click();
  await page.locator('.h2bapps-menu-btn').filter({ hasText: '任务看板' }).click();
  await page.locator('[data-gui-module-instance="native:h2b.kanban"]').waitFor();
  await nativeRail.getByRole('button', { name: '消息', exact: true }).click();
  await page.waitForFunction(expected=>{const node=document.querySelector('[data-native-conversation] [data-composer-input], [data-native-conversation] textarea[placeholder="Describe what you want to build"]');return node&&('value' in node?node.value:node.innerText)===expected;},unsent);
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText), unsent);
  await page.locator('[data-gui-action=workspace-menu]').click();
  await page.locator('[data-gui-action=native-navigation]').click();
  await page.locator('[data-gui-action=workspace-menu]').click();
  await button('全部功能').click();
  await page.locator('.gui-top-navigation').getByRole('button', { name: '常用页', exact: true }).click();
  await page.getByRole('region', { name: '整体工作空间' }).waitFor();
  assert.equal(await page.locator('[data-composer-input], textarea[placeholder="Describe what you want to build"]').count(), 1);
  assert.equal(await button('返回工作区').count(), 0);
  assert.equal(await page.getByRole('navigation', { name: '自定义工作空间', exact: true }).count(), 0);
  await page.locator('.gui-top-navigation').getByRole('button', { name: 'Agent 会话', exact: true }).click();
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText), unsent);
  // All three navigation layouts are explicit visual choices, independent of theme.
  await studio();
  await page.getByRole('navigation',{name:'选区层级',exact:true}).getByRole('button',{name:'工作空间',exact:true}).click();
  await page.getByLabel('导航布局',{exact:true}).selectOption('native');
  await button('发布并启用').click();
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  assert.equal((await rpc({operation:'profile'})).release.document.layout.navigation,'native');
  assert.equal(await page.locator('.gui-top-navigation').count(),0);
  assert.equal(await page.evaluate(()=>getComputedStyle(document.body).getPropertyValue('--gui-style-background').trim()),'#0c202c');
  for(const label of ['消息','通讯录','任务','运维']) await nativeRail.getByRole('button',{name:label,exact:true}).waitFor();
  await nativeRail.getByRole('button',{name:'任务',exact:true}).click();
  await page.locator('.h2bapps-menu-btn').filter({hasText:'任务看板'}).click();
  await page.locator('[data-gui-module-instance="native:h2b.kanban"]').waitFor();
  await nativeRail.getByRole('button',{name:'消息',exact:true}).click();
  assert.equal(await composer.evaluate(node=>'value' in node?node.value:node.innerText),unsent);
  assert.equal(await sidebar.getAttribute('data-gui-object-panel'),null,'Native layout removes temporary object-panel mode');
  assert.equal(await sidebar.locator('.fess').count(),1,'Restoring native navigation keeps one session list');
  await page.screenshot({path:join(output,'applied-native-navigation.png'),fullPage:true});
  await studio();
  await page.getByRole('navigation',{name:'选区层级',exact:true}).getByRole('button',{name:'工作空间',exact:true}).click();
  await page.getByLabel('导航布局',{exact:true}).selectOption('top');
  await button('发布并启用').click();
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  await page.locator('.gui-top-navigation').waitFor();
  assert.equal((await rpc({operation:'profile'})).release.document.layout.navigation,'top');
  await studio(); await edit({ ...custom, layout: { ...custom.layout, navigation: 'left' }, theme: { mode: 'light', preset: 'warm', density: 'comfortable', radius: 4 } });
  assert.equal((await rpc({ operation: 'profile' })).release.document.layout.navigation, 'left');
  assert.equal(await page.evaluate(() => getComputedStyle(document.body).getPropertyValue('--gui-style-background').trim()), '#fff7ed');
  // The slot owner can retain hidden descendants; the published geometry and
  // visible navigation must switch without requiring an unrelated unmount.
  await page.waitForFunction(() => ![...document.querySelectorAll('.gui-top-navigation')].some(node => node.getClientRects().length));
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText), unsent);
  await studio(); await page.locator('[data-gui-action=manage]').click(); page.once('dialog', dialog => dialog.accept()); await button('切换为原生界面').click(); await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  assert.equal(await composer.evaluate(node => 'value' in node ? node.value : node.innerText), unsent);
  await page.waitForFunction(() => getComputedStyle(document.body).getPropertyValue('--gui-style-background').trim() === '');
  await page.screenshot({ path: join(output, 'restored-native.png'), fullPage: true });
  await page.reload(); await dismissOnboarding();
  assert.equal((await rpc({ operation: 'profile' })).releaseId, null);
  const afterReload = await page.locator('[data-composer-input], textarea[placeholder="Describe what you want to build"]').evaluate(node => 'value' in node ? node.value : node.innerText).catch(() => null);
  // DSH owns unsent text persistence; custom layout promises continuity while mounted,
  // not a new cross-reload guarantee for native composer draft storage.
  assert.equal(errors.length, 0, errors.join('\n'));
  const prompts = requests.filter(item => /(?:session[./]prompt|agent[./]run|h2b-gui-studio.*agent)/.test(item.url) || /"(?:method|operation)"\s*:\s*"(?:prompt|session\.prompt|agent\.run)"/.test(item.body));
  assert.equal(prompts.length, 0, 'Smoke must never submit an Agent prompt');
  const report = { endpoint: target.origin, objectPanelChecks, pageErrors: errors, topAndLeftApplied: true, nativeThreeLevelNavigationAndTopReturn: true, editCurrentPreviewReturn: true, oneWorkspaceEntry: true, styledThemeAppliedAndReset: true, workflowRoundTrip: true, singleNativeComposer: true, nativeDockGeometryAndIdentity: true, unsentSurvivedApplyAndRestore: true, defaultSurvivedReload: true, nativeDraftAfterReload: afterReload === unsent ? 'retained' : 'owned-by-native-dsh', screenshots: ['object-panel-390.png', 'object-panel-contacts.png', 'native-agent-dock.png', 'applied-top.png', 'applied-native-navigation.png', 'restored-native.png'] };
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} catch (error) {
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  await writeFile(join(output, 'failure.txt'), `${error.stack}\n${await page.locator('body').innerText().catch(() => '')}`);
  throw error;
} finally { await browser.close(); }
