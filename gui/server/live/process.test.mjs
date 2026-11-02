import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createOwnedBackend } from './process.mjs';

const env = {HYPRIAL_GUI_PYTHON: '/trusted/python', HYPRIAL_HOME: '/isolated', HYPRIAL_GUI_PRINCIPAL: 'user:test'};
function child() {
  const value = new EventEmitter();
  Object.assign(value, {stdout: new PassThrough(), stdin: new PassThrough(), pid: 123,
    exitCode: null, signalCode: null, kills: [], kill(signal) {
      this.kills.push(signal);
      if (signal === 'SIGKILL') { this.signalCode = signal; this.emit('exit'); this.emit('close'); }
    }});
  return value;
}

test('timed-out accepted request is never resent and lookup is independent', async () => {
  const process = child();
  const lines = [];
  process.stdin.on('data', chunk => lines.push(JSON.parse(chunk.toString())));
  const backend = createOwnedBackend(env, () => process, {readyMs: 100, callMs: 15, killMs: 5});
  process.stdout.write(JSON.stringify({id: 'ready', ok: true}) + '\n');
  await assert.rejects(backend.call({operation: 'sessions.send', params: {}}), /unknown/);
  assert.equal(lines.length, 1);
  const lookup = backend.call({operation: 'sessions.send.lookup', params: {}});
  await new Promise(resolve => setImmediate(resolve));
  process.stdout.write(JSON.stringify({id: lines[1].id, ok: true, result: {status: 'accepted'}}) + '\n');
  assert.equal((await lookup).result.status, 'accepted');
  assert.equal(lines.length, 2);
  await backend.close();
  assert.deepEqual(process.kills, ['SIGTERM', 'SIGKILL']);
});

test('readiness deadline reaps a non-cooperating owned process', async () => {
  const process = child();
  const backend = createOwnedBackend(env, () => process, {readyMs: 5, killMs: 5});
  await assert.rejects(backend.call({operation: 'targets.list', params: {}}), /readiness/);
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.deepEqual(process.kills, ['SIGTERM', 'SIGKILL']);
  await backend.close();
});

test('spawn failure closes without waiting for an impossible exit', async () => {
  const process = child(); process.pid = undefined;
  const backend = createOwnedBackend(env, () => process);
  process.emit('error', new Error('absent'));
  await assert.rejects(backend.call({operation: 'targets.list', params: {}}), /unavailable/);
  await backend.close();
});

test('maximum admitted pending intents fit the recovery response budget', async () => {
  const process = child();
  const lines = [];
  process.stdin.on('data', chunk => lines.push(JSON.parse(chunk.toString())));
  const backend = createOwnedBackend(env, () => process, {readyMs: 1000, callMs: 1000, killMs: 5});
  process.stdout.write(JSON.stringify({id: 'ready', ok: true}) + '\n');
  const recovery = backend.call({operation: 'sessions.pending', params: {}});
  await new Promise(resolve => setImmediate(resolve));
  const requests = Array.from({length: 100}, (_, index) => ({sessionId: String(index),
    request: {text: '\n'.repeat(65000), idempotencyKey: String(index)}}));
  process.stdout.write(JSON.stringify({id: lines[0].id, ok: true, result: {requests}}) + '\n');
  assert.equal((await recovery).result.requests.length, 100);
  assert.equal(process.kills.length, 0);
  await backend.close();
});

test('concurrent server shutdown closes the owned process once', async () => {
  const process = child();
  const backend = createOwnedBackend(env, () => process, {killMs: 5});
  process.stdout.write(JSON.stringify({id: 'ready', ok: true}) + '\n');
  await Promise.all([backend.close(), backend.close()]);
  assert.deepEqual(process.kills, ['SIGTERM', 'SIGKILL']);
});

test('broken stdin becomes an unknown outcome without an unhandled error', async () => {
  const process = child();
  const backend = createOwnedBackend(env, () => process, {killMs: 5});
  process.stdout.write(JSON.stringify({id: 'ready', ok: true}) + '\n');
  process.stdin.emit('error', new Error('EPIPE'));
  await assert.rejects(backend.call({operation: 'sessions.send', params: {}}), /unavailable/);
  await backend.close();
});
