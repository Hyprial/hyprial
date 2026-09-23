// Actual React editor, isolated browser fixture. No Host or business traffic.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdtemp, mkdir, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let deps;
for (const base of [process.env.DSH_GUI_BROWSER_DEPS, join(repo, 'browser-tests/package.json'), join(homedir(), '.h2b/apps/gui/source/browser-tests/package.json')].filter(Boolean)) {
  try { const candidate = createRequire(resolve(base)); for (const name of ['react', 'react-dom/client', 'rolldown', 'playwright']) candidate.resolve(name); deps = candidate; break; } catch {}
}
if (!deps) throw new Error('Install GUI browser-test dependencies or set DSH_GUI_BROWSER_DEPS');
const { build } = await import(pathToFileURL(deps.resolve('rolldown')));
const { chromium } = deps('playwright');
const temp = await mkdtemp(join(tmpdir(), 'gui-editor-browser-'));
const output = process.env.GUI_EDITOR_ARTIFACTS ? resolve(process.env.GUI_EDITOR_ARTIFACTS) : temp;
await mkdir(output, { recursive: true });
const source = await readFile(join(repo, 'client/gui-editor.inc.js'), 'utf8');
const styleSource = (await readFile(join(repo, 'shared/gui-style.mjs'), 'utf8')).replace(/^export\s+/gm, '');
const studioSource = await readFile(join(repo, 'client/gui-studio.inc.js'), 'utf8');
const appearanceSource = studioSource.slice(studioSource.indexOf('      const appearance ='), studioSource.indexOf('      const editing ='));
assert.ok(appearanceSource.includes('guiThemeControls'), 'Exercise the real Studio appearance update path');
const css = await readFile(join(repo, 'client/gui-editor.css'), 'utf8');
const initial = { schemaVersion: 2, id: 'test', name: 'Test', kind: 'shell', navigation: [{ id: 'nav', label: 'Home', pageId: 'home' }], pages: [{ id: 'home', title: 'Home', layout: { type: 'Stack', id: 'root', children: [
  { type: 'Feature', id: 'workflow', instanceId: 'workflow-owner', feature: 'h2b.workflow', view: 'launcher' },
  { type: 'Grid', id: 'grid', columns: 2, children: [{ type: 'Text', id: 'note', text: 'Note' }] }
] } }] };
await writeFile(join(temp, 'entry.mjs'), `import React from ${JSON.stringify(deps.resolve('react'))};
import {createRoot} from ${JSON.stringify(deps.resolve('react-dom/client'))};
${styleSource}
${source}
const Editor=createGuiVisualEditor(React);
function App(){
  const [document,setDocument]=React.useState(${JSON.stringify(initial)});
  const [selected,setSelected]=React.useState('home/root');
  const [error,setError]=React.useState('');
  const preview=document,e=React.createElement;
  function edit(mutator){const next=structuredClone(document);mutator(next);setDocument(next);setError('');}
  ${appearanceSource}
  window.currentDocument=document;
  window.currentSelection=selected;
  window.replaceDocument=value=>setDocument(structuredClone(value));
  return React.createElement(Editor,{document,onChange:setDocument,selected,onSelect:setSelected,inspectorExtras:React.createElement('div',{id:'appearance-extra'},error?React.createElement('p',{role:'alert'},error):null,appearance),catalog:[{id:'h2b.workflow',label:'工作流',views:['launcher','list']}]});
}
createRoot(document.getElementById('root')).render(React.createElement(App));`);
await build({ input: join(temp, 'entry.mjs'), output: { file: join(temp, 'bundle.js'), format: 'iife' }, transform: { define: { 'process.env.NODE_ENV': '"production"' } } });
const bundle = await readFile(join(temp, 'bundle.js'));
const server = createServer((request, response) => {
  if (request.url === '/bundle.js') { response.setHeader('content-type', 'text/javascript'); response.end(bundle); return; }
  response.setHeader('content-type', 'text/html; charset=utf-8');
  response.end('<meta name="viewport" content="width=device-width,initial-scale=1"><style>*{box-sizing:border-box}body{margin:8px;font-family:sans-serif}' + css + '</style><div id="root"></div><script src="/bundle.js"></script>');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const inspector=page.getByRole('complementary',{name:'选区属性',exact:true});
const crumbs=page.getByRole('navigation',{name:'选区层级',exact:true});
async function workspace(){await crumbs.getByRole('button',{name:'工作空间',exact:true}).click();}
async function selectPage(name='Home'){if(!await page.locator('.gui-editor-pages').getByRole('button',{name,exact:true}).isVisible())await page.getByRole('button',{name:'页面与结构',exact:true}).click();await page.locator('.gui-editor-pages').getByRole('button',{name,exact:true}).click();}
async function details(label){const summary=page.getByText(label,{exact:true});if(!await summary.evaluate(node=>node.parentElement.open))await summary.click();}
async function scope(kind){assert.equal(await inspector.getAttribute('data-selection-kind'),kind);}
async function confirmation(){const dialog=page.getByRole('alertdialog');await dialog.waitFor();assert.equal(await dialog.getByRole('button',{name:'取消',exact:true}).evaluate(node=>node===document.activeElement),true,'Deletion initially focuses Cancel');return dialog;}
try {
  await page.goto('http://127.0.0.1:' + server.address().port);
  await page.locator('.gui-editor-frame-navigation').getByRole('button',{name:'Home',exact:true}).click();
  await scope('navigation');
  assert.equal(await inspector.getByRole('button',{name:'删除组件',exact:true}).count(),0);
  const destination = page.getByLabel('打开内容 · Home', { exact: true });
  await destination.selectOption('feature/h2b.workflow');
  assert.deepEqual(await page.evaluate(() => window.currentDocument.navigation[0]), { id: 'nav', label: 'Home', feature: 'h2b.workflow' });
  await destination.selectOption('page/home');
  assert.deepEqual(await page.evaluate(() => window.currentDocument.navigation[0]), { id: 'nav', label: 'Home', pageId: 'home' });
  await page.locator('[data-node-id="root"]').click({position:{x:8,y:8}});
  await scope('node');
  await page.getByRole('button', { name: '添加', exact: true }).click();
  for (const name of ['布局容器', '业务模块', '基础内容']) assert.ok(await page.getByRole('region', { name, exact: true }).isVisible());
  assert.equal(await page.locator('#appearance-extra').count(), 0,'Workspace properties are absent from node selection');
  await page.getByRole('button', { name: '文字', exact: true }).click();
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children.length), 3);
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children.length), 2);
  await page.getByRole('button', { name: '重做', exact: true }).click();
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children.length), 3);
  await page.locator('[data-node-id="workflow"]').dragTo(page.locator('[data-node-id="grid"]'), { targetPosition: { x: 8, y: 8 } });
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children[0].children[1].instanceId), 'workflow-owner');
  await page.locator('[data-node-id="workflow"]').focus();
  await page.keyboard.press('Alt+ArrowUp');
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children[0].children[0].id), 'workflow');
  const beforeDelete=await page.evaluate(()=>JSON.stringify(currentDocument));
  await page.keyboard.press('Delete');
  const deleteDialog=await confirmation();
  assert.equal(await page.evaluate(()=>JSON.stringify(currentDocument)),beforeDelete,'Delete key cannot bypass confirmation');
  await deleteDialog.getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await page.evaluate(()=>JSON.stringify(currentDocument)),beforeDelete);
  await page.locator('[data-node-id="workflow"]').focus();await page.keyboard.press('Delete');
  await (await confirmation()).getByRole('button',{name:'确认删除',exact:true}).click();
  assert.equal(await page.locator('[data-node-id="workflow"]').count(), 0);
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  assert.equal(await page.locator('[data-node-id="workflow"]').count(), 1);
  await page.locator('[data-node-id="grid"]').click({ position: { x: 8, y: 8 } });
  await page.getByRole('spinbutton', { name: '网格列数' }).fill('3');
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children[0].columns), 3);
  await workspace();await scope('workspace');
  assert.equal(await inspector.getByLabel('页面名称',{exact:true}).count(),0);
  assert.equal(await inspector.getByRole('button',{name:'删除组件',exact:true}).count(),0);
  await details('界面与外观');
  await page.getByLabel('风格预设', { exact: true }).selectOption('forest');
  await page.getByLabel('全局字号', { exact: true }).fill('18');
  await page.getByLabel('字体', { exact: true }).selectOption('serif');
  await page.getByText('自定义配色', { exact: true }).click();
  await page.getByLabel('浅色 · 页面背景', { exact: true }).fill('#f0e4d8');
  await page.getByLabel('浅色 · 主色', { exact: true }).fill('#135724');
  const canvas = page.locator('.gui-editor-canvas');
  const validTheme = await page.evaluate(() => JSON.stringify(window.currentDocument.theme));
  await page.getByLabel('浅色 · 正文', { exact: true }).fill('#ffffff');
  await page.getByRole('alert').filter({ hasText: '对比度必须至少 4.5' }).waitFor();
  assert.equal(await page.evaluate(() => JSON.stringify(window.currentDocument.theme)), validTheme, 'Invalid contrast must not enter the controlled draft');
  assert.ok(await canvas.isVisible(), 'Rejected appearance must not crash the editor');
  await page.getByLabel('浅色 · 正文', { exact: true }).fill('#172033');
  assert.equal(await page.getByRole('alert').count(), 0, 'A corrected palette clears the error and remains editable');
  assert.deepEqual(await canvas.evaluate(el => ({ background: getComputedStyle(el).backgroundColor, size: getComputedStyle(el).fontSize })), { background: 'rgb(240, 228, 216)', size: '18px' });
  assert.match(await canvas.evaluate(el => getComputedStyle(el).fontFamily), /serif/);
  await page.locator('[data-node-id="workflow"]').click({ position: { x: 8, y: 8 } });
  await details('选区外观');
  await page.getByLabel('选区外观 · 背景', { exact: true }).selectOption('primary');
  await page.getByLabel('选区外观 · 字号', { exact: true }).fill('24');
  await page.getByLabel('选区外观 · 内边距', { exact: true }).fill('20');
  const styled = await page.locator('[data-node-id="workflow"]').evaluate(el => ({ background: getComputedStyle(el).backgroundColor, size: getComputedStyle(el).fontSize, padding: getComputedStyle(el).padding }));
  assert.deepEqual(styled, { background: 'rgb(19, 87, 36)', size: '24px', padding: '20px' });
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children[0].children[0].instanceId), 'workflow-owner');
  await selectPage();await scope('page');
  assert.equal(await inspector.getByLabel('排列方式',{exact:true}).count(),0);
  assert.equal(await page.locator('#appearance-extra').count(),0);
  await details('页面外观');
  await page.getByLabel('页面外观 · 圆角', { exact: true }).fill('22');
  assert.equal(await canvas.evaluate(el => getComputedStyle(el).borderRadius), '22px');
  await page.getByRole('button', { name: '重置页面外观', exact: true }).click();
  assert.equal(await page.evaluate(() => Object.keys(window.currentDocument.pages[0].appearance).length), 0);
  await page.locator('[data-node-id="workflow"]').click({position:{x:8,y:8}});
  await details('选区外观');
  await page.getByRole('button', { name: '重置选区外观', exact: true }).click();
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children[0].children[0].instanceId), 'workflow-owner');
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  assert.equal(await page.locator('[data-node-id="workflow"]').evaluate(el => getComputedStyle(el).padding), '20px');
  await page.screenshot({ path: join(output, 'desktop.png'), fullPage: true });
  await workspace();await details('界面与外观');
  await page.getByRole('button', { name: '重置全局样式', exact: true }).click();
  assert.deepEqual(await page.evaluate(() => window.currentDocument.theme), {});
  assert.ok(await canvas.isVisible());
  await page.locator('[data-node-id="workflow"]').click({position:{x:8,y:8}});
  await details('选区外观');
  await page.getByRole('button', { name: '重置选区外观', exact: true }).click();
  await page.locator('[data-node-id="grid"]').click({ position: { x: 8, y: 8 } });
  await details('选区外观');
  await page.getByLabel('选区外观 · 字号', { exact: true }).fill('18');
  await page.getByLabel('选区外观 · 背景', { exact: true }).selectOption('primary');
  const descendant = page.locator('[data-node-id="workflow"]');
  assert.deepEqual(await descendant.evaluate(el => ({ size: getComputedStyle(el).fontSize, color: getComputedStyle(el).color })), { size: '18px', color: 'rgb(255, 255, 255)' }, 'Descendants inherit ancestor font size and on-primary text');
  assert.equal(await page.locator('[data-node-id="grid"]').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(54, 90, 203)');
  await descendant.click({ position: { x: 8, y: 8 } });
  await details('选区外观');
  await page.getByLabel('选区外观 · 文字颜色', { exact: true }).selectOption('primary');
  await page.getByRole('alert').filter({ hasText: '对比度' }).waitFor();
  assert.equal(await page.evaluate(() => window.currentDocument.pages[0].layout.children[0].children[0].appearance.textTone), undefined);
  assert.ok(await canvas.isVisible());
  await page.getByLabel('选区外观 · 文字颜色', { exact: true }).selectOption('default');
  assert.equal(await page.getByRole('alert').count(), 0);
  await page.screenshot({ path: join(output, 'inherited-appearance.png'), fullPage: true });
  // Breadcrumbs navigate to the actual parent and show only that scope's controls.
  await crumbs.getByRole('button',{name:/网格 · grid$/}).click();await scope('node');
  await inspector.getByLabel('网格列数',{exact:true}).waitFor();
  await crumbs.getByRole('button',{name:/页面 · Home$/}).click();await scope('page');
  assert.equal(await inspector.getByLabel('网格列数',{exact:true}).count(),0);
  await page.locator('[data-node-id="note"]').click({position:{x:8,y:8}});
  const textInput=inspector.getByLabel('文字内容',{exact:true});await textInput.fill('Typed');await textInput.press('End');await textInput.press('Backspace');
  assert.equal(await textInput.inputValue(),'Type');assert.equal(await page.getByRole('alertdialog').count(),0,'Typing Backspace never deletes a node');
  // Navigation deletion uses the same cancel-first confirmation and undo path.
  await page.locator('.gui-editor-frame-navigation').getByRole('button',{name:'Home',exact:true}).click();
  await inspector.getByRole('button',{name:'移除入口',exact:true}).click();
  await (await confirmation()).getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await page.evaluate(()=>currentDocument.navigation.length),1);
  await inspector.getByRole('button',{name:'移除入口',exact:true}).click();
  await (await confirmation()).getByRole('button',{name:'确认删除',exact:true}).click();
  assert.equal(await page.evaluate(()=>currentDocument.navigation.length),0);
  await page.getByRole('button',{name:'撤销',exact:true}).click();
  assert.equal(await page.evaluate(()=>currentDocument.navigation.length),1);
  // Deleting a page reports its dependent navigation references and is reversible.
  await page.getByRole('button',{name:'页面与结构',exact:true}).click();
  await page.getByRole('button',{name:'添加页面',exact:true}).click();await selectPage('新页面');
  await details('更多操作');await inspector.getByRole('button',{name:'删除页面',exact:true}).click();
  const pageDialog=await confirmation();await pageDialog.getByText(/1 个导航引用/).waitFor();
  await pageDialog.getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await page.evaluate(()=>currentDocument.pages.length),2);
  await inspector.getByRole('button',{name:'删除页面',exact:true}).click();
  await (await confirmation()).getByRole('button',{name:'确认删除',exact:true}).click();
  assert.equal(await page.evaluate(()=>currentDocument.pages.length),1);assert.equal(await page.evaluate(()=>currentDocument.navigation.length),1);
  await page.getByRole('button',{name:'撤销',exact:true}).click();assert.equal(await page.evaluate(()=>currentDocument.pages.length),2);
  await selectPage();await page.locator('[data-node-id="grid"]').click({position:{x:8,y:8}});
  await details('更多操作');await inspector.getByRole('button',{name:'删除组件',exact:true}).click();await confirmation();
  await page.evaluate(()=>replaceDocument({...structuredClone(currentDocument),name:'Updated externally'}));
  await page.getByRole('alertdialog').waitFor({state:'hidden'});
  await page.getByRole('alert').filter({hasText:'设计已更新'}).waitFor();
  assert.equal(await page.locator('[data-node-id="grid"]').count(),1,'An external document update invalidates pending deletion');
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.locator('.gui-editor-canvas').isVisible());
  assert.equal(await page.locator('.gui-editor-inspector').isVisible(), false);
  await page.getByRole('button', { name: '属性', exact: true }).click();
  assert.equal(await page.locator('#appearance-extra').count(),0,'Mobile node inspector does not mix in workspace settings');
  assert.equal(await page.locator('.gui-editor-canvas').isVisible(), false);
  await page.getByRole('button', { name: '页面与添加', exact: true }).click();
  await page.getByRole('button', { name: '页面与结构', exact: true }).click();
  assert.ok(await page.locator('.gui-editor-tree[aria-label="节点结构"]').isVisible());
  await page.locator('.gui-editor-tree[aria-label="节点结构"] button').first().click();
  assert.equal(await page.locator('.gui-editor-tree[aria-label="节点结构"] button').first().getAttribute('aria-pressed'), 'true');
  await page.getByRole('button', { name: '画布', exact: true }).click();
  await page.screenshot({ path: join(output, 'mobile.png'), fullPage: true });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.locator('[data-node-id="note"]').click({position:{x:8,y:8}});
  await page.locator('[data-node-id="note"]').focus();await page.keyboard.press('Delete');
  const mobileDialog=await confirmation(),mobileBox=await mobileDialog.boundingBox();
  assert.ok(mobileBox&&mobileBox.x>=0&&mobileBox.y>=0&&mobileBox.x+mobileBox.width<=390&&mobileBox.y+mobileBox.height<=844,'Deletion confirmation fits a small screen');
  await page.keyboard.press('Tab');assert.equal(await mobileDialog.getByRole('button',{name:'确认删除',exact:true}).evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('Tab');assert.equal(await mobileDialog.getByRole('button',{name:'取消',exact:true}).evaluate(node=>node===document.activeElement),true,'Confirmation traps keyboard focus');
  await page.keyboard.press('Escape');assert.equal(await page.locator('[data-node-id="note"]').count(),1);

  assert.deepEqual(errors, []);
  const report = { scopedInspector: true, selectableCanvasNavigation: true, breadcrumbs: true, deletionCancelConfirmUndo: true, externalUpdateCancelsDeletion: true, textBackspacePreserved: true, dragAndDrop: true, keyboard: true, undoRedo: true, resize: true, inheritedAppearance: true, invalidAppearanceRecovery: true, mobile: true, pageErrors: errors };
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ output, ...report }));
} catch (error) {
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  throw error;
} finally {
  await browser.close(); await new Promise(resolve => server.close(resolve));
  if (output !== temp) await rm(temp, { recursive: true, force: true });
}
