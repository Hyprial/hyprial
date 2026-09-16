// Actual React WorkflowWorkbench instances, isolated browser fixture. No Host or business traffic.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdtemp, mkdir, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let deps;
for (const base of [process.env.DSH_GUI_BROWSER_DEPS, join(repo, 'dashboard/package.json'), join(homedir(), '.h2b/apps/gui/source/dashboard/package.json')].filter(Boolean)) {
  try { const candidate = createRequire(resolve(base)); for (const name of ['react', 'react-dom/client', 'rolldown', 'playwright']) candidate.resolve(name); deps = candidate; break; } catch {}
}
if (!deps) throw new Error('Install dashboard browser dependencies or set DSH_GUI_BROWSER_DEPS');
const { build } = await import(pathToFileURL(deps.resolve('rolldown')));
const { chromium } = deps('playwright');
const temp = await mkdtemp(join(tmpdir(), 'workflow-instance-browser-'));
const output = process.env.WORKFLOW_INSTANCE_ARTIFACTS ? resolve(process.env.WORKFLOW_INSTANCE_ARTIFACTS) : temp;
await mkdir(output, { recursive: true });
const source = await readFile(join(repo, 'client/workflow-workbench.inc.js'), 'utf8');
const css = '.wb-layout{display:grid;grid-template-columns:180px 1fr;gap:12px}.wb-library{display:flex;flex-direction:column;gap:8px}.wb-workbench{padding:12px;border:1px solid #aaa;margin:10px}.h2bcontrol-field{display:flex;flex-direction:column}.h2bcontrol-textarea{min-height:100px;width:100%;box-sizing:border-box}';
await writeFile(join(temp, 'entry.mjs'), `import React from ${JSON.stringify(deps.resolve('react'))};
import {createRoot} from ${JSON.stringify(deps.resolve('react-dom/client'))};
const docs=Object.fromEntries(['a','b'].map(key=>['wf-'+key,{id:'wf-'+key,name:key==='a'?'Alpha':'Beta',sessionId:'session-'+key,revision:1,bindingVersion:0,revisions:[{number:1,yaml:'name: '+key+'\\n',changes:[],createdAt:1}],runs:[{requestId:'request-'+key,runId:'run-'+key,revision:1,outcome:'started'}]}]));
window.workflowCalls=[];
const host={async call(method,input){window.workflowCalls.push(input);if(method!=='h2b-workflow-workbench')throw Error('Wrong transport');if(input.operation==='list')return{workflows:Object.values(docs).map(d=>({id:d.id,name:d.name,revision:d.revision,runs:d.runs.length}))};if(input.operation==='get')return structuredClone(docs[input.id]);if(input.operation==='inspect')return{revision:1,status:{state:'completed',targets:[]},snapshot:{yaml:'name: saved'},sessionId:docs[input.id].sessionId};throw Error('Unexpected business write '+input.operation);}};
const slots={inject(){}};const sessions={};const listedSessions=()=>[];const persistedHumanChats={};const isSystemSession=()=>false;const currentAppSurface=()=> 'messages';const snapshotOf=value=>value;const workspaces={};const listedWorkspaces=()=>[];
${source}
function App(){const[hidden,setHidden]=React.useState(false);const[showB,setShowB]=React.useState(true);const[viewA,setViewA]=React.useState('default');window.workflowFixture={setHidden,setShowB,setViewA};return React.createElement(React.Fragment,null,React.createElement(WorkflowWorkbench,{instanceId:'auth:personal:instance-a',context:{workflowId:'wf-a'},visible:!hidden,view:viewA}),showB?React.createElement(WorkflowWorkbench,{instanceId:'auth:personal:instance-b',context:{workflowId:'wf-b'},visible:!hidden}):null);}
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
try {
  await page.goto('http://127.0.0.1:' + server.address().port);
  const a = page.locator('[data-workflow-instance="auth:personal:instance-a"]');
  const b = page.locator('[data-workflow-instance="auth:personal:instance-b"]');
  async function plan(panel) {
    const button = panel.getByRole('button', { name: '方案', exact: true });
    await button.waitFor(); if (await button.isEnabled()) await button.click();
    if (!await panel.getByRole('textbox', { name: 'Workflow YAML' }).isVisible()) await panel.getByText('YAML · 查看、导入与精确编辑', { exact: true }).click();
  }
  await plan(a); await plan(b);
  const yamlA = a.getByRole('textbox', { name: 'Workflow YAML' });
  const yamlB = b.getByRole('textbox', { name: 'Workflow YAML' });
  await yamlA.fill('name: unsaved alpha from A'); await yamlB.fill('name: unsaved beta from B');
  await b.locator('.wb-library button').filter({ hasText: 'Alpha' }).click(); await plan(b);
  await yamlB.fill('name: same workflow separate B draft');
  assert.equal(await yamlA.inputValue(), 'name: unsaved alpha from A');
  await a.getByRole('button', { name: '刷新方案', exact: true }).click();
  assert.equal(await yamlA.inputValue(), 'name: unsaved alpha from A');
  await a.getByRole('button', { name: '运行（1）', exact: true }).click();
  assert.ok(await a.getByText('关联运行', { exact: true }).isVisible());
  assert.ok(await yamlB.isVisible());
  await plan(a);
  await page.evaluate(() => window.workflowFixture.setHidden(true));
  await a.waitFor({ state: 'hidden' });
  await page.waitForTimeout(100);
  const readsBefore = await page.evaluate(() => window.workflowCalls.length);
  await page.waitForTimeout(5200);
  assert.equal(await page.evaluate(() => window.workflowCalls.length), readsBefore, 'Hidden instances must pause polling');
  await page.evaluate(() => window.workflowFixture.setHidden(false)); await a.waitFor({ state: 'visible' });
  assert.equal(await yamlA.inputValue(), 'name: unsaved alpha from A');
  assert.equal(await yamlB.inputValue(), 'name: same workflow separate B draft');
  await page.evaluate(() => window.workflowFixture.setShowB(false)); await b.waitFor({ state: 'detached' });
  await page.evaluate(() => window.workflowFixture.setShowB(true)); await plan(b);
  assert.equal(await yamlB.inputValue(), 'name: same workflow separate B draft');
  await b.locator('.wb-library button').filter({ hasText: 'Beta' }).click(); await plan(b);
  assert.equal(await yamlB.inputValue(), 'name: unsaved beta from B');
  assert.equal(await yamlA.inputValue(), 'name: unsaved alpha from A');
  await page.evaluate(() => window.workflowFixture.setViewA('list'));
  await a.locator('.wb-library').waitFor({ state: 'visible' }); await a.locator('.wb-detail').waitFor({ state: 'hidden' });
  await page.evaluate(() => window.workflowFixture.setViewA('detail'));
  await a.locator('.wb-library').waitFor({ state: 'hidden' }); await a.locator('.wb-detail').waitFor({ state: 'visible' });
  assert.equal(await yamlA.inputValue(), 'name: unsaved alpha from A');
  await page.evaluate(() => window.workflowFixture.setViewA('runs'));
  await a.getByText('关联运行', { exact: true }).waitFor({ state: 'visible' });
  await page.evaluate(() => window.workflowFixture.setViewA('default')); await plan(a);
  assert.equal(await yamlA.inputValue(), 'name: unsaved alpha from A');
  assert.deepEqual(errors, []);
  const operations = await page.evaluate(() => [...new Set(window.workflowCalls.map(call => call.operation))]);
  assert.ok(operations.every(operation => ['list', 'get', 'inspect'].includes(operation)));
  await page.screenshot({ path: join(output, 'two-workflows.png'), fullPage: true });
  const report = { independentYaml: true, independentSelection: true, independentRunTab: true, focusedViews: true, hiddenPollingPaused: true, remountRestoredDraft: true, operations, pageErrors: errors };
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ output, ...report }));
} catch (error) {
  await page.screenshot({ path: join(output, 'failure.png'), fullPage: true }).catch(() => {});
  throw error;
} finally {
  await browser.close(); await new Promise(resolve => server.close(resolve));
  if (output !== temp) await rm(temp, { recursive: true, force: true });
}
