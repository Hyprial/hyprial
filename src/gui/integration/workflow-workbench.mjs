import { mkdir, readFile, writeFile, rename, rm, readdir, lstat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { parseDocument, stringify } from 'yaml';
import lockfile from 'proper-lockfile';

const LIMIT = 64 * 1024;
const TTL = 5 * 60 * 1000;
const digest = value => createHash('sha256').update(value).digest('hex');
const object = value => !!value && typeof value === 'object' && !Array.isArray(value);
function fail(code, message) { throw Object.assign(new Error(message), { code }); }
function text(value, label, max = LIMIT) {
  if (typeof value !== 'string' || !value.trim() || Buffer.byteLength(value) > max || value.includes('\0')) fail('INVALID_ARGUMENT', label + ' is required and must be bounded text');
  return value;
}
export function parseWorkflow(yaml) {
  text(yaml, 'yaml');
  const doc = parseDocument(yaml, { version: '1.1', uniqueKeys: true, strict: true });
  if (doc.errors.length || doc.warnings.length) fail('WORKFLOW_YAML_ERROR', (doc.errors[0] || doc.warnings[0]).message);
  const value = doc.toJS({ maxAliasCount: 0 });
  if (!object(value)) fail('WORKFLOW_YAML_ERROR', 'Workflow must be a mapping');
  return value;
}
export function workflowDiff(before, after, path = '', changes = []) {
  if (JSON.stringify(before) === JSON.stringify(after)) return changes;
  if ((object(before) && object(after)) || (Array.isArray(before) && Array.isArray(after))) {
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
      workflowDiff(before[key], after[key], path + '/' + key.replaceAll('~', '~0').replaceAll('/', '~1'), changes);
    }
  } else changes.push({ path: path || '/', before: before ?? null, after: after ?? null });
  return changes;
}
const FIELDS = {
  list: ['sessionId'], create: ['sessionId', 'name', 'yaml'], get: ['id'],
  propose: ['id', 'baseRevision', 'yaml', 'instructionId'], edit: ['id', 'baseRevision', 'field', 'value'],
  validate: ['id', 'revision'], instruct: ['id', 'baseRevision', 'text', 'mode'], revoke: ['id', 'instructionId'],
  authorize: ['id', 'revision'], run: ['id', 'revision'], inspect: ['id', 'runId', 'target'], 'node-inspect': ['id', 'runId', 'target'],
  cancel: ['id', 'runId', 'confirmed'], clone: ['id', 'revision', 'sessionId', 'name'],
  analyze: ['id', 'runId', 'target'], discussion: ['id', 'runId', 'target'],
  rebind: ['id', 'baseRevision', 'baseBindingVersion', 'sessionId'],
  complete: ['id', 'runId', 'target', 'requestId', 'reasonRef'],
  fail: ['id', 'runId', 'target', 'requestId', 'reasonRef']
};

const legacyRun = run => run.snapshot?.definition?.version === 1 || run.backend === 'legacy-archive';
const definitionNodes = definition => definition?.version === 2
  ? (definition.nodes || []).map(n => ({...n, name:n.id}))
  : definition?.targets;
function normalizeStatus(value) {
  if (!Array.isArray(value?.nodes)) return value;
  return {...value, targets:value.nodes.map(n => ({
    ...n, target:n.nodeId, recipient:n.owner, conversationId:'pac-' + value.graphId
  }))};
}

