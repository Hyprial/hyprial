import { randomBytes, timingSafeEqual } from 'node:crypto';
import { createAuthRunner } from './runner.mjs';

const PREFIX = '/__gui/auth/';
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);
function reply(res, status, data) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.end(JSON.stringify(data));
}
function matchesToken(input, expected) {
  return typeof input === 'string' && /^[a-f0-9]{64}$/.test(input) && input.length === expected.length && timingSafeEqual(Buffer.from(input), Buffer.from(expected));
}
async function requestBody(req, action) {
  let body = '';
  for await (const chunk of req) {
    body += chunk;
    if (body.length > (action === 'model-save' ? 5000 : 128)) return null;
  }
  try {
    const value = JSON.parse(body);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const keys = Object.keys(value);
    if (action === 'model-save') return keys.length === 1 && keys[0] === 'key' && typeof value.key === 'string' ? value : null;
    return keys.length === 0 ? value : null;
  }
  catch { return null; }
}

/** Local GUI host capability, not a public daemon/business HTTP contract. */
export function createAuthMiddleware({ origin, enabled = false, pythonExecutable, hyprialHome, runner: injectedRunner, setupRunner = null }) {
  const sessions = new Map();
  let activeOperation = null;
  const runner = enabled ? injectedRunner ?? createAuthRunner({ pythonExecutable, hyprialHome }) : null;
  const middleware = async (req, res, next) => {
    if (!req.url?.startsWith(PREFIX)) { next(); return; }
    let expected;
    try {
      expected = new URL(typeof origin === 'function' ? origin() : origin);
      if (expected.protocol !== 'http:' || !LOOPBACK.has(expected.hostname) || expected.username || expected.password || expected.pathname !== '/') throw new Error();
    } catch { reply(res, 503, { ok: false, code: 'AUTH_CONFIGURATION_INVALID' }); return; }
    let requestOrigin;
    try {
      const requestUrl = new URL(`http://${req.headers.host}`);
      if (!LOOPBACK.has(requestUrl.hostname) || requestUrl.port !== expected.port || requestUrl.username || requestUrl.password || requestUrl.pathname !== '/' || requestUrl.host !== req.headers.host) throw new Error();
      requestOrigin = requestUrl.origin;
    } catch { reply(res, 403, { ok: false, code: 'AUTH_ORIGIN_REJECTED' }); return; }
    if ((req.headers.origin !== undefined && req.headers.origin !== requestOrigin) || (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) {
      reply(res, 403, { ok: false, code: 'AUTH_ORIGIN_REJECTED' }); return;
    }
    if (!enabled) { reply(res, 503, { ok: false, code: 'AUTH_UNAVAILABLE' }); return; }
    const action = req.url.slice(PREFIX.length);
    if (action === 'session' && req.method === 'GET') {
      for (const [key, entry] of sessions) {
        if (Date.now() - entry.createdAt > 30 * 60_000 && activeOperation?.nonce !== key) sessions.delete(key);
      }
      if (sessions.size >= 64) { reply(res, 429, { ok: false, code: 'AUTH_SESSION_LIMIT' }); return; }
      const nonce = randomBytes(32).toString('hex');
      sessions.set(nonce, { nonce, createdAt: Date.now() });
      reply(res, 200, { ok: true, nonce }); return;
    }
    if (req.method !== 'POST' || !['status', 'login', 'device', 'cancel', 'setup-status', 'model-save', 'squire-prepare'].includes(action)) {
      reply(res, 404, { ok: false, code: 'INVALID_ACTION' }); return;
    }
    const session = sessions.get(req.headers['x-hyprial-auth-session']);
    if (req.headers.origin !== requestOrigin || !session || !matchesToken(req.headers['x-hyprial-auth-session'], session.nonce) || req.headers['content-type']?.split(';')[0].trim() !== 'application/json') {
      reply(res, 403, { ok: false, code: 'AUTH_REQUEST_REJECTED' }); return;
    }
    try {
      const body = await requestBody(req, action);
      if (!body) { reply(res, 400, { ok: false, code: 'INVALID_ARGUMENT' }); return; }
      if (action === 'cancel') {
        if (activeOperation && activeOperation.nonce !== session.nonce) { reply(res, 403, { ok: false, code: 'AUTH_NOT_OWNER' }); return; }
        await (activeOperation?.runner ?? runner).cancel(); reply(res, 200, { ok: true }); return;
      }
      if (activeOperation) { reply(res, 409, { ok: false, code: 'AUTH_BUSY' }); return; }
      const isSetup = ['setup-status', 'model-save', 'squire-prepare'].includes(action);
      const selectedRunner = isSetup ? setupRunner : runner;
      if (!selectedRunner) { if (body.key) body.key = ''; reply(res, 503, { ok: false, code: 'SETUP_UNAVAILABLE' }); return; }
      const operation = { nonce: session.nonce, runner: selectedRunner };
      activeOperation = operation;
      const streaming = !['status', 'setup-status', 'model-save'].includes(action);
      let started = false;
      let ended = false;
      const frame = (type, data) => {
        if (res.destroyed) return;
        if (!started) {
          started = true;
          res.setHeader('Content-Type', 'application/x-ndjson');
          res.setHeader('Cache-Control', 'no-store');
          res.setHeader('X-Content-Type-Options', 'nosniff');
        }
        res.write(`${JSON.stringify({ type, data })}\n`);
      };
      const disconnect = () => { if (!ended && activeOperation === operation) void selectedRunner.cancel(); };
      res.once('close', disconnect);
      try {
        const onProgress = progress => { if (streaming) frame('progress', progress); };
        const result = isSetup
          ? await selectedRunner.run(action, body, onProgress, session.nonce)
          : await selectedRunner.run(action, onProgress);
        ended = true;
        if (streaming) { frame('result', result); res.end(); }
        else reply(res, 200, result);
      } catch (error) {
        ended = true;
        const code = /^[A-Z][A-Z0-9_]{0,79}$/.test(error?.code) ? error.code : 'AUTH_FAILED';
        if (streaming && started) { frame('result', { ok: false, code }); res.end(); }
        else reply(res, code === 'AUTH_BUSY' ? 409 : 503, { ok: false, code });
      } finally {
        if (body.key) body.key = '';
        if (activeOperation === operation) activeOperation = null;
        res.off('close', disconnect);
      }
    } catch { if (!res.destroyed) reply(res, 400, { ok: false, code: 'INVALID_ARGUMENT' }); }
  };
  return { middleware, close: async () => { await runner?.close(); await setupRunner?.close(); } };
}
