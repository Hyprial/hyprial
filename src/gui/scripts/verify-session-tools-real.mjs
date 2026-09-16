// Explicit opt-in: two disposable local actors, no production sessions/messages.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { installSessionTools } from '../integration/session-tools.js';

if (!process.argv.includes('--run')) throw new Error('Use --run to create and clean up two disposable local test actors');
const root = fileURLToPath(new URL('..', import.meta.url));
const temp = await mkdtemp(path.join(tmpdir(), 'dsh-session-tools-'));
const marker = randomUUID();
const sessions = ['h2b-console-tools-a-' + marker, 'h2b-console-tools-b-' + marker];
const env = { ...process.env, H2B_DSH_DEMO_LEDGER: path.join(temp, 'ledger.json'), H2B_DSH_DEMO_ALLOW_FROM: '' };
const created = [];
function rpc(request) {
  const result = spawnSync(process.execPath, [path.join(root, 'h2b-session-bridge.mjs'), 'rpc'], { cwd: root, env, input: JSON.stringify(request), encoding: 'utf8', timeout: 15000 });
  const value = JSON.parse(result.stdout || '{}');
  if (result.status !== 0 || value.ok === false) throw new Error(JSON.stringify(value.error || { message: 'bridge failed' }));
  return value;
}
const tools = new Map();
installSessionTools({ tools: { register(def) { tools.set(def.name, def); return () => {}; } }, on() {} }, rpc);
const call = (index, name, args = {}) => tools.get('h2b_session_' + name).execute(args, { agent: { session: { id: sessions[index] } } });
async function waitMessage(index, text) {
  for (let attempt = 0; attempt < 30; attempt++) {
    const inbox = await call(index, 'inbox');
    const item = inbox.messages.find(item => item.message === text);
    if (item) return item;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('message did not arrive at its owning session');
}
try {
  for (const sessionId of sessions) {
    const registered = rpc({ operation: 'connect', sessionId });
    created.push({ sessionId, actor: registered.actor });
  }
  const [a, b] = await Promise.all([call(0, 'identity'), call(1, 'identity')]);
  assert.notEqual(a.actor, b.actor);
  const ping = 'session-tool-ping-' + marker, pong = 'session-tool-pong-' + marker;
  await call(0, 'send', { target: b.actor, message: ping });
  await call(1, 'send', { target: a.actor, message: pong });
  const inbound = await waitMessage(1, ping);
  assert.equal(inbound.from, a.actor);
  await assert.rejects(call(0, 'ack', { messageId: inbound.messageId }), /MESSAGE_NOT_AUTHORIZED/);
  const reply = 'session-tool-result-' + marker;
  await call(1, 'reply', { messageId: inbound.messageId, message: reply });
  const received = await waitMessage(0, reply);
  assert.equal(received.from, b.actor);
  await call(0, 'ack', { messageId: received.messageId });
  const opposite = await waitMessage(0, pong);
  await call(0, 'ack', { messageId: opposite.messageId });
  console.log('PASS session tools: real daemon, two identities, isolated inbox, send/reply/ACK');
} finally {
  for (const { sessionId, actor } of created) {
    try { rpc({ operation: 'disconnect', sessionId }); }
    catch (error) { console.error('test disconnect failed:', error.message); process.exitCode = 1; }
    // Exact actors created by this run only. Never select by a broad prefix.
    const result = spawnSync('h2b', ['agent', 'destroy', actor.split(':').at(-1), '--yes', '--json'], { encoding: 'utf8', timeout: 15000 });
    if (result.status !== 0) { console.error('test actor cleanup failed:', actor); process.exitCode = 1; }
  }
  await rm(temp, { recursive: true, force: true });
}
