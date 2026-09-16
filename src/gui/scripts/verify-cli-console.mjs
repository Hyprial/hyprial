#!/usr/bin/env node
// Opt-in, no model / no external services. Never adopts the ambient H2B home.
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, access } from 'node:fs/promises';
import { constants } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, basename, isAbsolute } from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { Readable } from 'node:stream';

const workflowOnly = process.argv.includes('--workflow-only');
const binary = process.env.H2B_BIN;
assert.ok(binary && isAbsolute(binary), 'Set H2B_BIN to the exact installed executable under test');
assert.equal(basename(binary), 'h2b', 'H2B_BIN must be named h2b so Host PATH queries use this exact executable');
assert.equal(process.platform, 'linux', 'This isolated process-group harness currently requires Linux');
await access(binary, constants.X_OK);
const root = await mkdtemp(join(tmpdir(), 'dsh-cli-e2e-'));
const unsafeEnvironment = /^(H2B_|HARNESS_|DSH_|CODEX_|CLAUDE|PI_|TASKRC$|TASKDATA$|BASH_ENV$|ENV$)/;
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  !unsafeEnvironment.test(key)));
Object.assign(env, {
  H2B_HOME: join(root, 'home'), HARNESS_STATE_DIR: join(root, 'state'),
  HARNESS_SOCKET_PATH: join(root, 'state', 'daemon.sock'),
  H2B_OWNER: 'console-e2e', H2B_NODE_ID: 'console-e2e-node',
  H2B_PEER_DISCOVERY: '0', H2B_ZENOH_LISTEN: 'tcp/127.0.0.1:0', H2B_ZENOH_CONNECT: '', H2B_ZENOH_GOSSIP: '0',
  H2B_CONTROL_BIN: binary, PATH: dirname(binary) + ':' + (process.env.PATH || ''),
  H2B_DSH_DEMO_LEDGER: join(root, 'state', 'dsh-ledger.json')
});
await mkdir(env.H2B_HOME, { recursive: true });
await mkdir(env.HARNESS_STATE_DIR, { recursive: true });
// Set roots before loading the Host, whose paths are derived at module load.
for (const key of Object.keys(process.env)) if (unsafeEnvironment.test(key)) delete process.env[key];
Object.assign(process.env, env);
const { apply } = await import('../static/host.js');
const timeout = ms => new Promise(resolve => setTimeout(resolve, ms));
let daemon = null, daemonLog = '', checks = 0, interrupted = false;
const commands = new Set();
function killGroup(child, signal) {
  if (!Number.isInteger(child.pid)) return;
  try { process.kill(-child.pid, signal); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
}
function handleSignal(signal) {
  interrupted = true;
  process.exitCode = signal === 'SIGINT' ? 130 : 143;
  for (const child of commands) killGroup(child, 'SIGKILL');
  if (daemon) killGroup(daemon, 'SIGTERM');
  // Do not exit here: command/until unwind into the normal awaited cleanup.
}
const onSigint = () => handleSignal('SIGINT');
const onSigterm = () => handleSignal('SIGTERM');
process.on('SIGINT', onSigint);
process.on('SIGTERM', onSigterm);
function pass(name) { checks++; console.log('PASS ' + name); }
async function command(bin, args, input, extraEnv = {}, limit = 30000) {
  if (interrupted) throw Error('test interrupted');
  const child = spawn(bin, args, { env: { ...env, ...extraEnv }, detached: true, stdio: ['pipe', 'pipe', 'pipe'] });
  commands.add(child);
  let out = '', err = '', expired = false, overflow = false, bytes = 0;
  const timer = setTimeout(() => { expired = true; killGroup(child, 'SIGKILL'); }, limit);
  function collect(kind, data) {
    bytes += data.length;
    if (bytes > 2e6) { overflow = true; killGroup(child, 'SIGKILL'); return; }
    if (kind === 'out') out += data; else err += data;
  }
  child.stdout.on('data', data => collect('out', data));
  child.stderr.on('data', data => collect('err', data));
  child.stdin.on('error', error => { if (error.code !== 'EPIPE') killGroup(child, 'SIGKILL'); });
  child.stdin.end(input || '');
  try { const [code] = await once(child, 'close'); return { code, out, err, expired, overflow }; }
  finally { clearTimeout(timer); commands.delete(child); }
}
async function cli(...args) {
  const r = await command(binary, args);
  assert.equal(r.expired, false, 'CLI timed out: ' + args.join(' '));
  assert.equal(r.overflow, false, 'CLI output exceeded safety limit');
  assert.equal(r.code, 0, args.join(' ') + ': ' + (r.err || r.out));
  return JSON.parse(r.out);
}
async function until(read, predicate, label, ms = 30000) {
  const end = Date.now() + ms; let last;
  while (Date.now() < end) {
    if (interrupted) throw Error('test interrupted');
    try { last = await read(); if (predicate(last)) return last; } catch (error) { last = error.message; }
    await timeout(300);
  }
  throw Error(label + ': ' + JSON.stringify(last));
}
async function start() {
  if (interrupted) throw Error('test interrupted');
  daemon = spawn(binary, ['daemon', 'run'], { env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
  daemon.on('error', error => { daemonLog += '\n' + error.message; });
  daemon.stdout.on('data', x => { daemonLog = (daemonLog + x).slice(-12000); });
  daemon.stderr.on('data', x => { daemonLog = (daemonLog + x).slice(-12000); });
  const state = await until(() => cli('ps', '--json'), x => x.daemon?.running, 'daemon readiness');
  assert.equal(state.daemon.nodeId, env.H2B_NODE_ID);
  assert.equal(state.daemon.socket, env.HARNESS_SOCKET_PATH);
  assert.equal(state.daemon.pid, daemon.pid);
  assert.ok(Array.isArray(state.adapters), 'ps must expose adapters');
  assert.equal(state.adapters.length, 0);
  assert.ok(Array.isArray(state.zenoh?.connect), 'ps must expose connect endpoints');
  assert.deepEqual(state.zenoh.connect, []);
  assert.ok(Array.isArray(state.zenoh?.listen), 'ps must expose listen endpoints');
  assert.equal(state.zenoh.listen.length, 1);
  assert.match(state.zenoh.listen[0], /^tcp\/127\.0\.0\.1:\d+$/);
  return state.daemon.epoch;
}
async function stop() {
  if (!daemon) return;
  const child = daemon; daemon = null;
  if (child.exitCode !== null || child.signalCode !== null) return;
  const closed = once(child, 'close'); killGroup(child, 'SIGTERM');
  const timer = setTimeout(() => killGroup(child, 'SIGKILL'), 10000);
  try { await closed; } finally { clearTimeout(timer); }
}
let route;
const registeredTools = new Map();
const dispose = apply({ sessions: { get: () => undefined }, sessionPersistence: {
  async inspect(id) {
    assert.ok(['isolated-workflow-author', 'isolated-workflow-successor'].includes(id));
    return { meta: { id } };
  }
}, tools: { register(definition) { registeredTools.set(definition.name, definition); return () => {}; } }, on() {}, webServer: { register(value) { route = value; return () => {}; } }, shell: {
  resolve: spec => spec,
  async run(spec) {
    const r = await command('bash', ['-c', spec.command], spec.stdin, spec.env, spec.timeoutMs || 30000);
    return { exitCode: r.overflow ? 1 : r.code, timedOut: r.expired, stdout: { text: r.out, truncated: r.overflow }, stderr: { text: r.err, truncated: r.overflow } };
  }
} });
async function host(method, args) {
  const req = Readable.from([Buffer.from(JSON.stringify({ method, args }))]); req.method = 'POST';
  req.headers = {host:'127.0.0.1:3080',origin:'http://127.0.0.1:3080','content-type':'application/json'};
  let status, response;
  await route.handler(req, { writeHead(code) { status = code; }, end(body) { response = JSON.parse(body); } });
  assert.equal(status, 200, JSON.stringify(response));
  assert.equal(response.ok, true); return response.value;
}
const action = (operation, args = {}) => host('h2b-control-action', { operation, ...args });
const query = operation => host('h2b-control-query', { operation });
const actor = name => `agent:console-e2e:console-e2e-node:${name}`;
try {
  console.log('H2B executable: ' + binary);
  console.log('H2B version: ' + JSON.stringify(await cli('version', '--json')));
  let epoch = await start(); pass('isolated-daemon');
  const reloaded = await action('adapter-reload', { confirmed: true });
  assert.equal(reloaded.document.ok, true);
  const afterReload = await cli('ps', '--json');
  assert.equal(afterReload.daemon.epoch, epoch);
  assert.deepEqual(afterReload.adapters, []);
  pass('isolated-adapter-config-reload');
  for (const name of ['coordinator', 'worker-a', 'worker-b']) await cli('agent', 'create', '--name', name, '--json');
  const authorSession = 'isolated-workflow-author';
  const connected = await host('h2b-demo-rpc', {operation:'status',sessionId:authorSession});
  assert.equal(connected.sessionRegistered, false, 'Fresh author must start unregistered');
  const workflow = (operation, fields = {}) => host('h2b-workflow-workbench',{operation,...fields});
  const workflowTool = (name, args = {}) => registeredTools.get('h2b_workflow_'+name).execute(args,{agent:{session:{id:authorSession}}});
  const draft = await workflow('create',{sessionId:authorSession,name:'agent-authored'});
  const instruction = await workflow('instruct',{id:draft.id,baseRevision:0,text:'Review both isolated workers and run once',mode:'run'});
  const proposedYaml = 'version: 1\nname: agent-authored\ntask: "Review {{target}}; reply DONE {{nonce}} when finished"\ntargets: [{name: worker-a, role: review}, {name: worker-b, role: review}]\nawait: {kind: reply, timeout: 60s, match: "DONE {{nonce}}"}\non_timeout: {action: report}\n';
  const proposal = await workflowTool('propose',{id:draft.id,baseRevision:0,yaml:proposedYaml,instructionId:instruction.instruction.id});
  assert.equal(proposal.grant.revision,1);
  assert.equal((await workflow('get',{id:draft.id})).revisions[0].yaml,proposedYaml);
  pass('workflow-tool-proposal-visible-in-host-workbench');
  const checked = await workflowTool('validate',{id:draft.id,revision:1});
  assert.equal(checked.revisions[0].validation.ok,true,JSON.stringify(checked.revisions[0].validation));
  assert.equal(checked.revisions[0].validation.plan.targets.length,2);
  pass('workflow-tool-real-cli-validation');
  const executed = await workflowTool('execute',{id:draft.id,revision:1});
  assert.equal(executed.runs[0].outcome,'started',JSON.stringify(executed.runs[0]));
  const linkedRunId = executed.runs[0].runId;
  await assert.rejects(workflowTool('execute',{id:draft.id,revision:1}),{code:'WORKFLOW_AUTHORIZATION_REQUIRED'});
  pass('workflow-tool-single-authorized-start');
  let linked = await until(()=>workflowTool('inspect',{id:draft.id,runId:linkedRunId}),x=>x.status.targets?.length===2 && x.status.targets.every(t=>t.conversationId),'linked targets dispatched');
  await assert.rejects(workflow('rebind',{id:draft.id,baseRevision:1,baseBindingVersion:0,sessionId:null}),/WORKFLOW_BINDING_BUSY/);
  for (const target of linked.status.targets) {
    const replyArgs=[];
    if(process.argv.includes('--node-observe')) {
      const observed=await workflow('node-inspect',{id:draft.id,runId:linkedRunId,target:target.target});
      assert.equal(observed.node.state,'available',JSON.stringify(observed.node));
      assert.equal(observed.node.deliveries.length,1);
      replyArgs.push('--reply-to',observed.node.deliveries[0].deliveryId);
    }
    await cli('send','--from',actor(target.target.split(':').at(-1)),'--to',connected.actor,'--conversation',target.conversationId,...replyArgs,'DONE '+linked.status.nonce,'--json');
  }
  linked = await until(()=>workflowTool('inspect',{id:draft.id,runId:linkedRunId}),x=>x.status.state==='completed','linked reply completion');
  assert.ok(linked.status.targets.every(t=>t.state==='done'));
  if(process.argv.includes('--node-observe')) {
    for(const target of linked.status.targets) {
      const observed=await workflow('node-inspect',{id:draft.id,runId:linkedRunId,target:target.target});
      assert.equal(observed.node.results.replies.length,1,JSON.stringify(observed.node));
      assert.equal(observed.node.results.replies[0].text,'DONE '+linked.status.nonce);
      assert.equal(observed.node.results.replies[0].matchesAwait,true);
    }
    pass('workflow-node-host-bridge-daemon-correlated-consumed-replies');
  }
  await workflow('edit',{id:draft.id,baseRevision:1,field:'timeout',value:1800});
  const evidence = await workflow('analyze',{id:draft.id,runId:linkedRunId,target:linked.status.targets[0].target});
  assert.equal(evidence.revision,1); assert.equal(evidence.snapshot.yaml,proposedYaml);
  const reused = await workflow('clone',{id:draft.id,revision:1});
  assert.equal(reused.revisions[0].yaml,proposedYaml);
  pass('workflow-run-evidence-snapshot-and-reuse');
  const released = await workflow('rebind',{id:draft.id,baseRevision:2,baseBindingVersion:0,sessionId:null});
  assert.equal(released.sessionId,null);
  assert.equal((await workflow('inspect',{id:draft.id,runId:linkedRunId})).sender,connected.actor);
  await assert.rejects(workflowTool('context',{id:draft.id}),{code:'WORKFLOW_SESSION_MISMATCH'});
  const transferred = await workflow('rebind',{id:draft.id,baseRevision:2,baseBindingVersion:1,sessionId:'isolated-workflow-successor'});
  assert.equal(transferred.runs[0].sessionId,authorSession);
  const successor = await workflow('validate',{id:draft.id,revision:2});
  assert.equal(successor.revisions.at(-1).validation.ok,true);
  assert.notEqual(successor.revisions.at(-1).validation.from,connected.actor);
  pass('workflow-live-state-release-and-transfer-preserves-original-sender');
  if (!workflowOnly) {
  const from = actor('coordinator');
  const yaml = 'version: 1\nname: cli-console-multi\ntask: "VERIFY {{target}}"\ntargets: [worker-a, worker-b]\nawait: {kind: reply, timeout: 60s, match: "DONE"}\non_timeout: {action: report}\n';
  const preview = await action('workflow-plan', { yaml, from }); assert.ok(preview.previewToken); pass('host-cli-workflow-plan');
  const started = await action('workflow-run', { yaml, from, confirmed: true, previewToken: preview.previewToken });
  const runId = started.document.runId; assert.ok(runId); pass('host-cli-workflow-run');
  let state = await until(() => action('workflow-status', { runId }), x => x.document.targets?.length === 2 && x.document.targets.every(t => t.conversationId), 'targets dispatched');
  for (const target of state.document.targets) await cli('send', '--from', actor(target.target.split(':').at(-1)), '--to', from, '--conversation', target.conversationId, 'DONE console isolated', '--json');
  state = await until(() => action('workflow-status', { runId }), x => x.document.state === 'completed', 'reply completion');
  assert.ok(state.document.targets.every(t => t.state === 'done')); pass('two-target-reply-terminal');
  const pendingYaml = yaml.replace('cli-console-multi', 'cli-console-pending')
    .replace('targets: [worker-a, worker-b]', 'targets: [' + actor('worker-a') + ']')
    .replace('timeout: 60s', 'timeout: 240s');
  const p2 = await action('workflow-plan', { yaml: pendingYaml, from });
  const pending = await action('workflow-run', { yaml: pendingYaml, from, previewToken: p2.previewToken, confirmed: true });
  const routineYaml = 'version: 1\nname: cli-console-schedule\nschedule: {interval: 60s}\nsource: {kind: pac-journal, idle_threshold: 1s}\npolicy:\n  routes: [{tag: "route:self", target: self}]\n  default: self\n  task_template: "CHECK {{nonce}} {{task.description}}"\nlimits:\n  max_in_flight: 1\n  circuit_breaker: {window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm"}\non_task_timeout: {action: escalate, escalate_to: "user:console-e2e"}\n';
  const rp = await action('routine-plan', { yaml: routineYaml, from }); assert.ok(rp.previewToken); pass('host-cli-routine-plan');
  await action('routine-add', { yaml: routineYaml, from, previewToken: rp.previewToken, confirmed: true }); pass('host-cli-routine-add');
  const triggered = await until(() => action('routine-status', { name: 'cli-console-schedule' }), x => x.document.inFlight?.length > 0, 'routine scheduled trigger', 90000);
  const childRunId = triggered.document.inFlight[0].runId;
  assert.ok(childRunId && childRunId !== pending.document.runId);
  assert.equal((await action('workflow-status', { runId: childRunId })).document.runId, childRunId);
  pass('routine-trigger-linked-workflow');
  await action('routine-pause', { name: 'cli-console-schedule', confirmed: true });
  let routine = await action('routine-status', { name: 'cli-console-schedule' });
  assert.equal(routine.document.enabled, false); pass('routine-pause');
  await stop(); const nextEpoch = await start(); assert.notEqual(nextEpoch, epoch);
  assert.equal((await action('workflow-status', { runId })).document.state, 'completed');
  assert.equal((await action('routine-status', { name: 'cli-console-schedule' })).document.enabled, false); pass('daemon-restart-persisted-state');
  const cancelled = await action('workflow-cancel', { runId: pending.document.runId, confirmed: true });
  assert.equal(cancelled.document.state, 'cancelled'); pass('host-cli-workflow-cancel');
  await action('routine-resume', { name: 'cli-console-schedule', confirmed: true });
  assert.equal((await action('routine-status', { name: 'cli-console-schedule' })).document.enabled, true); pass('routine-resume');
  await action('routine-remove', { name: 'cli-console-schedule', confirmed: true });
  assert.equal((await query('routines')).document.routines.length, 0); pass('routine-remove');
  assert.ok((await query('workflows')).document.runs.some(r => r.runId === runId)); pass('host-cli-list');
  }
} catch (error) {
  console.error('FAIL ' + error.stack); console.error(daemonLog); process.exitCode ||= 1;
} finally {
  for (const child of commands) killGroup(child, 'SIGKILL');
  await stop(); dispose();
  await rm(root, { recursive: true, force: true });
  process.off('SIGINT', onSigint);
  process.off('SIGTERM', onSigterm);
  console.log('cleanup: isolated daemon stopped and test home removed');
  console.log(`RESULT ${process.exitCode ? 'FAIL' : 'PASS'} DSH-CLI-CONSOLE scope=${workflowOnly ? 'workflow' : 'all'} checks=${checks}`);
}
