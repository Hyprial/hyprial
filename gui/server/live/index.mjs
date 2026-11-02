import { randomBytes, timingSafeEqual } from 'node:crypto';
import { createOwnedBackend } from './process.mjs';

const PREFIX = '/api/gui/v1/live';
const OPERATIONS = new Set([
  'targets.list', 'sessions.create', 'sessions.history', 'sessions.pending',
  'sessions.send', 'sessions.send.lookup', 'sessions.send.confirm', 'lark.adapters', 'lark.authorization.status',
  'lark.authorization.start', 'lark.authorization.operation', 'lark.authorization.cancel', 'lark.binding.set',
]);
const HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

function sameOrigin(request) {
  let url;
  try { url = new URL(`http://${request.headers.host}`); } catch { return false; }
  return HOSTS.has(url.hostname) && url.port === String(request.socket.localPort) &&
    url.username === '' && url.password === '' && url.pathname === '/' && url.search === '' && url.hash === '' &&
    (!request.socket.remoteAddress || ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(request.socket.remoteAddress)) &&
    (!request.headers.origin || request.headers.origin === url.origin) &&
    (!request.headers['sec-fetch-site'] || ['same-origin', 'none'].includes(request.headers['sec-fetch-site']));
}

function bodyOf(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let bytes = 0;
    const timer = setTimeout(() => reject(Object.assign(new Error('request timeout'), {status: 408})), 10000);
    request.on('data', chunk => {
      bytes += Buffer.byteLength(chunk);
      if (bytes > 131072) { clearTimeout(timer); reject(Object.assign(new Error('body limit'), {status: 413})); return; }
      chunks.push(Buffer.from(chunk));
    });
    request.once('error', error => { clearTimeout(timer); reject(error); });
    request.once('end', () => { clearTimeout(timer); try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch { reject(new Error('invalid JSON')); } });
  });
}

export function createLiveMiddleware(backend, token = randomBytes(32).toString('hex')) {
  return async (request, response, next) => {
    if (request.url !== PREFIX && request.url !== `${PREFIX}/bootstrap`) { next(); return; }
    const reply = (status, value) => {
      response.statusCode = status;
      response.setHeader('Content-Type', 'application/json; charset=utf-8');
      response.setHeader('Cache-Control', 'no-store');
      response.setHeader('X-Content-Type-Options', 'nosniff');
      response.end(JSON.stringify(value));
    };
    const refuse = (status, code, definitive = true) => reply(status, {ok: false,
      error: {code, message: definitive ? '请求未获允许。' : '结果尚未确认，请查询状态。', definitive, canRetry: false}});
    if (!sameOrigin(request)) { refuse(403, 'origin-rejected'); return; }
    if (request.url.endsWith('/bootstrap') && request.method === 'GET') {
      reply(200, backend ? {enabled: true, csrfToken: token} : {enabled: false}); return;
    }
    if (!backend) { refuse(503, 'backend-disabled', false); return; }
    if (request.url !== PREFIX || request.method !== 'POST') { refuse(405, 'method-rejected'); return; }
    const supplied = request.headers['x-hyprial-gui-token'];
    if (typeof supplied !== 'string' || Buffer.byteLength(supplied) !== Buffer.byteLength(token) ||
        !timingSafeEqual(Buffer.from(supplied), Buffer.from(token))) { refuse(403, 'token-rejected'); return; }
    let frame;
    try {
      frame = await bodyOf(request);
      if (!frame || typeof frame !== 'object' || Array.isArray(frame) ||
          Object.keys(frame).sort().join(',') !== 'operation,params' || !OPERATIONS.has(frame.operation) ||
          !frame.params || typeof frame.params !== 'object' || Array.isArray(frame.params)) throw new Error('invalid frame');
    } catch (error) { refuse(error.status ?? 400, 'invalid-argument'); return; }
    try { reply(200, await backend.call(frame)); }
    catch { refuse(503, 'unavailable', false); }
  };
}

export function liveBackendPlugin(enabled, env = process.env) {
  let backend;
  function configure(server) {
    if (enabled) backend ??= createOwnedBackend(env);
    server.middlewares.use(createLiveMiddleware(backend));
    server.httpServer?.once('close', () => { if (backend) void backend.close(); });
  }
  return {name: 'hyprial-live-backend', configureServer: configure,
    configurePreviewServer: configure, async closeBundle() { if (backend) await backend.close(); }};
}
