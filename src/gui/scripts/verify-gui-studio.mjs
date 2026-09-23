// Isolated browser integration: actual React, GUI includes and durable Host.
// The native DSH session is a stateful fixture; no model or business traffic runs.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile, rm, mkdir } from 'node:fs/promises';
import { tmpdir, homedir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { createGuiStudioHost } from '../integration/gui-studio-host.mjs';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let deps;
for (const base of [process.env.DSH_GUI_BROWSER_DEPS, join(repo, 'browser-tests/package.json'), join(homedir(), '.h2b/apps/gui/source/browser-tests/package.json')].filter(Boolean)) {
  try { const candidate = createRequire(resolve(base)); for (const name of ['react', 'react-dom/client', 'rolldown', 'playwright']) candidate.resolve(name); deps = candidate; break; } catch {}
}
if (!deps) throw new Error('Install GUI browser-test dependencies, or set DSH_GUI_BROWSER_DEPS to a package.json resolving React, React DOM, rolldown and Playwright.');
const { build } = await import(pathToFileURL(deps.resolve('rolldown')).href);
const { chromium } = deps('playwright');
const temp = await mkdtemp(join(tmpdir(), 'gui-studio-browser-'));
const output = process.env.GUI_STUDIO_ARTIFACTS ? resolve(process.env.GUI_STUDIO_ARTIFACTS) : null;
if (output) await mkdir(output, { recursive: true });
let browser, server;
try {
  const agentRequests = [];
  const handle = createGuiStudioHost({ root: join(temp, 'state/gui-studio') });
  const workspace = await readFile(join(repo, 'client/gui-workspace.inc.js'), 'utf8');
  const styleSource = (await readFile(join(repo, 'shared/gui-style.mjs'), 'utf8')).replace(/^export\s+/gm, '');
  const moduleRuntimeSource = await readFile(join(repo, 'client/gui-module-runtime.inc.js'), 'utf8');
  const layoutBundle = await readFile(join(repo, 'packages/gui-layout/client.js'), 'utf8');
  const studio = await readFile(join(repo, 'client/gui-studio.inc.js'), 'utf8');
  const authoringSource = await readFile(join(repo, 'client/gui-authoring.inc.js'), 'utf8');
  const profileSource = await readFile(join(repo, 'client/gui-profile.inc.js'), 'utf8');
  const visualEditorSource = await readFile(join(repo, 'client/gui-editor.inc.js'), 'utf8');
  const plugin = await readFile(join(repo, 'imskin-plugin.js'), 'utf8');
  let css = '';
  try { css = await readFile(join(repo, 'client/gui-studio.css'), 'utf8'); } catch {
    const start = plugin.indexOf('styles.insert(`'); if (start >= 0) css = plugin.slice(start + 'styles.insert(`'.length, plugin.indexOf('`)', start));
  }
  css += await readFile(join(repo, 'client/gui-theme.css'), 'utf8');
  css += await readFile(join(repo, 'client/gui-business.css'), 'utf8');
  css += await readFile(join(repo, 'client/gui-editor.css'), 'utf8');
  const entry = `import React from ${JSON.stringify(deps.resolve('react'))};
import {createRoot} from ${JSON.stringify(deps.resolve('react-dom/client'))};
import * as jsxRuntime from ${JSON.stringify(deps.resolve('react/jsx-runtime'))};
const host={async call(method,args){const r=await fetch('/rpc',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(args)});const value=await r.json();if(!r.ok)throw Object.assign(new Error(value.message),{code:value.code});return value;}};
const entries=new Map(),overlays=new Map();let rootEntry,layout;
const slots={inject(name,fn){fn();},register(meta,component){if(meta.name==='root')rootEntry={meta,component};else if(meta.name==='shell.overlay')overlays.set(meta.id,component);else entries.set(meta.key?meta.name+':'+meta.key:meta.name,component);return()=>{};}};
const notifyAppShell=()=>{};
const focusListeners=new Set();
const ctx={get(name){return name==='layout'?layout:name==='conversation'?{guiDesignContextVersion:1}:null;},slots,reflect:{provide(name,value){layout=value;return()=>{};}},effect(fn){fn();},timeout(fn,ms){const timer=setTimeout(fn,ms);return()=>clearTimeout(timer);},on(name,fn){if(name==='gui-design/before-send')focusListeners.add(fn);return()=>focusListeners.delete(fn);},theme:{getTheme(){return{active:{id:'light',colorScheme:'light',tokens:{}},themes:[{id:'light',colorScheme:'light',tokens:{}},{id:'dark',colorScheme:'dark',tokens:{}}]};}}};
window.__ModuleLoader__={load(def){const exported=def.factory(name=>{if(name==='react')return React;if(name==='react/jsx-runtime')return jsxRuntime;if(name==='@deepseek-ai/dsh-client-runtime/client')return{defineStore:spec=>spec};throw new Error('Unexpected layout dependency '+name);});exported.apply(ctx);}};
${layoutBundle}
const spec=rootEntry.meta.store();let panelState=spec.init();const panelListeners=new Set();
const actions=Object.fromEntries(Object.entries(spec.actions).map(([name,action])=>[name,(...args)=>{const next=structuredClone(panelState);action(next,...args);if(JSON.stringify(next)!==JSON.stringify(panelState)){panelState=next;panelListeners.forEach(fn=>fn());}}]));
rootEntry.meta.inject(actions);
function usePanels(selector){const value=React.useSyncExternalStore(fn=>{panelListeners.add(fn);return()=>panelListeners.delete(fn);},()=>panelState);return selector(value);}
const snapshotOf=value=>value;
const currentAppSurface=()=> 'messages';
const h2bControlState={section:'kanban',runScope:'agent-task'};
const workspaces={archivedSessionIds:[]};
const listedSessions=()=>[{id:'fixture-direct'},{id:'fixture-native'}];
const demoEntry=id=>({humanChat:id==='fixture-direct'});
const createdSessionId=value=>value.id;
const sessions={current:'fixture-native',create:async()=>({id:'fixture-native'}),open(id){window.openedSession=id;},binding(id){return {session:{rename:async()=>{},prompt:async(parts)=>{window.lastPrompt=parts[0].text;const r=await fetch('/agent',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sessionId:id,prompt:parts[0].text})});if(!r.ok)throw new Error(await r.text());return {ok:true,value:{accepted:true}};}}};}};
const openMostRecentMessageSession=async()=>{window.lastNavigation='dsh.conversation';};
const openH2bDirectory=async()=>{window.lastNavigation='h2b.contacts';};
const selectH2bControlSection=section=>{window.lastNavigation=section;};
const openH2bControl=async()=>{if(window.controlGate){window.controlWaiting=true;await window.controlGate;window.controlWaiting=false;}};
${workspace}
${styleSource}
${moduleRuntimeSource}
${visualEditorSource}
${profileSource}
${authoringSource}
${studio}
let previewRoot;
window.fixture={async beforeSend(sessionId){const request={sessionId,text:'ordinary discussion',contextText:''};await Promise.all([...focusListeners].map(fn=>fn(request)));return request;},navigationButtons:guiNavigationButtons,state:guiState,open:guiOpen,call:guiCall,notify:guiNotify,navigate:guiNavigate,clearPage:guiClearPage,refreshProfile:guiRefreshProfile,navigateTarget:guiNavigateTarget,holdControl(){window.controlGate=new Promise(resolve=>window.releaseControl=resolve);},async releaseControl(){window.releaseControl();window.controlGate=null;await window.pendingNavigation;},mountPreview(value,tool='h2b_gui_preview'){previewRoot ||= createRoot(document.getElementById('preview-fixture'));const View=entries.get('tool.call.toolview:'+tool);if(!View)throw new Error('Missing GUI tool result view');previewRoot.render(React.createElement(View,{block:{kind:'tool-result',content:[{type:'text',text:JSON.stringify(value)}]}}));},clearPreview(){previewRoot?.render(null);}};
window.workflowMounts=0;window.workflowUnmounts=0;
guiModuleViews['h2b.workflow']=function WorkflowControllerFixture(props){const[value,setValue]=React.useState('');React.useEffect(()=>{window.workflowMounts++;return()=>window.workflowUnmounts++;},[]);return React.createElement('textarea',{'aria-label':'Workflow editor '+props.instanceId,value,onChange:e=>setValue(e.target.value)});};
guiModuleViews['h2b.kanban']=function KanbanFixture(props){const[value,setValue]=React.useState('');return React.createElement('textarea',{'aria-label':'Kanban view-local filter'+(props.instanceId?' '+props.instanceId:''),value,onChange:e=>setValue(e.target.value)});};
window.nativeMounts=0;window.nativeUnmounts=0;
function NativeConversation(){const[text,setText]=React.useState(()=>localStorage.getItem('fixture-input')||'');React.useEffect(()=>{window.nativeMounts++;return()=>window.nativeUnmounts++;},[]);return React.createElement('section',{'data-testid':'native-conversation'},React.createElement('textarea',{'aria-label':'Native unsent message',value:text,onChange:e=>{setText(e.target.value);localStorage.setItem('fixture-input',e.target.value);}}),React.createElement('button',{'aria-label':'Native send',onClick:async()=>{const request=await window.fixture.beforeSend('fixture-native');await sessions.binding('fixture-native').session.prompt([{type:'text',text:request.contextText+'\\n\\n'+text}]);setText('');localStorage.removeItem('fixture-input');}},'发送'),React.createElement('div',{id:'preview-fixture'}));}
createRoot(document.getElementById('root')).render(React.createElement(rootEntry.component,{useStore:usePanels,useSessions:selector=>selector({current:'fixture-native',byId:{'fixture-native':{blank:false}}}),actions,renderSlot(name){if(name==='conversation')return React.createElement(NativeConversation);if(name==='shell.overlay')return React.createElement(React.Fragment,null,...Array.from(overlays,([key,View])=>React.createElement(View,{key})));const View=entries.get(name);return View?React.createElement(View):null;}}));
`;
  await writeFile(join(temp, 'entry.mjs'), entry);
  await build({ input: join(temp, 'entry.mjs'), output: { file: join(temp, 'bundle.js'), format: 'iife' }, transform: { define: { 'process.env.NODE_ENV': '"production"' } } });
  const bundle = await readFile(join(temp, 'bundle.js'));
  server = createServer(async (request, response) => {
    try {
      if (request.url === '/bundle.js') { response.setHeader('content-type', 'text/javascript'); response.end(bundle); return; }
      if (request.method === 'GET') { response.setHeader('content-type', 'text/html'); response.end('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#root{height:100%;margin:0;font-family:system-ui}button,input,textarea{font:inherit}*{box-sizing:border-box}' + css + '</style><div id="root"></div><script src="/bundle.js"></script>'); return; }
      let body = ''; for await (const chunk of request) { body += chunk; if (body.length > 262144) throw new Error('Fixture request too large'); }
      const args = JSON.parse(body);
      let value;
      if (request.url === '/agent') {
        const id = args.prompt.match(/界面 ID：([^\n]+)/)?.[1] || JSON.parse(args.prompt.split('\n')[1]).draftId;
        const context = { source: 'agent', sessionId: args.sessionId };
        const draft = await handle({ operation: 'get', id }, context);
        agentRequests.push({ id, revision: draft.revision, document: structuredClone(draft.document) });
        draft.document.name = 'Agent designed workspace';
        draft.document.theme = { mode: 'dark', density: 'compact', accent: '#2468aa' };
        draft.document.pages[0].layout = { type: 'Tabs', id: 'tabs', labels: ['Conversation', 'Kanban'], children: [{ type: 'Feature', id: 'conversation', instanceId: 'fixture-conversation', feature: 'dsh.conversation' }, { type: 'Feature', id: 'kanban', instanceId: 'fixture-kanban', feature: 'h2b.kanban', view: 'readOnly' }] };
        value = await handle({ operation: 'update', id, baseRevision: draft.revision, document: draft.document }, context);
        await handle({ operation: 'validate', id, revision: value.revision }, context);
        await handle({ operation: 'preview', id, revision: value.revision }, context);
      } else value = await handle(args, { source: 'user' });
      response.setHeader('content-type', 'application/json'); response.end(JSON.stringify(value));
    } catch (error) { response.statusCode = 400; response.setHeader('content-type', 'application/json'); response.end(JSON.stringify({ code: error.code, message: error.message })); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(8000);
  const errors = [], external = [];
  page.on('pageerror', error => { errors.push(error.message); console.error('Browser:', error.message); });
  page.on('request', request => { if (!request.url().startsWith(origin)) external.push(request.url()); });
  const action = name => page.locator('[data-gui-action="' + name + '"]');
  async function waitDraft(name, revision) {
    await page.locator('.gui-studio-heading strong').filter({hasText:name}).waitFor({timeout:10000});
    await page.locator('.gui-studio-heading [role=status]').filter({hasText:'草稿已保存 · 修订 '+revision}).waitFor({timeout:10000});
    if(await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).isVisible()) await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).click();
  }
  async function workspaceAction(name) {
    if (!await action(name).isVisible()) await action('workspace-menu').click();
    await action(name).click();
  }
  async function allFeatures() {
    if (!await page.getByRole('button', {name:'全部功能',exact:true}).isVisible()) await action('workspace-menu').click();
    await page.getByRole('button', {name:'全部功能',exact:true}).click();
  }
  async function openStudio(draftLabel) {
    await workspaceAction('studio');
    await page.locator('[data-gui-view="manage"]').waitFor();
    if (draftLabel) { await page.locator('.gui-studio-library').getByRole('button', {name:draftLabel,exact:true}).click(); await page.locator('[data-gui-view=edit]').waitFor(); await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).click(); }
  }
  await page.goto(origin);
  await allFeatures();
  await page.getByRole('button', { name: 'H2B 直聊', exact: true }).click();
  assert.equal(await page.evaluate(() => window.openedSession), 'fixture-direct');
  await allFeatures();
  await page.getByRole('button', { name: 'Agent 会话', exact: true }).click();
  assert.equal(await page.evaluate(() => window.openedSession), 'fixture-native');
  await page.getByLabel('Native unsent message').fill('preserve my unsent message');
  await openStudio();
  await page.getByRole('button', { name: '新建界面', exact: true }).click();
  await action('save').waitFor();
  assert.equal(await page.getByRole('alert').count(), 0);
  await page.locator('.gui-agent-dock').waitFor();
  assert.equal(await page.locator('.gui-design-request').count(),0);
  const focusCard=page.getByRole('region',{name:'设计讨论焦点'});
  await page.locator('.gui-editor-tree[aria-label="节点结构"] button').last().click();
  const selectedFocus=await action('locate-focus').textContent();
  assert.notEqual(selectedFocus,'整个工作空间');
  await action('lock-focus').click();
  await page.getByRole('button',{name:'工作空间设置',exact:true}).click();
  assert.equal(await action('locate-focus').textContent(),selectedFocus,'Locked focus survives canvas selection');
  const nativeFocus=await page.evaluate(()=>fixture.beforeSend('fixture-native'));
  assert.match(nativeFocus.contextText,/GUI 设计焦点/);
  const focusSnapshot=JSON.parse(nativeFocus.contextText.split('\n')[1]);
  assert.ok(focusSnapshot.selection.includes('/'));
  assert.notEqual(focusSnapshot.selection,'@workspace');
  assert.equal(focusSnapshot.revision,1);
  assert.equal((await page.evaluate(()=>fixture.beforeSend('fixture-direct'))).contextText,'','Other sessions get no design context');
  await action('lock-focus').click();
  assert.equal(await action('locate-focus').textContent(),'整个工作空间','Unlock resumes following selection');
  await page.getByLabel('Native unsent message').fill('Keep my unsubmitted design request');
  await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).click();
  assert.equal(await page.locator('.gui-editor-inspector').isVisible(),true);
  assert.equal(await page.locator('.gui-agent-seat').isVisible(),true);
  await page.getByLabel('属性面板高度').fill('280');
  await page.locator('.gui-agent-dock').getByRole('button',{name:'收起属性',exact:true}).click();
  assert.equal(await page.getByLabel('Native unsent message').inputValue(),'Keep my unsubmitted design request');
  await page.getByLabel('Native unsent message').fill('Use dark mode and show Workflow beside my Agent conversation');
  await page.getByLabel('Native send',{exact:true}).click();
  await waitDraft('Agent designed workspace', 2);
  assert.equal(await page.getByLabel('Native unsent message').inputValue(),'');
  await page.getByLabel('Native unsent message').fill('preserve my unsent message');
  assert.match(await page.evaluate(() => window.lastPrompt), /h2b_gui_context/);
  assert.equal(await page.evaluate(() => window.openedSession), 'fixture-native');
  const initialDraft = (await handle({ operation: 'list' }, { source: 'user' })).drafts[0];
  const historicalPreview = await handle({ operation: 'preview', id: initialDraft.id, revision: 2 }, { source: 'agent', sessionId: 'fixture-native' });
  // Precision editing uses the same authoritative optimistic revision as tools.
  await page.getByText('界面定义与精确编辑', { exact: true }).click();
  const editor = page.getByLabel('GUI JSON');
  const document = JSON.parse(await editor.inputValue()); document.name = 'My reviewed workspace';
  await editor.fill(JSON.stringify(document));
  await page.getByRole('button', { name: '保存草稿', exact: true }).click();
  await waitDraft('My reviewed workspace', 3);
  const beforeDeleteCancel=await readFile(join(temp,'state/gui-studio/store.json'),'utf8');
  await page.locator('.gui-editor-more > summary').click();
  await page.getByRole('button',{name:'删除草稿',exact:true}).click();
  const deleteDraftDialog=page.getByRole('alertdialog',{name:'删除整个界面草稿',exact:true});
  await deleteDraftDialog.waitFor();
  assert.equal(await deleteDraftDialog.getByRole('button',{name:'取消',exact:true}).evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('Escape');
  await deleteDraftDialog.waitFor({state:'hidden'});
  assert.equal(await page.locator('[data-gui-view=edit]').isVisible(),true,'Escape cancels deletion without exiting Studio');
  await page.getByRole('button',{name:'删除草稿',exact:true}).click();
  await deleteDraftDialog.getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await readFile(join(temp,'state/gui-studio/store.json'),'utf8'),beforeDeleteCancel,'Cancel whole-draft deletion preserves durable data');
  await page.getByRole('button', { name: '退出编辑', exact: true }).click();
  const beforeReplay = await readFile(join(temp, 'state/gui-studio/store.json'), 'utf8');
  for (const tool of ['h2b_gui_preview', 'h2b_gui_prepare_publish']) {
    await page.evaluate(({ value, tool }) => window.fixture.mountPreview(value, tool), { value: historicalPreview, tool });
    await page.getByText('Agent designed workspace · v2', { exact: true }).waitFor();
    await page.getByText('查看界面设计', { exact: true }).click();
    assert.equal(await page.locator('#preview-fixture [data-gui-node="conversation"]').count(), 1);
    assert.equal(await readFile(join(temp, 'state/gui-studio/store.json'), 'utf8'), beforeReplay);
  }
  await page.getByRole('button', { name: '打开当前草稿', exact: true }).click();
  await waitDraft('My reviewed workspace', 3);
  assert.equal((await handle({ operation: 'get', id: initialDraft.id }, { source: 'user' })).revision, 3);
  await page.evaluate(() => window.fixture.clearPreview());
  await page.getByRole('button', { name: '交互预览', exact: true }).click();
  await page.getByRole('button', { name: '结束预览', exact: true }).waitFor();
  assert.equal((await handle({ operation: 'profile' }, { source: 'user' })).releaseId, null);
  assert.equal(await page.getByLabel('Native unsent message').inputValue(), 'preserve my unsent message');
  await page.evaluate(() => window.fixture.navigate(undefined, 'home'));
  await page.getByRole('tab', { name: 'Kanban', exact: true }).click();
  await page.locator('textarea[aria-label^="Kanban view-local filter"]:visible').fill('preserve kanban local filter');
  await page.getByRole('tab', { name: 'Conversation', exact: true }).click();
  await page.getByRole('tab', { name: 'Kanban', exact: true }).click();
  assert.equal(await page.locator('textarea[aria-label^="Kanban view-local filter"]:visible').inputValue(), 'preserve kanban local filter');
  await allFeatures();
  await page.getByRole('navigation', { name: '全部功能', exact: true }).getByRole('button', { name: 'Agent 会话', exact: true }).click();
  // A default Workflow node must reuse its native owner, never render an embedded controller.
  const trialDocument = await page.evaluate(() => structuredClone(window.fixture.state.trial));
  // The applied shell has the same frame during trial: one global navigation,
  // no personal-page chrome, and a reversible native menu without remounting.
  await page.evaluate(() => { const d = structuredClone(fixture.state.trial); d.layout = { ...d.layout, navigation: 'top', nativeSidebar: false }; fixture.state.trial = d; fixture.notify(); });
  await page.evaluate(() => fixture.navigate(undefined, 'home'));
  assert.equal(await page.getByRole('navigation', { name: '个人工作空间导航', exact: true }).count(), 1);
  assert.equal(await page.getByRole('navigation', { name: '自定义工作空间', exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '返回工作区', exact: true }).count(), 0);
  await page.waitForFunction(() => document.querySelector('[data-native-sidebar]')?.getAttribute('data-gui-object-panel') === 'inline');
  assert.equal(await page.locator('[data-native-sidebar]').getAttribute('data-gui-object-panel'), 'inline');
  assert.doesNotMatch(await page.locator('[data-gui-layout]').evaluate(e => e.style.gridTemplateColumns), /^0px /);
  await workspaceAction('native-navigation');
  assert.doesNotMatch(await page.locator('[data-gui-layout]').evaluate(e => e.style.gridTemplateColumns), /^0px /);
  await workspaceAction('native-navigation');
  await page.waitForFunction(() => document.querySelector('[data-native-sidebar]')?.getAttribute('data-gui-object-panel') === 'inline');
  assert.equal(await page.locator('[data-native-sidebar]').getAttribute('data-gui-object-panel'), 'inline');
  assert.doesNotMatch(await page.locator('[data-gui-layout]').evaluate(e => e.style.gridTemplateColumns), /^0px /);
  await page.locator('[data-gui-action=objects]').click();
  assert.match(await page.locator('[data-gui-layout]').evaluate(e => e.style.gridTemplateColumns), /^0px /);
  await page.locator('[data-gui-action=objects]').click();
  assert.equal(await page.getByLabel('Native unsent message').inputValue(), 'preserve my unsent message');
  assert.deepEqual(await page.evaluate(() => ({ mounts: nativeMounts, unmounts: nativeUnmounts })), { mounts: 1, unmounts: 0 });

  await page.evaluate(() => { const d = structuredClone(window.fixture.state.trial); d.schemaVersion=1; d.pages[0].layout = { type:'Feature', id:'workflow', feature:'h2b.workflow' }; window.fixture.state.trial=d; });
  await page.evaluate(() => window.fixture.navigate(undefined, 'home'));
  await page.getByRole('region', { name: '整体工作空间', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '返回工作区', exact: true }).count(), 0);
  assert.equal(await page.getByRole('navigation', { name: '自定义工作空间', exact: true }).count(), 0);
  assert.equal(await page.evaluate(() => window.workflowMounts), 0);
  assert.equal(await page.evaluate(() => window.nativeMounts), 1);
  await allFeatures();
  await page.getByRole('navigation', { name: '全部功能', exact: true }).getByRole('button', { name: 'Agent 会话', exact: true }).click();
  // Delayed native activation cannot resurrect a page after another navigation or default exit.
  await page.evaluate(() => { window.fixture.holdControl(); window.pendingNavigation=window.fixture.navigate(undefined,'home'); });
  await page.waitForFunction(() => window.controlWaiting === true);
  await page.evaluate(() => window.fixture.navigate('h2b.contacts'));
  await page.evaluate(() => window.fixture.releaseControl());
  assert.equal(await page.evaluate(() => window.fixture.state.pageId), null);
  assert.equal(await page.evaluate(() => window.lastNavigation), 'h2b.contacts');
  await page.evaluate(() => { window.fixture.holdControl(); window.pendingNavigation=window.fixture.navigate(undefined,'home'); });
  await page.waitForFunction(() => window.controlWaiting === true);
  await page.evaluate(() => { window.fixture.state.trial=null;window.fixture.clearPage();window.fixture.notify(); });
  await page.evaluate(() => window.fixture.releaseControl());
  assert.equal(await page.evaluate(() => window.fixture.state.pageId), null);
  assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-surface'), 'native');
  // v2 mounts full modules in flat host-owned seats. Equal-size reordering and
  // different layout parents must preserve two independent editor controllers.
  await page.evaluate(() => {
    const conversation={type:'Feature',id:'agent',instanceId:'fixture-conversation',feature:'dsh.conversation'};
    const workflow={type:'Feature',id:'workflow',instanceId:'workflow-one',feature:'h2b.workflow'};
    const second={type:'Feature',id:'workflow-two',instanceId:'workflow-two',feature:'h2b.workflow'};
    const kanban={type:'Feature',id:'kanban',instanceId:'fixture-kanban',feature:'h2b.kanban',view:'readOnly'};
    window.fixture.state.trial={schemaVersion:2,id:'multi-fixture',name:'Multi module fixture',kind:'shell',navigation:[],layout:{navigation:'left'},pages:[
      {id:'home',title:'Composition',layout:{type:'Grid',id:'grid',columns:2,children:[conversation,workflow,second,kanban]}},
      {id:'other',title:'Other page',layout:{type:'Text',id:'text',text:'Another page'}}
    ]};
  });
  await page.evaluate(() => window.fixture.navigate(undefined, 'home'));
  await page.getByLabel('Workflow editor native:h2b.workflow').fill('retain first controller draft');
  await page.getByLabel('Workflow editor auth:multi-fixture:workflow-two').fill('retain second independent draft');
  await page.getByLabel('Kanban view-local filter auth:multi-fixture:fixture-kanban').fill('retain v2 kanban state');
  await page.evaluate(() => {
    const d=structuredClone(window.fixture.state.trial),children=d.pages[0].layout.children;
    d.pages[0].layout={type:'Stack',id:'reparent',children:[{type:'Grid',id:'inner',columns:2,children:[children[2],children[1],children[0],children[3]]}]};
    window.fixture.state.trial=d;window.fixture.notify();
  });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.getByLabel('Workflow editor native:h2b.workflow').inputValue(), 'retain first controller draft');
  assert.equal(await page.getByLabel('Workflow editor auth:multi-fixture:workflow-two').inputValue(), 'retain second independent draft');
  await page.evaluate(() => window.fixture.navigate(undefined,'other'));
  await page.evaluate(() => window.fixture.navigate(undefined,'home'));
  await page.getByLabel('Workflow editor native:h2b.workflow').waitFor();
  assert.equal(await page.getByLabel('Workflow editor auth:multi-fixture:workflow-two').inputValue(), 'retain second independent draft');
  assert.deepEqual(await page.evaluate(() => ({mounts:window.workflowMounts,unmounts:window.workflowUnmounts})),{mounts:2,unmounts:0});
  assert.equal(await page.getByLabel('Kanban view-local filter auth:multi-fixture:fixture-kanban').inputValue(), 'retain v2 kanban state');
  // Page presentation is local to its native/business surfaces, never the shell.
  const shellBackground = await page.locator('[data-gui-layout]').evaluate(node=>getComputedStyle(node).getPropertyValue('--gui-style-background'));
  await page.evaluate(() => {
    const d=structuredClone(window.fixture.state.trial); d.kind='page'; d.theme={preset:'ocean',mode:'dark',typography:{font:'serif',size:18},radius:16};
    window.fixture.state.trial=d;window.fixture.notify();
  });
  await page.waitForFunction(()=>document.querySelector('[data-gui-module-instance="native:h2b.workflow"]')?.style.getPropertyValue('--gui-style-background')==='#0c202c');
  await page.waitForFunction(()=>document.querySelector('[data-native-conversation]')?.style.getPropertyValue('--gui-style-background')==='#0c202c',null,{timeout:5000});
  assert.equal(await page.locator('[data-gui-layout]').evaluate(node=>getComputedStyle(node).getPropertyValue('--gui-style-background')),shellBackground);
  await page.evaluate(()=>{const d=structuredClone(window.fixture.state.trial);d.theme={preset:'forest',mode:'light',typography:{font:'mono',size:16}};window.fixture.state.trial=d;window.fixture.notify();});
  await page.waitForFunction(()=>document.querySelector('[data-gui-module-instance="auth:multi-fixture:workflow-two"]')?.style.getPropertyValue('--gui-style-background')==='#f1f7f2');
  await page.evaluate(()=>{
    const d=structuredClone(window.fixture.state.trial);
    d.pages[0].appearance={radius:18,fontSize:19};
    function visit(node){if(node.type==='Feature')node.appearance=node.feature==='dsh.conversation'?{radius:8,fontSize:17}:node.id==='workflow'?{surface:'primary',fontSize:16}:{surface:'surface',fontSize:20};for(const child of node.children||[])visit(child);}
    visit(d.pages[0].layout);window.fixture.state.trial=d;window.fixture.notify();
  });
  await page.waitForFunction(()=>getComputedStyle(document.querySelector('[data-gui-module-instance="native:h2b.workflow"]')).backgroundColor==='rgb(40, 100, 67)');
  assert.equal(await page.locator('[data-gui-module-instance="auth:multi-fixture:workflow-two"]').evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(255, 255, 255)');
  await page.waitForFunction(()=>document.querySelector('[data-native-conversation]')?.style.fontSize==='17px');
  assert.equal(await page.locator('[data-native-conversation]').evaluate(node=>node.style.borderRadius),'8px');
  assert.equal(await page.locator('[data-gui-module-instance="auth:multi-fixture:workflow-two"]').evaluate(node=>getComputedStyle(node).fontSize),'20px');
  assert.equal(await page.locator('[data-gui-layout]').evaluate(node=>getComputedStyle(node).getPropertyValue('--gui-style-background')),shellBackground);
  if (output) await page.screenshot({path:join(output,'styled-modules.png'),fullPage:true});
  assert.equal(await page.getByLabel('Workflow editor native:h2b.workflow').inputValue(),'retain first controller draft');
  assert.equal(await page.getByLabel('Workflow editor auth:multi-fixture:workflow-two').inputValue(),'retain second independent draft');
  assert.deepEqual(await page.evaluate(()=>({mounts:window.workflowMounts,unmounts:window.workflowUnmounts})),{mounts:2,unmounts:0});
  await allFeatures();
  await page.getByRole('navigation', { name: '全部功能', exact: true }).getByRole('button', { name: 'Agent 会话', exact: true }).click();
  await page.waitForFunction(()=>!document.querySelector('[data-native-conversation]')?.style.getPropertyValue('--gui-style-background'));
  await page.evaluate(d => { window.fixture.state.trial=d; window.fixture.notify(); }, trialDocument);
  await openStudio();
  await page.getByRole('button', { name: 'My reviewed workspace · v3', exact: true }).click();
  await page.getByRole('button', { name: '发布并启用', exact: true }).click();
  await page.locator('.gui-use-notice').filter({hasText:/已启用 .+，下次正常打开继续使用。/}).waitFor();
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  const firstProfile = await handle({ operation: 'profile' }, { source: 'user' }); assert.equal(firstProfile.release.document.name, document.name);
  const usingLocation = await page.evaluate(()=>({pageId:fixture.state.pageId,pageReleaseId:fixture.state.pageReleaseId}));
  await workspaceAction('edit-current');
  await waitDraft('My reviewed workspace',3);
  await action('preview').click();
  await action('back-to-editor').click();
  if(await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).isVisible()) await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).click();
  await action('exit').click();
  assert.deepEqual(await page.evaluate(()=>({pageId:fixture.state.pageId,pageReleaseId:fixture.state.pageReleaseId})),usingLocation,'Edit-preview-return-exit restores the original page');
  assert.equal(await page.locator('.gui-system-bar button').count(),1,'Use mode exposes one workspace entry');
  // Editing current must target the published source and never mutate its release.
  await workspaceAction('edit-current');
  await waitDraft('My reviewed workspace',3);
  assert.equal(await page.locator('.gui-top-navigation').isVisible(),false,'Business navigation is absent while editing');
  await page.getByText('界面定义与精确编辑', { exact: true }).click();
  const second = JSON.parse(await editor.inputValue()); second.name = 'Next version';
  await editor.fill(JSON.stringify(second)); await page.getByRole('button', { name: '保存草稿', exact: true }).click();
  await waitDraft('Next version', 4);
  assert.equal((await handle({ operation: 'profile' }, { source: 'user' })).release.document.name, document.name);
  await page.getByRole('button', { name: '发布并启用', exact: true }).click();
  await page.locator('.gui-use-notice').filter({hasText:/已启用 .+，下次正常打开继续使用。/}).waitFor();
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  await page.evaluate(() => window.fixture.navigate(undefined, 'home'));
  assert.equal(await page.evaluate(() => window.fixture.state.pageId), 'home');
  const beforeRemoteApply = await handle({ operation:'profile' }, { source:'user' });
  await handle({ operation:'apply', releaseId:firstProfile.releaseId, baseRevision:beforeRemoteApply.revision }, { source:'user' });
  await page.evaluate(() => window.fixture.refreshProfile());
  assert.equal(await page.evaluate(() => window.fixture.state.pageId), null);
  assert.equal(await page.evaluate(() => window.fixture.state.nativeSessionId), null);
  await openStudio();
  await page.locator('.gui-release-list summary').click();
  await page.getByRole('button', { name: 'My reviewed workspace · v3', exact: true }).click();
  await page.waitForFunction(() => window.fixture.state.applied?.name === 'My reviewed workspace');
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  await openStudio();
  // An exported package re-enters through the validated user import path.
  const draft = (await handle({ operation: 'list' }, { source: 'user' })).drafts[0];
  const exported = await handle({ operation: 'export', id: draft.id }, { source: 'user' });
  if (await action('manage').isVisible()) await action('manage').click();
  await page.waitForFunction(() => document.querySelector('input[type=file]')?.disabled === false);
  await page.locator('input[type=file]').setInputFiles({ name: 'workspace.gui.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(exported)) });
  await waitDraft('Next version', 1);
  assert.equal((await handle({ operation: 'list' }, { source: 'user' })).drafts.length, 2);
  await page.getByRole('navigation',{name:'选区层级',exact:true}).getByRole('button',{name:'工作空间',exact:true}).click();
  assert.equal(await page.getByLabel('导航布局',{exact:true}).isVisible(),true,'Navigation layout selector remains visible outside collapsed appearance settings');
  await page.getByLabel('导航布局',{exact:true}).selectOption('native');
  const nativeStructure=page.locator('.gui-editor-frame-structure[data-navigation=native]');
  assert.equal(await nativeStructure.locator('.gui-editor-frame-column').count(),3);
  for(const label of ['一级应用','二级功能','三级列表']) await nativeStructure.getByText(label,{exact:true}).waitFor();
  const editorContrast=await page.evaluate(()=>{
    const rgba=value=>{const channels=value.match(/[\d.]+/g)?.map(Number)||[];return [channels[0]||0,channels[1]||0,channels[2]||0,channels[3]??1];};
    const blend=(front,back)=>front.slice(0,3).map((value,index)=>value*front[3]+back[index]*(1-front[3]));
    function background(element){const layers=[];for(let node=element;node;node=node.parentElement)layers.push(rgba(getComputedStyle(node).backgroundColor));return layers.reverse().reduce((color,layer)=>blend(layer,color),[255,255,255]);}
    const luminance=color=>color.map(value=>{const c=value/255;return c<=0.04045?c/12.92:((c+0.055)/1.055)**2.4;}).reduce((sum,value,index)=>sum+value*[0.2126,0.7152,0.0722][index],0);
    return ['.gui-editor-canvas','.gui-editor-node','.gui-editor-frame-column strong'].map(selector=>{
      const element=document.querySelector(selector),bg=background(element),fg=blend(rgba(getComputedStyle(element).color),bg),a=luminance(bg),b=luminance(fg);
      return {selector,background:bg,text:fg,contrast:(Math.max(a,b)+0.05)/(Math.min(a,b)+0.05)};
    });
  });
  for(const item of editorContrast) assert.ok(item.contrast>=4.5,`Dark draft editor text must remain legible: ${JSON.stringify(item)}`);
  if(output) await writeFile(join(output,'editor-contrast.json'),JSON.stringify(editorContrast,null,2)+'\n');
  if(output) await page.screenshot({path:join(output,'studio-native-structure.png'),fullPage:true});
  const profileBeforeNativePreview=await handle({operation:'profile'},{source:'user'});
  await action('preview').click();
  await action('back-to-editor').waitFor();
  assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-navigation'),'left');
  assert.doesNotMatch(await page.locator('[data-gui-layout]').evaluate(element=>element.style.gridTemplateColumns),/^0px /,'Native navigation must expand its sidebar');
  assert.equal(await page.locator('.gui-top-navigation').count(),0);
  assert.equal(await page.evaluate(()=>fixture.navigationButtons()),null,'Native navigation restores the stock app rail');
  assert.deepEqual(await handle({operation:'profile'},{source:'user'}),profileBeforeNativePreview,'Navigation preview does not activate a draft');
  await action('back-to-editor').click();
  if(await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).isVisible()) await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).click();
  await page.getByRole('navigation',{name:'选区层级',exact:true}).getByRole('button',{name:'工作空间',exact:true}).click();
  await page.getByRole('button',{name:'查看选区属性',exact:true}).click();
  await page.getByText('界面与外观',{exact:true}).click();
  await page.getByLabel('界面名称', {exact:true}).fill('Inspector reviewed');
  await page.getByLabel('导航布局',{exact:true}).selectOption('top');
  await page.getByLabel('主题').selectOption('light');
  // Leaving authoring through business navigation must retain the unsaved visual preview.
  await page.evaluate(()=>fixture.navigate('h2b.contacts'));
  await page.waitForFunction(()=>!fixture.state.open);
  await openStudio('Next version · v2');
  await page.getByRole('navigation',{name:'选区层级',exact:true}).getByRole('button',{name:'工作空间',exact:true}).click();
  await page.getByRole('button',{name:'查看选区属性',exact:true}).click();
  await page.getByText('界面与外观',{exact:true}).click();
  assert.equal(await page.getByLabel('界面名称',{exact:true}).inputValue(),'Inspector reviewed');
  assert.equal(await page.getByLabel('导航布局',{exact:true}).inputValue(),'top');
  assert.equal(await page.getByLabel('主题').inputValue(),'light');
  await page.getByRole('button', {name:'保存草稿',exact:true}).click();
  await waitDraft('Inspector reviewed', 3);
  const inspected = (await handle({operation:'list'}, {source:'user'})).drafts.find(d => d.document.name === 'Inspector reviewed');
  assert.equal(inspected.document.layout.navigation,'top'); assert.equal(inspected.document.theme.mode,'light');
  if(await page.locator('.gui-agent-dock').isVisible()) await page.locator('.gui-agent-dock').getByRole('button',{name:'收起',exact:true}).click();
  for (const width of [390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    const dimensions = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }));
    assert.ok(dimensions.document <= dimensions.viewport + 1, `GUI overflows at ${width}: ${JSON.stringify(dimensions)}`);
    for (const name of ['exit','manage','save','preview','apply','agent']) {
      const bounds=await action(name).boundingBox();
      assert.ok(bounds&&bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=width+1&&bounds.y+bounds.height<=1000,`Primary action ${name} must fit viewport at ${width}: ${JSON.stringify(bounds)}`);
    }
    if (output) await page.screenshot({ path: join(output, `studio-${width}.png`), fullPage: true });
  }
  await action('manage').click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '切换为原生界面', exact: true }).click();
  await page.waitForFunction(() => window.fixture.state.applied === null);
  assert.equal(await page.getByLabel('Native unsent message').inputValue(), 'preserve my unsent message');
  assert.deepEqual(await page.evaluate(() => ({ mounts: window.nativeMounts, unmounts: window.nativeUnmounts })), { mounts: 1, unmounts: 0 });
  await page.reload();
  await page.getByRole('button', { name: '工作空间', exact: true }).waitFor();
  assert.equal((await handle({ operation: 'profile' }, { source: 'user' })).releaseId, null);
  // Durable personal-library integration: active Shell survives independent page installation.
  const shellDocument = {schemaVersion:2,id:'library-shell',name:'Library test shell',kind:'shell',navigation:[{id:'workflow',label:'Shell Workflow',feature:'h2b.workflow'},{id:'main',label:'Shell home',pageId:'main'}],pages:[{id:'main',title:'Shell main',layout:{type:'Text',id:'shell-text',text:'Shell content'}}],layout:{navigation:'top',sidebarWidth:280,detailsWidth:360},theme:{mode:'dark',accent:'#2468aa'}};
  const shellDraft = await handle({operation:'create',document:shellDocument,sessionId:'fixture-native'},{source:'user'});
  const libraryShell = await handle({operation:'publish',id:shellDraft.id,revision:1},{source:'user'});
  const nativeProfile = await handle({operation:'profile'},{source:'user'});
  await page.evaluate(async args=>{await fixture.call('apply',args);await fixture.refreshProfile();},{releaseId:libraryShell.id,baseRevision:nativeProfile.revision});
  const pageDocument = {schemaVersion:1,id:'standalone-page',name:'Standalone page',kind:'page',navigation:[{id:'home',label:'Page home',pageId:'home'}],pages:[{id:'home',title:'Standalone home',layout:{type:'Text',id:'page-text',text:'Independent page content'}}],theme:{mode:'light',accent:'#cc0000'}};
  const standaloneDraft = await handle({operation:'create',document:pageDocument,sessionId:'fixture-native'},{source:'user'});
  const standaloneRelease = await handle({operation:'publish',id:standaloneDraft.id,revision:1},{source:'user'});
  await openStudio();
  const manager = page.getByRole('region',{name:'个人页面与导航管理'});
  await manager.getByText('安装已发布页面',{exact:true}).click();
  await manager.locator('div').filter({has:page.getByText('Standalone page · v1',{exact:true})}).filter({has:page.getByRole('button',{name:'安装页面',exact:true})}).last().getByRole('button',{name:'安装页面',exact:true}).click();
  await page.waitForFunction(id=>fixture.state.profile.pageReleaseIds.includes(id),standaloneRelease.id);
  assert.equal((await handle({operation:'profile'},{source:'user'})).releaseId,libraryShell.id);
  await manager.getByRole('button',{name:'打开 Standalone home',exact:true}).click();
  await page.getByText('Independent page content',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-navigation'),'top');
  assert.equal(await page.locator('body').getAttribute('data-ds-dark-theme'),'');
  assert.equal(await page.getByRole('button',{name:'Shell Workflow',exact:true}).count(),1);
  assert.equal(await page.evaluate(()=>fixture.state.pageReleaseId),standaloneRelease.id);
  await workspaceAction('edit-current');
  await page.locator('[data-gui-view=edit]').waitFor();
  await action('exit').click();
  await page.getByText('Independent page content',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>fixture.state.pageReleaseId),standaloneRelease.id,'Editing after opening a page from management returns to that page');
  await openStudio();
  await manager.getByRole('button',{name:'Workflow',exact:true}).click();
  await page.waitForFunction(()=>!fixture.state.open&&window.lastNavigation==='workflows');
  assert.equal(await manager.count(),0);
  assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-surface'),'native');
  await openStudio();
  await manager.getByLabel('启动页目标').selectOption('page:'+standaloneRelease.id+'/home');
  await manager.getByRole('button',{name:'设置启动页',exact:true}).click();
  await page.waitForFunction(id=>fixture.state.profile.home?.releaseId===id,standaloneRelease.id);
  await manager.getByRole('button',{name:'收藏 Standalone page / Standalone home',exact:true}).click();
  await page.waitForFunction(id=>fixture.state.profile.favorites.some(t=>t.releaseId===id),standaloneRelease.id);
  const savedPreference = await handle({operation:'profile'},{source:'user'});
  const conflict = await page.evaluate(async revision=>{try{await fixture.call('configure-profile',{baseRevision:revision-1,home:null});return null;}catch(error){return error.code;}},savedPreference.revision);
  assert.equal(conflict,'GUI_REVISION_CONFLICT');
  assert.deepEqual((await handle({operation:'profile'},{source:'user'})).home,savedPreference.home);
  // Delay the first actual durable Host profile response, then let user navigation win.
  let releaseFirstProfile, markProfileWaiting;
  const profileWaiting=new Promise(resolve=>{markProfileWaiting=resolve;});
  const profileGate=new Promise(resolve=>{releaseFirstProfile=resolve;});
  let heldProfile=false;
  const holdInitialProfile=async route=>{
    if(!heldProfile&&route.request().postDataJSON()?.operation==='profile'){
      heldProfile=true;const response=await route.fetch();markProfileWaiting();await profileGate;await route.fulfill({response});
    }else await route.continue();
  };
  await page.route('**/rpc',holdInitialProfile);
  await page.reload();await profileWaiting;
  await page.evaluate(()=>fixture.navigate('h2b.contacts'));
  assert.equal(await page.evaluate(()=>window.lastNavigation),'h2b.contacts');
  releaseFirstProfile();await page.waitForFunction(()=>fixture.state.ready);
  assert.equal(await page.evaluate(()=>fixture.state.pageId),null);
  assert.equal(await page.evaluate(()=>window.lastNavigation),'h2b.contacts');
  assert.equal(await page.getByText('Independent page content',{exact:true}).count(),0);
  await page.unroute('**/rpc',holdInitialProfile);
  await page.reload();await page.getByText('Independent page content',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-navigation'),'top');
  assert.equal(await page.locator('body').getAttribute('data-ds-dark-theme'),'');
  assert.deepEqual(await page.evaluate(()=>fixture.state.profile.favorites),savedPreference.favorites);
  await page.goto(origin+'/?gui=default');await page.waitForFunction(()=>fixture.state.ready);
  assert.equal(await page.evaluate(()=>fixture.state.pageId),null);assert.equal(await page.getByText('Independent page content',{exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'Shell Workflow',exact:true}).count(),0);
  assert.deepEqual((await handle({operation:'profile'},{source:'user'})).home,savedPreference.home);
  await page.goto(origin);await page.getByText('Independent page content',{exact:true}).waitFor();
  await action('workspace-menu').click();
  await page.getByRole('button',{name:'常用收藏',exact:true}).click();
  await page.getByRole('navigation',{name:'常用收藏',exact:true}).getByRole('button',{name:'Standalone page / Standalone home',exact:true}).click();
  await page.getByText('Independent page content',{exact:true}).waitFor();
  await openStudio();
  await manager.locator('li').filter({hasText:'Standalone page · v1'}).getByRole('button',{name:'移除页面',exact:true}).click();
  const removeInstalledDialog=page.getByRole('alertdialog',{name:'移除已安装页面',exact:true});
  await removeInstalledDialog.waitFor();
  assert.equal(await removeInstalledDialog.getByRole('button',{name:'取消',exact:true}).evaluate(node=>node===document.activeElement),true);
  await removeInstalledDialog.getByRole('button',{name:'取消',exact:true}).click();
  assert.equal((await handle({operation:'profile'},{source:'user'})).pageReleaseIds.includes(standaloneRelease.id),true,'Cancel removal preserves installed page');
  await manager.locator('li').filter({hasText:'Standalone page · v1'}).getByRole('button',{name:'移除页面',exact:true}).click();
  await removeInstalledDialog.getByRole('button',{name:'确认移除',exact:true}).click();
  await page.waitForFunction(id=>!fixture.state.profile.pageReleaseIds.includes(id),standaloneRelease.id);
  await page.waitForFunction(()=>fixture.state.pageReleaseId===null&&fixture.state.pageId===null);
  const removedProfile=await handle({operation:'profile'},{source:'user'});assert.equal(removedProfile.releaseId,libraryShell.id);assert.equal(removedProfile.home,null);assert.equal(removedProfile.favorites.some(t=>t.releaseId===standaloneRelease.id),false);
  await page.getByRole('button',{name:'退出编辑',exact:true}).click();assert.equal(await page.locator('[data-gui-layout]').getAttribute('data-gui-surface'),'native');
  // Management changes to drafts never erase immutable releases, even while installed pages reference them.
  const migrated=await page.evaluate(id=>fixture.call('migrate',{id,baseRevision:1}),standaloneDraft.id);assert.equal(migrated.document.schemaVersion,2);
  const copied=await page.evaluate(id=>fixture.call('clone',{id,baseRevision:2,name:'Disposable copy',sessionId:'fixture-native'}),standaloneDraft.id);
  await page.evaluate(id=>fixture.call('delete',{id,baseRevision:1}),copied.id);
  await page.evaluate(id=>fixture.call('delete',{id,baseRevision:2}),standaloneDraft.id);
  const retained=(await handle({operation:'releases'},{source:'user'})).releases.find(r=>r.id===standaloneRelease.id);assert.deepEqual(retained,standaloneRelease);
  const currentProfile=await handle({operation:'profile'},{source:'user'});
  await page.evaluate(async args=>{await fixture.call('install-page',args);await fixture.refreshProfile();},{releaseId:standaloneRelease.id,baseRevision:currentProfile.revision});
  await page.evaluate(async()=>{await fixture.call('configure-profile',{baseRevision:fixture.state.profile.revision,home:{feature:'h2b.workflow'},favorites:[{feature:'h2b.workflow'}],navigation:{hiddenFeatures:['h2b.contacts'],orderedFeatures:['h2b.workflow']}});await fixture.refreshProfile();});
  await page.evaluate(async()=>{await fixture.call('restore',{baseRevision:fixture.state.profile.revision});await fixture.refreshProfile();});
  const restoredProfile=await handle({operation:'profile'},{source:'user'});
  assert.equal(restoredProfile.releaseId,null);assert.equal(restoredProfile.home,null);
  assert.deepEqual(restoredProfile.navigation,{hiddenFeatures:[],orderedFeatures:[]});
  assert.deepEqual(restoredProfile.pageReleaseIds,[standaloneRelease.id]);assert.deepEqual(restoredProfile.favorites,[{feature:'h2b.workflow'}]);
  // Scope conversion preserves the authored module and styles; publication applies
  // the unsaved shell scope rather than installing the original page draft.
  const scopeDocument = {schemaVersion:2,id:'scope-purple',name:'Purple scope',kind:'page',navigation:[{id:'workspace-feature-0',label:'Purple home',pageId:'purple'}],pages:[{id:'purple',title:'Purple home',layout:{type:'Feature',id:'workflow-purple',instanceId:'stable-purple-workflow',feature:'h2b.workflow',appearance:{radius:12}}}],theme:{preset:'default',accent:'#6B21A8',mode:'dark'}};
  await handle({operation:'create',sessionId:'fixture-native',document:scopeDocument},{source:'user'});
  const scopeProfileBefore = await handle({operation:'profile'},{source:'user'});
  await openStudio('Purple scope · v1');
  assert.equal(await page.locator('[data-gui-scope]').getAttribute('data-gui-scope'),'page');
  assert.equal(await action('apply').textContent(),'发布并安装页面');
  await action('scope-shell').click();
  assert.equal(await page.locator('[data-gui-scope]').getAttribute('data-gui-scope'),'shell');
  assert.equal(await action('apply').textContent(),'发布并启用','Unsaved conversion immediately changes the publish action');
  const scopeConverted=JSON.parse(await page.getByLabel('GUI JSON',{exact:true}).inputValue());
  assert.deepEqual(scopeConverted.pages,scopeDocument.pages);
  assert.deepEqual(scopeConverted.theme,scopeDocument.theme);
  assert.equal(new Set(scopeConverted.navigation.map(item=>item.id)).size,scopeConverted.navigation.length,'Existing navigation IDs survive collisions');
  for(const feature of ['dsh.conversation','h2b.workflow','h2b.kanban','h2b.contacts','h2b.directChat','h2b.operations','h2b.routine']) assert.ok(scopeConverted.navigation.some(item=>item.feature===feature));
  assert.deepEqual(await handle({operation:'profile'},{source:'user'}),scopeProfileBefore,'Draft conversion never applies a release');
  await action('scope-page').click();
  assert.equal(await action('apply').textContent(),'发布并安装页面','Scope can be switched back before publishing');
  await action('scope-shell').click();
  await action('apply').click();
  await page.locator('.gui-use-notice').filter({hasText:/已启用 .+，下次正常打开继续使用。/}).waitFor();
  const scopeApplied=await handle({operation:'profile'},{source:'user'});
  const scopeRelease=(await handle({operation:'releases'},{source:'user'})).releases.find(item=>item.id===scopeApplied.releaseId);
  assert.equal(scopeRelease.document.kind,'shell');
  assert.deepEqual(scopeRelease.document.pages,scopeDocument.pages);
  assert.deepEqual(scopeRelease.document.theme,scopeDocument.theme);
  assert.equal(await action('exit').count(),0,'Publish leaves the editor automatically');
  const deletedSource=(await handle({operation:'list'},{source:'user'})).drafts.find(item=>item.document.id===scopeDocument.id);
  await handle({operation:'delete',id:deletedSource.id,baseRevision:deletedSource.revision},{source:'user'});
  const beforeRecovery=(await handle({operation:'list'},{source:'user'})).drafts.length;
  await workspaceAction('edit-current');
  await page.locator('[data-gui-view=edit]').waitFor();
  const recoveredDrafts=(await handle({operation:'list'},{source:'user'})).drafts;
  assert.equal(recoveredDrafts.length,beforeRecovery+1,'Missing active source is recovered into one draft');
  const recovered=recoveredDrafts.find(item=>item.document.id===scopeDocument.id);
  assert.ok(recovered);
  await action('exit').click();
  await workspaceAction('edit-current');
  await page.locator('[data-gui-view=edit]').waitFor();
  const reusedDrafts=(await handle({operation:'list'},{source:'user'})).drafts;
  assert.equal(reusedDrafts.length,recoveredDrafts.length,'Repeated editing reuses the recovered draft');
  assert.equal(reusedDrafts.find(item=>item.document.id===scopeDocument.id).id,recovered.id);
  await action('exit').click();

  await page.evaluate(async()=>{await fixture.call('restore',{baseRevision:fixture.state.profile.revision});await fixture.refreshProfile();});
  // Publishing a personal page opens that page without replacing the active shell.
  const pagePublishDocument={schemaVersion:2,id:'page-publish-flow',name:'Published personal page',kind:'page',navigation:[{id:'personal',label:'Personal',pageId:'personal'}],pages:[{id:'personal',title:'Personal',layout:{type:'Text',id:'personal-text',text:'Published page opens immediately'}}]};
  await handle({operation:'create',sessionId:'fixture-native',document:pagePublishDocument},{source:'user'});
  const shellBeforePagePublish=(await handle({operation:'profile'},{source:'user'})).releaseId;
  await openStudio('Published personal page · v1');
  await action('apply').click();
  await page.locator('[data-gui-view]').waitFor({state:'hidden'});
  await page.getByText('Published page opens immediately',{exact:true}).waitFor();
  assert.equal((await handle({operation:'profile'},{source:'user'})).releaseId,shellBeforePagePublish);
  // Management-first authoring has one canvas and durable local work drafts.
  const uxDocument={schemaVersion:2,id:'ux-cache',name:'UX cache workspace',kind:'shell',navigation:[{id:'first',label:'First page',pageId:'first'},{id:'second',label:'Second page',pageId:'second'}],pages:[{id:'first',title:'First page',layout:{type:'Text',id:'first-text',text:'First page body'}},{id:'second',title:'Second page',layout:{type:'Text',id:'second-text',text:'Second page body'}}],layout:{navigation:'left'}};
  const uxDraft=await handle({operation:'create',sessionId:'fixture-native',document:uxDocument},{source:'user'});
  await openStudio('UX cache workspace · v1');
  assert.equal(await page.locator('[data-gui-view="edit"] .gui-editor-canvas').count(),1);
  for(const name of ['exit','manage','save','preview','apply','agent']) assert.equal(await action(name).isVisible(),true,'Persistent header action '+name);
  await page.locator('.gui-editor-pages').getByRole('button',{name:'Second page',exact:true}).click();
  await action('preview').click();
  await page.locator('.gui-personal-page').getByText('Second page body',{exact:true}).waitFor();
  assert.equal(await action('back-to-editor').isVisible(),true);assert.equal(await action('end-trial').isVisible(),true);
  await action('back-to-editor').click();
  if(await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).isVisible()) await page.locator('.gui-agent-dock').getByRole('button',{name:'选区属性',exact:true}).click();
  await page.locator('[data-gui-view="edit"]').waitFor();
  await page.locator('[data-node-id=second-text]').click({position:{x:8,y:8}});
  await page.getByRole('button',{name:'查看选区属性',exact:true}).click();
  await page.getByLabel('文字内容',{exact:true}).fill('Cached unsaved second page');
  await page.locator('.gui-editor-more > summary').click();
  await page.getByRole('button',{name:'放弃本地编辑',exact:true}).click();
  const discardDialog=page.getByRole('alertdialog',{name:'放弃整个草稿的未保存修改',exact:true});
  await discardDialog.waitFor();assert.equal(await discardDialog.getByRole('button',{name:'取消',exact:true}).evaluate(node=>node===document.activeElement),true);
  await discardDialog.getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await page.getByLabel('文字内容',{exact:true}).inputValue(),'Cached unsaved second page','Cancel discard preserves local unsaved work');
  await page.locator('.gui-editor-more > summary').click();

  if(!await page.locator('.gui-agent-dock').isVisible()) await action('agent').click();
  await page.getByLabel('Native unsent message').fill('Preserve this request across management and reload');
  assert.equal(await page.locator('.gui-agent-dock').isVisible(),true);
  assert.equal(await action('exit').isEnabled(),true,'Dirty state must not trap the user');
  assert.deepEqual(await page.evaluate(()=>({mounts:window.nativeMounts,unmounts:window.nativeUnmounts})),{mounts:1,unmounts:0});
  await action('agent').click();
  assert.equal(await page.locator('.gui-agent-dock').isVisible(),false);
  await action('manage').click();await page.locator('[data-gui-view="manage"]').waitFor();
  await page.locator('.gui-studio-library').getByRole('button',{name:'UX cache workspace · v1',exact:true}).click();
  assert.equal(await page.getByLabel('文字内容',{exact:true}).inputValue(),'Cached unsaved second page');
  await action('exit').click();await page.reload();
  await openStudio('UX cache workspace · v1');
  assert.equal(await page.getByLabel('文字内容',{exact:true}).inputValue(),'Cached unsaved second page');
  if(!await page.locator('.gui-agent-dock').isVisible()) await action('agent').click();
  assert.equal(await page.getByLabel('Native unsent message').inputValue(),'Preserve this request across management and reload');
  const beforeDirtyAgent=agentRequests.length;
  const dirtyAgentResponse=page.waitForResponse(response=>response.url()===origin+'/agent'&&response.status()===200);
  await page.getByLabel('Native send',{exact:true}).click();
  await page.waitForFunction(()=>window.lastPrompt?.includes('Preserve this request across management and reload'));
  await dirtyAgentResponse;
  assert.equal(agentRequests.length,beforeDirtyAgent+1,'Dirty authoring makes one Agent submission');
  assert.equal(agentRequests.at(-1).revision,2,'Local dirty draft is saved before the Agent reads it');
  assert.equal(agentRequests.at(-1).document.pages[1].layout.text,'Cached unsaved second page');
  assert.deepEqual(await page.evaluate(()=>({mounts:window.nativeMounts,unmounts:window.nativeUnmounts})),{mounts:1,unmounts:0});
  await action('exit').click();
  await page.goto(origin+'/?gui=default');await openStudio();
  await page.locator('.gui-studio').getByText(/安全打开原生界面|默认模式|安全启动/).first().waitFor();
  await action('exit').click();
  // A long document must scroll only its canvas, with the inspector still reachable.
  const longDocument={schemaVersion:2,id:'focus-scroll',name:'Focus scrolling',kind:'shell',navigation:[],pages:[{id:'long',title:'Long page',layout:{id:'root',type:'Stack',children:Array.from({length:24},(_,i)=>({id:'text-'+i,type:'Text',text:'Long content '+i+' '+('Details '.repeat(60))}))}}]};
  await handle({operation:'create',sessionId:'fixture-native',document:longDocument},{source:'user'});
  await openStudio('Focus scrolling · v1');
  await page.setViewportSize({width:1440,height:1000});
  const canvas=page.locator('.gui-editor-canvas'),properties=page.locator('.gui-editor-inspector');
  await canvas.evaluate(node=>{node.scrollTop=node.scrollHeight;});
  await page.locator('[data-node-id=text-23]').click({position:{x:10,y:10}});
  const geometry=await page.evaluate(()=>{const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height};};return {canvas:box('.gui-editor-canvas'),inspector:box('.gui-editor-inspector'),header:box('.gui-agent-dock > header'),scroll:document.querySelector('.gui-editor-canvas').scrollTop};});
  assert.ok(geometry.scroll>500);
  assert.ok(geometry.inspector.top>=geometry.header.bottom && geometry.inspector.bottom<=1000 && geometry.inspector.height>200,JSON.stringify(geometry));
  await properties.getByText('选区外观',{exact:true}).click();
  await properties.evaluate(node=>{node.scrollTop=node.scrollHeight;});
  await page.locator('[data-node-id=text-22]').click({position:{x:10,y:10}});
  assert.equal(await properties.evaluate(node=>node.scrollTop),0,'New focus resets property scroll');
  await page.getByRole('button',{name:'查看选区属性',exact:true}).click();
  await page.getByLabel('文字内容',{exact:true}).fill('Local dirty focus content');
  if(!await page.locator('.gui-agent-dock').isVisible()) await action('agent').click();
  const attached=await page.evaluate(()=>fixture.beforeSend('fixture-native'));
  const attachedSnapshot=JSON.parse(attached.contextText.split('\n')[1]);
  assert.equal(attachedSnapshot.revision,2,'Native send saves dirty design first');
  assert.equal(attachedSnapshot.selection,'long/text-22');
  const savedFocus=(await handle({operation:'list'},{source:'user'})).drafts.find(d=>d.document.id==='focus-scroll');
  assert.equal(savedFocus.document.pages[0].layout.children[22].text,'Local dirty focus content');
  await action('lock-focus').click();
  await page.getByRole('button',{name:'工作空间设置',exact:true}).click();
  await action('locate-focus').click();
  await page.waitForFunction(()=>document.querySelector('[data-node-id="text-22"]')?.classList.contains('is-selected'));
  assert.ok(await canvas.evaluate(node=>node.scrollTop)>500,'Focus locate scrolls to the chosen node');
  if(output) await page.screenshot({path:join(output,'focus-agent-1440.png')});
  await page.setViewportSize({width:390,height:844});
  await page.locator('.gui-agent-dock').getByRole('button',{name:'收起',exact:true}).click();
  await page.getByRole('button',{name:'查看选区属性',exact:true}).click();
  assert.equal(await properties.isVisible(),true);
  await action('exit').click();
  assert.equal((await page.evaluate(()=>fixture.beforeSend('fixture-native'))).contextText,'','Exit removes GUI context');


  await writeFile(join(temp, 'state/gui-studio/store.json'), '{corrupt store retained');
  await page.goto(origin + '/?gui=default');
  await page.getByRole('button', { name: '工作空间', exact: true }).waitFor();
  await page.getByRole('alert').filter({ hasText: 'GUI 定制暂不可用' }).waitFor();
  await page.getByLabel('Native unsent message').fill('native remains usable despite corrupt GUI storage');
  assert.equal(await readFile(join(temp, 'state/gui-studio/store.json'), 'utf8'), '{corrupt store retained');
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  const result = { passed: true, wholeDraftDeleteCancelAndEscape: true, localDiscardCancel: true, installedPageRemovalConfirmation: true, pageScopePreview: true, coverage: ['dark draft canvas, nodes and native navigation diagram maintain readable contrast', 'native navigation preview restores stock rail and sidebar, then switches back to top', 'deleted source recovery is reused on repeated edit-current', 'management-opened page remains return location after edit', 'workspace menu is the only use-mode entry', 'publish exits editor, opens target and edit-current locates source', 'edit-preview-return-exit restores original page', 'personal-page publish opens page without replacing shell', 'page-to-shell conversion preserves styles and module identity, keeps all seven features reachable, and publishes the current unsaved scope', 'management-first authoring with one canvas', 'persistent save/preview/apply/exit actions', 'preview current page and return to editing', 'dirty work draft survives management/exit/reload', 'dirty save precedes one Agent submission', 'native Agent dock toggles without remounting', 'default startup explanation', 'real React include integration', 'actual layout package frame/state transitions', 'durable Host', 'late initial home does not override user navigation', 'profile business navigation closes Studio', 'unsaved visual preview survives business navigation', 'installed page preserves shell navigation/theme', 'home/favorites survive reload and stale revision conflict', 'default query ignores personal home', 'uninstall current page returns native workspace', 'draft clone/migrate/delete preserve releases', 'direct chat vs Agent navigation', 'session-owned Agent update simulation', 'historical inline preview immutable replay and open current revision', 'no duplicate Workflow controller mount', 'late native route cannot resurrect exited page', 'remote profile switch clears old page and native binding', 'precise edit', 'visual property inspector saves authoritative revision', 'trial', 'publish immutable releases', 'history apply', 'import', 'restore', 'reload', 'default query', 'corrupt-store default startup without erasing data', 'page theme and nested appearance project into persistent native/business surfaces without shell pollution', 'fixture native composer preservation', 'tab child state preservation', '390/768/1024/1440 widths'], caveat: 'Native session fixture; separate real DSH smoke required.', errors, external };
  if (output) await writeFile(join(output, 'verification.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
} catch (error) { if (browser && output) { const failurePage=browser.contexts()[0]?.pages()[0]; if(failurePage) await failurePage.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{}); } throw error; } finally { if (browser) await browser.close(); if (server) await new Promise(resolve => server.close(resolve)); await rm(temp, { recursive: true, force: true }); }
