import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { createLiveMiddleware } from './index.mjs';

async function call(handler, {method = 'POST', path = '/api/gui/v1/live', headers = {}, body = {}} = {}) {
  const req = new PassThrough();
  Object.assign(req, {method, url: path, socket: {localPort: 4173},
    headers: {host: 'localhost:4173', origin: 'http://localhost:4173', ...headers}});
  let resolve;
  const result = new Promise(r => { resolve = r; });
  const response = {statusCode: 200, setHeader() {}, end(body) {
    resolve({status: this.statusCode, body: JSON.parse(body)});
  }};
  handler(req, response, () => resolve({status: 404}));
  req.end(JSON.stringify(body));
  return result;
}

test('bootstrap capability cannot be read cross-origin or via DNS rebinding', async () => {
  const handler = createLiveMiddleware({call: async () => ({ok: true})}, 'secret');
  assert.equal((await call(handler, {method: 'GET', path: '/api/gui/v1/live/bootstrap'})).body.csrfToken, 'secret');
  assert.equal((await call(handler, {method: 'GET', path: '/api/gui/v1/live/bootstrap', headers: {origin: 'https://evil.test'}})).status, 403);
  assert.equal((await call(handler, {headers: {host: 'evil.test:4173'}})).status, 403);
  assert.equal((await call(handler, {headers: {host: 'localhost:9999'}})).status, 403);
});

test('explicit operation list and CSRF required before owned backend call', async () => {
  const seen = [];
  const handler = createLiveMiddleware({call: async frame => { seen.push(frame); return {ok: true, result: {status: 'unknown'}}; }}, 'secret');
  const body = {operation: 'sessions.send.lookup', params: {sessionId: 's', request: {text: 'hi', idempotencyKey: 'original'}}};
  assert.equal((await call(handler, {body})).status, 403);
  const headers = {'x-hyprial-gui-token': 'secret'};
  assert.equal((await call(handler, {headers, body: {operation: 'message.send', params: {}}})).status, 400);
  assert.equal((await call(handler, {headers, body})).body.result.status, 'unknown');
  assert.deepEqual(seen, [body]);
});

test('lost backend response is unknown; never automatically repeats', async () => {
  let count = 0;
  const handler = createLiveMiddleware({call: async () => { count++; throw Error('lost'); }}, 'secret');
  const result = await call(handler, {headers: {'x-hyprial-gui-token': 'secret'}, body: {operation: 'sessions.send', params: {}}});
  assert.equal(result.body.error.definitive, false);
  assert.equal(count, 1);
});

test('oversized bodies and identity fields refused', async () => {
  let count = 0;
  const handler = createLiveMiddleware({call: async () => { count++; return {ok: true}; }}, 'secret');
  const headers = {'x-hyprial-gui-token': 'secret'};
  assert.equal((await call(handler, {headers, body: {operation: 'targets.list', params: {}, principal: 'other'}})).status, 400);
  assert.equal((await call(handler, {headers, body: {operation: 'sessions.send', params: {text: 'a'.repeat(262144)}}})).status, 413);
  assert.equal(count, 0);
});
