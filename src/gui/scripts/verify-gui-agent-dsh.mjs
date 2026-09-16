// One real model request in a NEW temporary workspace/session. Never run as part
// of npm test: this intentionally consumes a small amount of model quota.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, mkdtemp, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import { zstdDecompressSync } from 'node:zlib';
import { createHash, randomUUID } from 'node:crypto';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const GUI_AGENT_TOOLS = Object.freeze(['h2b_gui_context', 'h2b_gui_update', 'h2b_gui_validate', 'h2b_gui_preview']);
export const GUI_AGENT_LABELS = Object.freeze({
  'New session': /^(New session|New Session|新建会话|新会话)$/,
  'Choose workspace': /^(Choose workspace|选择工作区)$/,
  'Add workspace…': /^(Add workspace…|添加工作区…)$/,
  'Edit path': /^(Edit path|编辑路径)$/,
  'Open': /^(Open|打开)$/,
  'Continue': /^(Continue|继续)$/,
  'Configure later': /^(Configure later|稍后配置)$/,
  'Select model': /^(Select model, current |选择模型)/,
});
export function guiAgentEndpoint(env = process.env) {
  const url = new URL(env.GUI_AGENT_DSH_URL || 'http://127.0.0.1:3198');
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || !['3198', '3080'].includes(url.port) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('Use the explicit local DSH port 3198 or 3080 without URL credentials, path or query.');
  if (url.port === '3080' && env.GUI_AGENT_LOCAL_AUTHORIZED !== '1') throw new Error('Port 3080 requires GUI_AGENT_LOCAL_AUTHORIZED=1 for the already-authorized local test.');
  return url;
}
export function guiAgentInstruction(id, marker) {
  return `这是一次专用测试会话中的 GUI 工具链验证。唯一目标草稿 ID=${id}。只允许依次使用 h2b_gui_context、h2b_gui_update、h2b_gui_validate、h2b_gui_preview。先读取该 ID 的权威文档和 revision；只将 home 页 marker-node 这个 Text 节点的 text 改为 ${marker}，其他内容完全保留；用 baseRevision 更新一次；验证更新后的准确 revision；预览同一 revision，然后停止。不要创建其他草稿，不发布、不安装、不应用、不改 profile；不要运行 workflow、shell、文件工具、网络工具或委派子agent；不要调用任何消息发送工具，不要联系任何人。若所需工具不可用，直接报告并停止，不尝试替代工具。`;
}
export function guiAgentToolStatus(rows) {
  const unexpected = [...new Set(rows.map(row => row.name).filter(name => !GUI_AGENT_TOOLS.includes(name)))];
  return { unexpected, complete: GUI_AGENT_TOOLS.every(name => rows.some(row => row.name === name && row.state === 'ok')), failed: rows.some(row => GUI_AGENT_TOOLS.includes(row.name) && ['error', 'stopped'].includes(row.state)) };
}
// Summarize only the dedicated session's persisted tool events. Raw content,
// tool arguments, model reasoning and credentials never leave this function.
export function guiAgentSessionDirectoryName(id) {
  assert.match(id, /^[a-zA-Z0-9_-]+$/);
  return id.startsWith('session-') ? id : 'session-' + id;
}
export function guiAgentLogTools(text) {
  const calls = new Map(), results = new Map();
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    const type=value.type || value.kind, data=value.data || {};
    if (type === 'tool/call' && typeof data.callId === 'string' && typeof data.name === 'string') calls.set(data.callId, data.name);
    if (type === 'tool-call' && typeof (value.id || value.callId) === 'string' && typeof value.name === 'string') calls.set(value.id || value.callId, value.name);
    if (type === 'tool/result' && typeof data.callId === 'string') results.set(data.callId, data.isError === true || Boolean(data.error) ? 'error' : 'ok');
    if (type === 'tool-result' && typeof (value.toolCallId || value.callId) === 'string') results.set(value.toolCallId || value.callId, value.isError === true ? 'error' : 'ok');
    Object.values(value).forEach(visit);
  }
  for (const line of text.split('\n')) { try { if (line) visit(JSON.parse(line)); } catch {} }
  return [...calls].map(([id,name])=>({name,state:results.get(id)||'running'}));
}
export function guiAgentDecompressLog(bytes, limit = 32 * 1024 * 1024) {
  const chunks = []; let offset = 0, total = 0;
  while (offset < bytes.length) {
    const frame = zstdDecompressSync(bytes.subarray(offset), { info: true, maxOutputLength: limit - total });
    const consumed = frame.engine.bytesWritten;
    assert.ok(Number.isSafeInteger(consumed) && consumed > 0 && consumed <= bytes.length - offset, 'Invalid Zstandard frame boundary');
    chunks.push(frame.buffer); total += frame.buffer.length; offset += consumed;
    assert.ok(total <= limit, 'Dedicated session decompression size bound exceeded');
  }
  return Buffer.concat(chunks, total);
}
async function ownSessionText(home, id) {
  const sessionDirectory = guiAgentSessionDirectoryName(id);
  for (const workspace of await readdir(join(home,'sessions'),{withFileTypes:true}).catch(()=>[])) {
    if (!workspace.isDirectory()) continue;
    for (const suffix of ['session.jsonl.zstd','session.jsonl']) {
      const file=join(home,'sessions',workspace.name,sessionDirectory,suffix);
      try {
        if ((await stat(file)).size > 8*1024*1024) throw new Error('Dedicated session log exceeded verification size bound');
        const bytes=await readFile(file), text=(suffix.endsWith('.zstd')?guiAgentDecompressLog(bytes):bytes).toString('utf8');
        return text;
      } catch (error) { if (error.message === 'Dedicated session log exceeded verification size bound') throw error; }
    }
  }
  return '';
}
async function ownSessionTools(home, id) { return guiAgentLogTools(await ownSessionText(home, id)); }
async function knownSessionIds(home) {
  const ids = new Set();
  for (const workspace of await readdir(join(home, 'sessions'), { withFileTypes: true }).catch(() => [])) {
    if (!workspace.isDirectory()) continue;
    for (const session of await readdir(join(home, 'sessions', workspace.name), { withFileTypes: true })) if (session.isDirectory() && session.name.startsWith('session-')) { ids.add(session.name); ids.add(session.name.slice(8)); }
  }
  return ids;
}
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');