// One fixed private directory per Host. Definitions are documents, not an execution engine.
export function createWorkflowWorkbench({ root, control, identity, prepare, resolveSession, observeNode, native, now = Date.now }) {
  const liveRequests = new Set();
  const previews = new Map();
  let lease;
  const pathFor = id => {
    if (typeof id !== 'string' || !/^wf-[a-f0-9-]{36}$/.test(id)) fail('INVALID_ARGUMENT', 'Invalid workflow ID');
    return join(root, id + '.json');
  };
  async function read(id) {
    const path = pathFor(id);
    let stat;
    try { stat = await lstat(path); } catch (e) { if (e.code === 'ENOENT') fail('WORKFLOW_NOT_FOUND', 'Workflow not found'); throw e; }
    if (!stat.isFile() || stat.size > 4 * 1024 * 1024) fail('WORKFLOW_STORE_ERROR', 'Invalid workflow document');
    const doc = JSON.parse(await readFile(path, 'utf8'));
    if (doc.id !== id || doc.schema !== 1 || !Array.isArray(doc.revisions)) fail('WORKFLOW_STORE_ERROR', 'Invalid workflow record');
    // H2B resolves the sender before invoking workflow.start: this rejection
    // proves no Run was created. Retain the historical request and error.
    doc.runs = doc.runs.map(run => run.outcome === 'unknown' && !run.runId && run.error?.code === 'SENDER_UNRESOLVED' ? { ...run, outcome: 'rejected' } : run);
    return doc;
  }
  async function save(doc) {
    doc.updatedAt = now();
    const bytes = JSON.stringify(doc);
    if (Buffer.byteLength(bytes) > 4 * 1024 * 1024) fail('WORKFLOW_STORE_FULL', 'Definition history reached 4 MiB; duplicate it to continue');
    if (lease?.error) throw lease.error;
    const path = pathFor(doc.id), tmp = path + '.' + randomUUID() + '.tmp';
    try {
      await writeFile(tmp, bytes, { mode: 0o600, flag: 'wx' });
      if (lease?.error) throw lease.error;
      await rename(tmp, path);
    }
    finally { await rm(tmp, { force: true }); }
  }
  async function locked(fn) {
    await mkdir(root, { recursive: true, mode: 0o700 });
    const currentLease = { error: null };
    let release;
    try {
      release = await lockfile.lock(root, {
        lockfilePath: join(root, '.write-lock'), stale: 30000, update: 5000,
        retries: { retries: 20, minTimeout: 50, maxTimeout: 500, factor: 1.5 },
        onCompromised(error) { currentLease.error = error; }
      });
    } catch (error) {
      if (error.code === 'ELOCKED') fail('WORKFLOW_BUSY', 'Another draft write is in progress; refresh and retry');
      throw error;
    }
    lease = currentLease;
    try { return await fn(); }
    finally { lease = null; await release(); }
  }

  function access(doc, context) {
    if (context.sessionId && doc.sessionId !== context.sessionId) fail('WORKFLOW_SESSION_MISMATCH', 'This workflow belongs to another DSH session');
  }
  function linked(doc) {
    if (!doc.sessionId) fail('WORKFLOW_SESSION_REQUIRED', '请先关联工作会话');
  }
  function sameBinding(before, current, context) {
    access(current, context);
    if (before.sessionId !== current.sessionId || (before.bindingVersion || 0) !== (current.bindingVersion || 0)) {
      fail('WORKFLOW_BINDING_CONFLICT', '关联会话已变化，请刷新后重试');
    }
  }
  function revision(doc, expected = doc.revision) {
    if (!Number.isInteger(expected) || expected !== doc.revision) fail('WORKFLOW_REVISION_CONFLICT', 'Draft changed; read the current revision before editing or running');
    return doc.revisions.at(-1);
  }
  function visible(doc) {
    return { ...doc, readOnly:doc.revisions.at(-1)?.definition?.version === 1, bindingVersion: doc.bindingVersion || 0, grant: doc.grant ? { revision: doc.grant.revision, expiresAt: doc.grant.expiresAt } : null,
      runs: doc.runs.map(run => ({ ...run, outcome: run.outcome === 'submitting' && !liveRequests.has(run.requestId) ? 'unknown' : run.outcome })) };
  }
  async function actor(doc) {
    linked(doc);
    const result = await identity(doc.sessionId);
    const value = result?.actor;
    if (!/^agent:[^:\s]+:[^:\s]+:[^:\s]+$/.test(value || '')) fail('WORKFLOW_IDENTITY_REQUIRED', 'Linked DSH session has no verified H2B identity');
    return value;
  }
  async function add(sessionId, name, yaml) {
    text(sessionId, 'sessionId', 4096); text(name, 'name', 120);
    if (yaml !== undefined) {
      text(yaml, 'yaml');
      try { if (parseWorkflow(yaml).version === 1) fail('WORKFLOW_RETIRED', 'Create a PAC graph with version: 2; legacy workflows are read-only history'); }
      catch (error) { if (error.code === 'WORKFLOW_RETIRED') throw error; }
    }
    const doc = { schema: 1, id: 'wf-' + randomUUID(), sessionId, name, revision: 0, revisions: [], runs: [], grant: null, instruction: null, createdAt: now() };
    if (yaml) { doc.revision = 1; doc.revisions.push(makeRevision(1, yaml, '')); }
    await save(doc); return visible(doc);
  }
  function makeRevision(number, yaml, previous) {
    let definition = null, previousDefinition = null, parseError = '';
    try { definition = parseWorkflow(yaml); } catch (e) { parseError = e.message; }
    if (definition?.version === 1) fail('WORKFLOW_RETIRED', 'Legacy execution syntax is retired; use version: 2 nodes and explicit flags');
    try { if (previous) previousDefinition = parseWorkflow(previous); } catch {}
    return { number, yaml, digest: digest(yaml), createdAt: now(), definition, parseError,
      changes: definition && (!previous || previousDefinition) ? workflowDiff(previousDefinition || {}, definition) : [{ path: '/source', before: previous, after: yaml }], validation: null };
  }
  async function readRun(doc, run, context = {}) {
    const operation = legacyRun(run) ? 'history-status' : 'status';
    const result = native && (!legacyRun(run) || context.sessionId)
      ? {document:{ok:true,...await native(run.sessionId || doc.sessionId, operation, {runId:run.runId})}}
      : await control({operation:'workflow-' + operation,runId:run.runId});
    return {...result,document:normalizeStatus(result.document)};
  }
  return async function handle(input, context = {}) {
    if (!object(input) || !Object.hasOwn(FIELDS, input.operation) || Object.keys(input).some(k => !['operation', ...FIELDS[input.operation]].includes(k))) fail('INVALID_ARGUMENT', 'Unsupported workflow request fields');
    const op = input.operation;
    if (context.sessionId && ['instruct', 'authorize', 'revoke', 'edit', 'cancel', 'analyze', 'rebind'].includes(op)) fail('WORKFLOW_USER_ACTION_REQUIRED', 'Use the user workbench action for this operation');
    if (op === 'list') {
      let files;
      try { files = await readdir(root); } catch (e) { if (e.code === 'ENOENT') return { workflows: [] }; throw e; }
      const docs = await Promise.all(files.filter(x => /^wf-[a-f0-9-]{36}\.json$/.test(x)).map(x => read(x.slice(0, -5))));
      const session = context.sessionId || input.sessionId;
      return { workflows: docs.filter(d => !session || d.sessionId === session).sort((a,b) => b.updatedAt-a.updatedAt).map(d => ({ id:d.id, name:d.name, sessionId:d.sessionId, revision:d.revision, updatedAt:d.updatedAt, runs:d.runs.length, runCount:d.runs.filter(r=>r.runId).length, rejectedCount:d.runs.filter(r=>!r.runId && r.outcome==='rejected').length, unresolvedCount:d.runs.filter(r=>!r.runId && r.outcome!=='rejected').length })) };
    }
    if (op === 'create') return locked(() => add(context.sessionId || input.sessionId, input.name || '新 Workflow', input.yaml));
    const doc = await read(input.id); access(doc, context);
    if (op === 'get') return visible(doc);
    if (doc.revisions.at(-1)?.definition?.version === 1 && !['inspect','node-inspect','analyze','discussion'].includes(op)) fail('WORKFLOW_LEGACY_READ_ONLY','Legacy workflow history is read-only; create a new PAC workflow');
    if (op === 'discussion') return locked(async () => {
      // Re-read under the shared disk lock: concurrent hosts/restarts must never
      // mint two conversations for one immutable execution node.
      const current = await read(doc.id); sameBinding(doc, current, context);
      text(input.runId, 'runId', 128); text(input.target, 'target', 2048);
      const run = current.runs.find(r => r.runId === input.runId && r.outcome === 'started');
      if (!run) fail('WORKFLOW_RUN_NOT_FOUND', 'Run is not associated with this workflow');
      const targets = definitionNodes(run.snapshot?.definition);
      if (!Array.isArray(targets) || !targets.some(t => (typeof t === 'string' ? t : t?.name) === input.target)) {
        fail('INVALID_ARGUMENT', 'Target does not belong to the immutable run snapshot');
      }
      const existing = (current.discussions || []).find(d => d.runId === run.runId && d.target === input.target);
      if (existing) return existing;
      const result = await readRun(current, run, context);
      const status = result?.document;
      if (status?.ok !== true || status.runId !== run.runId || !Array.isArray(status.targets)) fail('INVALID_RESPONSE', 'CLI returned no matching run evidence');
      const matches = status.targets.filter(t => t.target === input.target);
      if (matches.length !== 1) fail('INVALID_ARGUMENT', 'Target does not uniquely belong to this run');
      const executionConversationId = text(matches[0].conversationId, 'executionConversationId', 4096);
      const sessionId = run.sessionId === undefined ? current.sessionId : run.sessionId;
      text(sessionId, 'original work session', 4096);
      const canonical = value => typeof value === 'string' && value.length <= 2048 && /^agent:[^:\s\x00]+:[^:\s\x00]+:[^:\s\x00]+$/.test(value);
      const recorded = status.backend === 'legacy-archive' ? [...new Set((status.deliveries || []).filter(d=>d.targetRef===input.target).map(d=>d.actor))] : [];
      let actor = status.backend === 'pac' ? matches[0].owner : recorded.length===1 ? recorded[0] : input.target;
      if (status.backend === 'pac' && !canonical(actor)) fail('WORKFLOW_DISCUSSION_NOT_AGENT','This node is not owned by an Agent');
      if (!canonical(actor)) {
        if (!observeNode) fail('WORKFLOW_NODE_UNSUPPORTED', 'Node observation is required to resolve an alias');
        if (!run.sender || (await identity(sessionId))?.actor !== run.sender) fail('WORKFLOW_NODE_FORBIDDEN', 'Original workflow sender is unavailable');
        const node = await observeNode(sessionId, { runId: run.runId, target: input.target });
        if (node?.schemaVersion !== 1 || node.runId !== run.runId || node.target !== input.target || node.sender !== run.sender || node.conversationId !== executionConversationId || !Array.isArray(node.deliveries)) fail('INVALID_RESPONSE', 'Node observation does not match this run');
        const actors = [...new Set(node.deliveries.map(d => d?.actor))];
        if (actors.length !== 1 || !canonical(actors[0])) fail('WORKFLOW_DISCUSSION_AMBIGUOUS', 'Cannot resolve a unique recorded Agent for this node');
        actor = actors[0];
      }
      const discussion = { workflowId: current.id, runId: run.runId, target: input.target, revision: run.revision,
        name: typeof run.snapshot.definition.name === 'string' ? run.snapshot.definition.name.slice(0, 120) : current.name,
        actor, conversationId: 'wfd-' + randomUUID(), executionConversationId, sessionId };
      (current.discussions ||= []).push(discussion);
      await save(current);
      return discussion;
    });
    if (op === 'rebind') return locked(async () => {
      const current = await read(doc.id);
      revision(current, input.baseRevision);
      if (!Number.isInteger(input.baseBindingVersion) || input.baseBindingVersion !== (current.bindingVersion || 0)) {
        fail('WORKFLOW_BINDING_CONFLICT', '关联会话已变化，请刷新后重试');
      }
      const next = input.sessionId === null ? null : text(input.sessionId, 'sessionId', 4096);
      if (next === current.sessionId) return visible(current);
      if (current.runs.some(run => ['submitting', 'unknown'].includes(run.outcome))) {
        fail('WORKFLOW_BINDING_BUSY', '存在提交中或结果未知的运行，请先核对运行结果');
      }
      // Run records keep submission outcomes, not live tracking states.
      const runs = current.runs.filter(run => run.outcome !== 'rejected');
      const results = await Promise.all(runs.map(run => {
        if (!run.runId) fail('WORKFLOW_BINDING_BUSY', '运行结果不完整，无法变更关联');
        return readRun(current, run, context);
      }));
      for (let i = 0; i < results.length; i++) {
        const status = results[i]?.document;
        if (status?.ok !== true || status.runId !== runs[i].runId || !['completed', 'cancelled', 'failed'].includes(status.state)) {
          fail('WORKFLOW_BINDING_BUSY', '仍有运行在追踪中或状态无法确认，请结束追踪后重试');
        }
      }
      if (next !== null) {
        const session = typeof resolveSession === 'function' ? await resolveSession(next) : null;
        if (!session || session.id !== next || session.origin === 'subagent') fail('WORKFLOW_SESSION_UNAVAILABLE', '目标工作会话不存在或不是独立工作会话');
      }
      const previous = current.sessionId;
      for (const run of current.runs) if (run.sessionId === undefined) run.sessionId = previous;
      current.sessionId = next;
      current.bindingVersion = (current.bindingVersion || 0) + 1;
      (current.bindingHistory ||= []).push({ version: current.bindingVersion, fromSessionId: previous, toSessionId: next, changedAt: now() });
      current.grant = null;
      current.instruction = null;
      current.authorizationError = null;
      // The latest validation authorizes this session's next run; historical
      // YAML revisions and immutable run snapshots remain intact.
      if (current.revisions.length) current.revisions.at(-1).validation = null;
      previews.delete(current.id);
      await save(current);
      return visible(current);
    });
    if (['instruct', 'authorize', 'run', 'validate', 'analyze'].includes(op)) linked(doc);
    if (op === 'inspect' || op === 'analyze' || op === 'node-inspect') {
      const run = doc.runs.find(r => r.runId === input.runId);
      if (!run) fail('WORKFLOW_RUN_NOT_FOUND', 'Run is not associated with this workflow');
      const result = await readRun(doc, run, context);
      if (result.document?.ok !== true || result.document.runId !== run.runId || !Array.isArray(result.document.targets)) fail('INVALID_RESPONSE', 'CLI returned no matching run evidence');
      if (input.target && !result.document?.targets?.some(t => t.target === input.target)) fail('INVALID_ARGUMENT', 'Target does not belong to this run');
      const evidence = { workflowId:doc.id, runId:run.runId, revision:run.revision, sessionId:run.sessionId === undefined ? doc.sessionId : run.sessionId, sender:run.sender, snapshot:run.snapshot, observedAt:now(), status:result.document, target:input.target || null };
      if (op === 'node-inspect' && !input.target) fail('INVALID_ARGUMENT', 'target is required');
      if (input.target) {
        const node = result.document.targets.find(n => n.target === input.target);
        evidence.node = result.document.backend === 'pac'
          ? {schemaVersion:2,state:'available',runId:run.runId,target:input.target,
             sender:run.sender,conversationId:node.conversationId,tracking:node,
             owner:node.owner,flag:node.flag,requestId:node.requestId,reasonRef:node.reasonRef,
             observedAt:now(),progress:{events:[]},results:{replies:[],evidenceRef:node.reasonRef},
             deliveries:[],identityAvailable:!!node.owner}
          : {state:'archived',message:'Read-only legacy tracking history; no live execution observation',tracking:node};
        if (result.document.backend === 'pac' && observeNode && evidence.sessionId) {
          try {
            const detail=await observeNode(evidence.sessionId,{runId:run.runId,target:input.target});
            if (detail?.schemaVersion!==2 || detail.graphId!==run.runId || detail.target!==input.target || detail.requestId!==node.requestId || detail.owner!==node.owner) fail('WORKFLOW_REQUEST_STALE','Node changed while reading progress; refresh');
            evidence.node={...detail,state:'available'};
          } catch (error) {
            evidence.node={...evidence.node,progressError:error.message};
          }
        }
      }
      return evidence;
    }
    if (op === 'cancel') {
      if (input.confirmed !== true || !doc.runs.some(r => r.runId === input.runId)) fail('WORKFLOW_USER_ACTION_REQUIRED', 'Confirm cancellation of an associated run');
      if (native) return {document:{ok:true,...await native(doc.sessionId,'cancel',{runId:input.runId})}};
      return control({ operation:'workflow-cancel', runId:input.runId, confirmed:true });
    }
    if (op === 'complete' || op === 'fail') {
      if (context.sessionId) fail('WORKFLOW_USER_ACTION_REQUIRED','This workbench control requires an explicit user action');
      const run = doc.runs.find(r => r.runId === input.runId && !legacyRun(r));
      if (!run) fail('WORKFLOW_RUN_NOT_FOUND','No associated PAC graph');
      text(input.target,'node',64); text(input.requestId,'request ID',128); text(input.reasonRef,'evidence reference',512);
      if (!definitionNodes(run.snapshot?.definition)?.some(n => n.name === input.target)) fail('INVALID_ARGUMENT','Node does not belong to this graph');
      return control({operation:'workflow-' + op,runId:input.runId,nodeId:input.target,
                      requestId:input.requestId,reasonRef:input.reasonRef,confirmed:true});
    }
    if (op === 'validate') {
      const rev = revision(doc, input.revision);
      if (!rev) fail('WORKFLOW_DRAFT_REQUIRED', 'Ask the Agent to propose a draft first');
      if (rev.parseError) fail('WORKFLOW_YAML_ERROR', rev.parseError);
      const from = await actor(doc);
      let result, validation;
      try {
        result = await control({ operation:'workflow-plan', yaml:rev.yaml, from });
        if (!result?.previewToken || result.document?.ok !== true || !object(result.document.plan)) fail('INVALID_RESPONSE', 'CLI did not return a valid plan and preview');
        validation = { ok:true, plan:result.document.plan, from, checkedAt:now() };
      } catch (e) { validation = { ok:false, code:e.code || 'WORKFLOW_PLAN_FAILED', message:e.message, checkedAt:now() }; }
      return locked(async () => {
        const current = await read(doc.id); sameBinding(doc, current, context); revision(current, rev.number);
        current.revisions.at(-1).validation = validation;
        await save(current);
        if (validation.ok) previews.set(doc.id, { bindingVersion:current.bindingVersion || 0, revision:rev.number, token:result.previewToken, from, expiresAt:now()+TTL });
        else previews.delete(doc.id);
        return visible(current);
      });
    }
    if (op === 'run') {
      if (!revision(doc, input.revision)) fail('WORKFLOW_DRAFT_REQUIRED', 'No draft to run');
      const from = await actor(doc);
      const preview = previews.get(doc.id);
      const request = await locked(async () => {
        const current = await read(doc.id); sameBinding(doc, current, context); const rev = revision(current, input.revision);
        if (current.runs.some(r => ['submitting','unknown'].includes(r.outcome))) fail('WORKFLOW_OUTCOME_UNKNOWN', 'Previous start outcome is unknown; inspect H2B before issuing a new run');
        if (!current.grant || current.grant.revision !== rev.number || current.grant.actor !== from || current.grant.expiresAt <= now()) fail('WORKFLOW_AUTHORIZATION_REQUIRED', 'This revision needs user run authorization');
        if (!preview || preview.bindingVersion !== (current.bindingVersion || 0) || preview.revision !== rev.number || preview.from !== from || preview.expiresAt <= now()) fail('WORKFLOW_PREVIEW_REQUIRED', 'Validate the current revision again');
        if (typeof prepare !== 'function') fail('WORKFLOW_SENDER_NOT_READY', 'Workflow sender preparation is unavailable');
        const ready = await prepare(current.sessionId);
        if (ready?.actor !== from || ready.sessionRegistered !== true) fail('WORKFLOW_SENDER_NOT_READY', 'Workflow sender changed or was not registered; refresh the linked session');
        const run = { requestId:randomUUID(), sessionId:current.sessionId, revision:rev.number, snapshot:{yaml:rev.yaml,digest:rev.digest,definition:rev.definition}, sender:from, outcome:'submitting', requestedAt:now() };
        current.grant = null; current.runs.push(run); await save(current);
        liveRequests.add(run.requestId); previews.delete(doc.id); return run;
      });
      let result, error;
      try {
        result = native
          ? {document:{ok:true,...await native(doc.sessionId,'start',{yaml:request.snapshot.yaml,operationKey:request.requestId})}}
          : await control({operation:'workflow-run', yaml:request.snapshot.yaml, from, operationKey:request.requestId, previewToken:preview.token});
        if (result.document?.ok !== true || typeof result.document.runId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(result.document.runId)) fail('INVALID_RESPONSE', 'Start returned no authoritative runId');
      } catch (e) { error = e; }
      try {
        return await locked(async () => {
          const current = await read(doc.id), record = current.runs.find(r => r.requestId === request.requestId);
          Object.assign(record, error ? {outcome:error.code === 'SENDER_UNRESOLVED' ? 'rejected' : 'unknown', error:{code:error.code || 'START_UNKNOWN',message:error.message}} : {outcome:'started',runId:result.document.runId,graphId:result.document.graphId || result.document.runId,backend:'pac'});
          await save(current); return visible(current);
        });
      } finally { liveRequests.delete(request.requestId); }
    }
    return locked(async () => {
      const current = await read(doc.id); sameBinding(doc, current, context);
      if (op === 'clone') {
        const source = current.revisions.find(r => r.number === input.revision);
        if (!source) fail('WORKFLOW_DRAFT_REQUIRED', 'Source revision is not available');
        return add(context.sessionId || input.sessionId || current.sessionId, input.name || current.name + ' 副本', source.yaml);
      }
      if (op === 'instruct') {
        revision(current, input.baseRevision); text(input.text,'instruction',12000);
        if (!['draft','run'].includes(input.mode)) fail('INVALID_ARGUMENT','Choose draft or run');
        current.instruction = { id:randomUUID(), text:input.text, mode:input.mode, baseRevision:current.revision, createdAt:now(), expiresAt:now()+30*60*1000 };
        current.grant = null;
      } else if (op === 'revoke') {
        if (current.instruction?.id === input.instructionId) { current.instruction = null; current.grant = null; }
      } else if (op === 'authorize') {
        revision(current, input.revision);
        if (!current.revision) fail('WORKFLOW_DRAFT_REQUIRED','No draft to run');
        current.grant = {revision:current.revision,actor:await actor(current),expiresAt:now()+30*60*1000};
      } else if (op === 'propose' || op === 'edit') {
        const previous = revision(current, input.baseRevision);
        let yaml = input.yaml;
        if (op === 'edit') {
          if (!['name','timeout'].includes(input.field)) fail('INVALID_ARGUMENT','Only name and timeout can be edited inline');
          const def = parseWorkflow(previous?.yaml);
          if (input.field === 'name') def.name = text(input.value,'name',120);
          else {
            const value = Number(input.value);
            if (!Number.isFinite(value) || value <= 0 || value > 86400) fail('INVALID_ARGUMENT','Timeout must be 1–86400 seconds');
            def.defaults = {...def.defaults,timeout:value+'s'};
          }
          yaml = stringify(def, { version: '1.1' });
        }
        text(yaml,'yaml');
        const instruction = current.instruction;
        const allowed = context.sessionId && instruction?.mode === 'run' && instruction.id === input.instructionId && instruction.baseRevision === current.revision && instruction.expiresAt > now();
        current.revision++;
        const rev = makeRevision(current.revision,yaml,previous?.yaml || '');
        current.revisions.push(rev);
        if (typeof rev.definition?.name === 'string') current.name = rev.definition.name.slice(0,120);
        current.grant = null;
        current.authorizationError = null;
        if (allowed) {
          try { current.grant = {revision:current.revision,actor:await actor(current),expiresAt:now()+30*60*1000}; }
          catch (error) { current.authorizationError = error.message; }
        }
        current.instruction = null;
        previews.delete(current.id);
      }
      await save(current); return visible(current);
    });
  };
}
