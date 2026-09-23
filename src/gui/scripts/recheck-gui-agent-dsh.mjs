// Read-only follow-up for an already submitted dedicated verification session.
// No prompt, model selection, session creation or GUI/profile write operations.
import assert from 'node:assert/strict';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join, dirname, resolve } from 'node:path';
import { homedir } from 'node:os';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { guiAgentEndpoint, guiAgentDecompressLog, guiAgentLogTools, guiAgentToolStatus, GUI_AGENT_LABELS } from './verify-gui-agent-dsh.mjs';

const priorPath = resolve(process.env.GUI_AGENT_PRIOR_REPORT || '/tmp/gui-platform-real-agent-final/report.json');
const prior = JSON.parse(await readFile(priorPath, 'utf8'));
const endpoint = guiAgentEndpoint({ ...process.env, GUI_AGENT_DSH_URL: prior.endpoint });
assert.equal(prior.promptsSubmitted, 1);
assert.equal(prior.dedicatedWorkspaceVerified, true);
assert.ok(prior.workspace.startsWith('/tmp/dsh-gui-agent-'));
assert.match(prior.sessionId, /^session-[a-zA-Z0-9-]+$/);
const sessionRoot = join(process.env.GUI_AGENT_DSH_HOME || join(homedir(), '.dsh'), 'sessions');
let log;
for (const folder of await readdir(sessionRoot, { withFileTypes: true })) {
  if (!folder.isDirectory()) continue;
  try { log = guiAgentDecompressLog(await readFile(join(sessionRoot, folder.name, prior.sessionId, 'session.jsonl.zstd'))).toString(); break; } catch (error) { if (error.code !== 'ENOENT') throw error; }
}
assert.ok(log, 'Dedicated session log missing');
const records = log.split('\n').filter(Boolean).map(JSON.parse);
assert.equal(records[0].id, prior.sessionId); assert.equal(records[0].cwd, prior.workspace);
const tools = guiAgentLogTools(log), toolStatus = guiAgentToolStatus(tools);
assert.equal(toolStatus.complete, true); assert.deepEqual(toolStatus.unexpected, []); assert.equal(toolStatus.failed, false);
const contextIds = new Set();
function walk(value, fn) { if (!value || typeof value !== 'object') return; fn(value); Object.values(value).forEach(child => walk(child, fn)); }
walk(records, value => { if (value.type === 'tool-call' && value.name === 'h2b_gui_context') contextIds.add(value.id); if (value.type === 'tool/call' && value.data?.name === 'h2b_gui_context') contextIds.add(value.data.callId); });
let original;
walk(records, value => {
  if (value.type !== 'tool-result' || !contextIds.has(value.toolCallId) || value.isError) return;
  const text = (value.content || []).filter(part => part.type === 'text').map(part => part.text).join('\n');
  try { const data = JSON.parse(text); if (data.id === prior.draftId && data.revision === prior.beforeRevision) original = data.document; } catch {}
});
assert.ok(original, 'Authoritative pre-update document missing from the actual context tool result');
const expected = structuredClone(original); expected.pages[0].layout.text = prior.marker;
const deps = createRequire(process.env.DSH_GUI_BROWSER_DEPS || join(homedir(), '.h2b/apps/gui/source/browser-tests/package.json'));
const browser = await deps('playwright').chromium.launch({ headless: true });
const report = { mode: 'read-only-recheck', promptsSubmitted: 0, originalPromptCount: 1, sessionId: prior.sessionId, draftId: prior.draftId, marker: prior.marker, tools };
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const button = name => page.getByRole('button', { name: GUI_AGENT_LABELS[name] || name, exact: true });
  await page.goto(endpoint.origin + '/?gui=default');
  await page.locator('[data-gui-action="studio"]').waitFor();
  for (let i = 0; i < 12; i++) { for (const name of ['Continue', 'Configure later']) if (await button(name).isVisible().catch(() => false)) await button(name).click(); await page.waitForTimeout(200); }
  async function readGui(operation, id) {
    return page.evaluate(async ({operation,id}) => { const r=await fetch('/plugins/h2b-talk/rpc',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({method:'h2b-gui-studio',args:{operation,...id?{id}:{}}})});const b=await r.json();if(!b.ok)throw new Error('Read-only GUI query rejected');return b.value; },{operation,id});
  }
  const draft=await readGui('get',prior.draftId);
  assert.equal(draft.sessionId,prior.sessionId);assert.equal(draft.revision,prior.beforeRevision+1);assert.deepEqual(draft.document,expected);
  report.exactDocumentMatch=true;report.afterRevision=draft.revision;
  const profile=await readGui('profile');const profileHash=createHash('sha256').update(JSON.stringify(profile)).digest('hex');
  assert.equal(profileHash,prior.profileBeforeHash);report.profileUnchanged=true;
  if(prior.originalDefaultModel){
    const restored=await page.evaluate(async()=>{const r=await fetch('/api/settings.describe',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:crypto.randomUUID(),method:'settings.describe',payload:{}})});const b=await r.json();return b.result?.value?.namespaces?.find(item=>item.ns==='agent-default-model')?.value;});
    assert.deepEqual(restored,prior.originalDefaultModel);report.defaultModelRestored=true;
  }
  await page.locator('[data-gui-action="studio"]').click();
  await page.locator('.gui-studio-library').getByRole('button',{name:draft.document.name+' · v'+draft.revision,exact:true}).click();
  const more=page.locator('details').filter({has:page.getByRole('button',{name:'打开设计会话',exact:true,includeHidden:true})}).first();
  if(await more.count())await more.locator(':scope > summary').click();
  await button('打开设计会话').click();
  const preview=page.locator('.gui-conversation-preview');await preview.waitFor({state:'visible',timeout:20000});
  assert.ok((await preview.textContent()).includes(draft.document.name));
  await preview.locator('summary').click();
  await preview.getByText(prior.marker, { exact: true }).waitFor({ state: 'visible' });
  const screenshot=join(dirname(priorPath),'own-gui-preview-rechecked.png');await preview.screenshot({path:screenshot});
  report.previewVisible=true;report.screenshot=screenshot;report.status='passed';
} finally { await browser.close(); }
const output=join(dirname(priorPath),'read-only-recheck.json');await writeFile(output,JSON.stringify(report,null,2)+'\n',{mode:0o600});console.log(JSON.stringify({...report,evidence:output},null,2));