export async function verifyGuiAgent(env = process.env) {
  const endpoint = guiAgentEndpoint(env);
  const dshHome = env.GUI_AGENT_DSH_HOME || (endpoint.port === '3080' ? join(homedir(), '.dsh') : null);
  if (!dshHome) throw new Error('Set GUI_AGENT_DSH_HOME to the isolated server home, so fresh session identity can be checked before sending.');
  const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  let deps;
  for (const base of [env.DSH_GUI_BROWSER_DEPS, join(repo, 'dashboard/package.json'), join(homedir(), '.h2b/apps/gui/source/dashboard/package.json')].filter(Boolean)) {
    try { const candidate = createRequire(resolve(base)); candidate.resolve('playwright'); deps = candidate; break; } catch {}
  }
  if (!deps) throw new Error('Install dashboard Playwright dependencies before this explicit model test.');
  const workspace = await mkdtemp(join(tmpdir(), 'dsh-gui-agent-'));
  await writeFile(join(workspace, 'AGENTS.md'), 'This is an isolated GUI verification workspace. Only use h2b_gui_context, h2b_gui_update, h2b_gui_validate, h2b_gui_preview for the explicitly named draft. Do not run commands, modify files, execute workflows, use network or messaging tools, publish, apply, or delegate. Stop after the requested preview.\n', { mode: 0o600 });
  const output = resolve(env.GUI_AGENT_ARTIFACTS || join(workspace, 'evidence'));
  await mkdir(output, { recursive: true, mode: 0o700 });
  const marker = 'GUI_AGENT_VERIFIED_' + randomUUID();
  const report = { endpoint: endpoint.origin, workspace, marker, status: 'preparing', promptsSubmitted: 0, tools: [], profileUnchanged: null, timeoutMs: 180000 };
  const beforeSessions = await knownSessionIds(resolve(dshHome));
  const { chromium } = deps('playwright');
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(12000);
  const button = name => page.getByRole('button', { name: GUI_AGENT_LABELS[name] || name, exact: true });
  let profileBefore, ownSession = false, submitted = false, finished = false, deadlineTimer, originalModelDefault, temporaryModelDefault, modelRestoreNeeded = false;
  async function rpc(args) {
    const result = await page.evaluate(async args => {
      const response = await fetch('/plugins/h2b-talk/rpc', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ method: 'h2b-gui-studio', args }), signal: AbortSignal.timeout(10000) });
      const body = await response.json(); return { ok: body.ok === true, value: body.value, code: body.error?.code || 'GUI_RPC_ERROR' };
    }, args);
    if (!result.ok) throw new Error('GUI RPC rejected: ' + String(result.code).replace(/[^A-Z_0-9]/g, ''));
    return result.value;
  }
  async function nativeRpc(method, payload) {
    const value = await page.evaluate(async ({ method, payload }) => {
      const response = await fetch('/api/' + method, { method: 'POST', headers: { 'content-type': 'application/json' }, signal: AbortSignal.timeout(10000),
        body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method, payload }) });
      const body = await response.json();
      if (!body.result?.ok) return { ok: false, code: body.result?.error?.code || 'NATIVE_RPC_ERROR' };
      if (method === 'settings.describe') {
        const ns = body.result.value.namespaces.find(item => item.ns === 'agent-default-model');
        return { ok: true, value: ns ? { value: ns.value, user: ns.user || {}, revision: ns.revision } : null };
      }
      return { ok: true, value: body.result.value };
    }, { method, payload });
    if (!value.ok) throw new Error('Native model selection RPC rejected: ' + value.code);
    return value.value;
  }
  async function prepareModel(sessionId) {
    const catalog = await nativeRpc('session.models', { sessionId });
    report.expectedProviderReady = /deepseek/i.test(catalog.current?.provider || '');
    report.provider = catalog.current?.provider || 'unavailable';
    if (report.expectedProviderReady || env.GUI_AGENT_ALLOW_TEMP_MODEL !== '1') return;
    originalModelDefault = await nativeRpc('settings.describe', {});
    assert.ok(originalModelDefault && typeof originalModelDefault.value?.provider === 'string', 'Default model selection cannot be safely restored');
    for (const section of [originalModelDefault.value, originalModelDefault.user]) assert.ok(Object.keys(section).every(key => ['provider','model','reasoningEffort'].includes(key)), 'Unexpected fields in default-model section');
    const group = catalog.groups.find(item => item.id === 'deepseek-official');
    const model = group?.models.find(item => /flash/i.test(item.id + ' ' + item.name)) || group?.models[0];
    assert.ok(model, 'BLOCKED_PROVIDER: configured DeepSeek model is unavailable');
    report.originalDefaultModel = originalModelDefault.value;
    // Mark before the RPC: even a transport failure may follow a committed
    // selection. Finally reads the current namespace before restoring it.
    modelRestoreNeeded = true;
    temporaryModelDefault = { provider: group.id, model: model.id };
    const selected = await nativeRpc('session.selectModel', { sessionId, ...temporaryModelDefault });
    temporaryModelDefault = selected.selected;
    report.temporaryModel = temporaryModelDefault;
    const after = await nativeRpc('settings.describe', {});
    assert.equal(JSON.stringify(after.value), JSON.stringify(temporaryModelDefault), 'Temporary model default differs from selected model');
    report.expectedProviderReady = true; report.provider = temporaryModelDefault.provider;
  }
  async function restoreModelDefault() {
    if (!modelRestoreNeeded) return;
    const current = await nativeRpc('settings.describe', {});
    if (JSON.stringify(current.value) !== JSON.stringify(originalModelDefault.value)) {
      assert.deepEqual(current.value, temporaryModelDefault, 'Concurrent default model change: refusing to overwrite another selection or reasoning effort');
      await nativeRpc('settings.replace', { ns: 'agent-default-model', section: originalModelDefault.user, expectedRevision: current.revision });
    }
    const restored = await nativeRpc('settings.describe', {});
    assert.equal(JSON.stringify(restored.value), JSON.stringify(originalModelDefault.value), 'Original model default did not restore');
    assert.equal(JSON.stringify(restored.user), JSON.stringify(originalModelDefault.user), 'Original model override did not restore');
    report.defaultModelRestored = true;
  }
  async function stopOwnGeneration() {
    if (!ownSession || !submitted || finished) return;
    const stop = page.getByRole('button', { name: /^(Stop generating|停止生成)$/ });
    if (await stop.isVisible().catch(() => false)) { await stop.click(); report.stopRequested = true; }
    else report.stopRequested = false;
  }
  try {
    // The query bypasses personal home navigation in this fresh browser only.
    // No restore/apply/configure-profile calls are made by this script.
    await page.goto(endpoint.origin + '/?gui=default');
    await page.locator('[data-gui-action="studio"]').waitFor();
    for (let i = 0; i < 12; i++) {
      for (const name of ['Continue', 'Configure later']) if (await button(name).isVisible().catch(() => false)) await button(name).click();
      await page.waitForTimeout(200);
    }
    profileBefore = await rpc({ operation: 'profile' }); report.profileBeforeHash = hash(profileBefore);
    const beforeDrafts = new Set((await rpc({ operation: 'list' })).drafts.map(draft => draft.id));
    // New Session alone can reuse an old blank session. A unique newly created
    // workspace is mandatory before allowing a model prompt.
    report.stage = 'new-session';
    await button('New session').first().click();
    report.stage = 'choose-workspace';
    await button('Choose workspace').click();
    report.stage = 'add-workspace';
    await page.getByText(GUI_AGENT_LABELS['Add workspace…'], { exact: true }).click();
    report.stage = 'edit-directory';
    await button('Edit path').click();
    await page.getByRole('textbox', { name: GUI_AGENT_LABELS['Edit path'], exact: true }).fill(workspace);
    await page.getByRole('textbox', { name: GUI_AGENT_LABELS['Edit path'], exact: true }).press('Enter');
    report.stage = 'open-directory';
    await button('Open').click();
    await page.waitForFunction(label => [...document.querySelectorAll('button[aria-label="Choose workspace"], button[aria-label="选择工作区"]')].some(node => node.textContent.includes(label)), workspace.split('/').at(-1));
    report.stage = 'open-studio';
    await page.locator('[data-gui-action="studio"]').click();
    report.stage = 'new-draft';
    await button('新建界面').click();
    await page.locator('[data-gui-action=save]').waitFor();
    const drafts = (await rpc({ operation: 'list' })).drafts.filter(draft => !beforeDrafts.has(draft.id));
    assert.equal(drafts.length, 1, 'Expected exactly one script-created GUI draft; concurrent changes require a fresh verification run.');
    let draft = await rpc({ operation: 'get', id: drafts[0].id });
    assert.ok(typeof draft.sessionId === 'string' && draft.sessionId && !beforeSessions.has(draft.sessionId), 'Refusing to prompt an existing session.');
    const sessionHeader = (await ownSessionText(resolve(dshHome), draft.sessionId)).split('\n').filter(Boolean).map(line => JSON.parse(line)).find(row => row.type === 'session');
    assert.equal(sessionHeader?.cwd, workspace, 'Refusing to prompt a session outside the newly created dedicated workspace.');
    assert.equal(guiAgentSessionDirectoryName(sessionHeader.id), guiAgentSessionDirectoryName(draft.sessionId));
    ownSession = true; report.sessionId = draft.sessionId; report.draftId = draft.id; report.dedicatedWorkspaceVerified = true;
    const document = { ...draft.document, name: 'GUI Agent verification ' + marker.slice(-8), kind: 'page', navigation: [{ id: 'home', label: 'Verification', pageId: 'home' }], pages: [{ id: 'home', title: 'Verification', layout: { type: 'Text', id: 'marker-node', text: 'Awaiting real model tool update' } }] };
    draft = await rpc({ operation: 'update', id: draft.id, baseRevision: draft.revision, document });
    report.beforeRevision = draft.revision;
    const more = page.locator('details').filter({has:page.getByRole('button',{name:'刷新草稿',exact:true,includeHidden:true})}).first();
    if (await more.count()) await more.locator(':scope > summary').click();
    await button('刷新草稿').click();
    if (await more.count() && await more.evaluate(element=>element.open)) await more.locator(':scope > summary').click();
    await page.locator('[data-gui-action=agent]').click();
    await page.getByRole('textbox', { name: 'GUI 设计要求', exact: true }).fill(guiAgentInstruction(draft.id, marker));
    report.stage = 'prepare-model';
    await prepareModel(draft.sessionId);
    if (env.GUI_AGENT_PREPARE_ONLY === '1') { report.status = 'prepared'; return report; }
    assert.ok(report.expectedProviderReady, 'BLOCKED_PROVIDER: dedicated session does not use DeepSeek; no prompt sent');
    // This is the only submission. There is no retry or second prompt path.
    submitted = true; report.promptsSubmitted = 1; report.submittedAt = new Date().toISOString();
    const deadline = Date.now() + 180000;
    deadlineTimer = setTimeout(() => { report.deadlineReached = true; void stopOwnGeneration().catch(() => { report.stopRequested = false; }); }, 180000);
    await button('交给 Agent 设计').click();
    await page.getByText('已交给原生 Agent 会话。可在右侧继续讨论，设计结果会同步到画布。', { exact: true }).waitFor();
    await button('退出定制').click();
    while (Date.now() < deadline) {
      report.tools = await ownSessionTools(resolve(dshHome), draft.sessionId);
      // Preview replaces the generic tool row, so it has no data-tool attr.
      // The persisted own-session result proves which tool actually executed;
      // the DOM separately proves the custom preview is visible.
      report.visibleToolRows = await page.locator('[data-tool]').evaluateAll(nodes => nodes.filter(node => node.getClientRects().length > 0).map(node => ({ name: node.getAttribute('data-tool'), state: node.getAttribute('data-state') })).filter(row => row.name));
      const status = guiAgentToolStatus(report.tools);
      if (status.unexpected.length) throw new Error('UNEXPECTED_TOOL: model attempted a tool outside the requested GUI-only verification.');
      if (status.failed) throw new Error('BLOCKED_PROVIDER_OR_TOOL: a required GUI tool failed or was stopped.');
      const latest = await rpc({ operation: 'get', id: draft.id });
      const correct = latest.revision === draft.revision + 1 && latest.document.pages[0]?.layout?.text === marker;
      if (latest.revision > draft.revision + 1) throw new Error('UNEXPECTED_REVISION: model changed the draft more than once.');
      if (correct && status.complete) {
        const expected = structuredClone(document); expected.pages[0].layout.text = marker;
        assert.deepEqual(latest.document, expected, 'Model modified fields outside the requested Text node.');
        report.afterRevision = latest.revision;
        const preview = page.locator('.gui-conversation-preview');
        await preview.waitFor({ state: 'visible' });
        await preview.locator('summary').click();
        await preview.getByText(marker, { exact: true }).waitFor({ state: 'visible' });
        await preview.screenshot({ path: join(output, 'own-gui-preview.png') });
        report.previewVisible = true;
        // Do not let an otherwise-complete test leave additional model work
        // running: stop the remaining final prose after all four tools settle.
        await stopOwnGeneration(); finished = true; report.status = 'passed'; break;
      }
      const failedProvider = await page.locator('[role="alert"]').evaluateAll(nodes => nodes.some(node => /api.?key|authentication|unauthorized|quota|rate.?limit|provider|模型|鉴权/i.test(node.textContent || '')));
      if (failedProvider) throw new Error('BLOCKED_PROVIDER: existing provider rejected the dedicated test; no automatic retry.');
      await page.waitForTimeout(1200);
    }
    if (!finished) throw new Error('BLOCKED_PROVIDER_OR_MODEL_TIMEOUT: no complete GUI tool flow within 180 seconds; no automatic retry.');
  } catch (error) {
    report.status = 'blocked';
    // Do not emit raw provider responses, page bodies, request payloads or
    // other session contents. Full test-owned IDs remain in this report.
    report.reason = /BLOCKED_PROVIDER/.test(error.message) ? 'Provider/model/tool execution unavailable or timed out' : /UNEXPECTED_TOOL/.test(error.message) ? 'Unexpected non-GUI tool attempted' : /UNEXPECTED_REVISION/.test(error.message) ? 'Draft updated more than once' : 'UI contract or verification assertion failed before completion';
    report.failedCheck = String(error.message).split('\n')[0].replace(/[^a-zA-Z0-9 _.:()=-]/g, '').slice(0, 160);
    await stopOwnGeneration().catch(() => { report.stopRequested = false; });
  } finally {
    clearTimeout(deadlineTimer);
    try { await restoreModelDefault(); } catch { report.defaultModelRestored = false; report.status = 'failed'; report.reason = 'Temporary model default could not be restored; original identifiers are recorded for recovery'; }
    if (profileBefore) {
      try { const after = await rpc({ operation: 'profile' }); report.profileAfterHash = hash(after); report.profileUnchanged = JSON.stringify(profileBefore) === JSON.stringify(after); if (!report.profileUnchanged) { report.status = 'failed'; report.reason = 'GUI profile changed during verification; investigate concurrent changes or model behavior'; } }
      catch { report.profileUnchanged = null; report.status = 'blocked'; report.reason = 'Unable to verify final GUI profile'; }
    }
    await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
    await browser.close();
    console.log(JSON.stringify({ ...report, evidence: join(output, 'report.json') }, null, 2));
  }
  return report;
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  verifyGuiAgent().then(report => { if (!['passed', 'prepared'].includes(report.status)) process.exitCode = 2; }).catch(error => { console.error(error.message); process.exitCode = 2; });
}
