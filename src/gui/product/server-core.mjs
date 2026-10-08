import { createServer } from 'node:http';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHost } from '../host/index.mjs';
import { createStudio } from '../studio/index.mjs';
import { createTransportDriver } from '../transport/index.mjs';
import { createUiRuntime } from '../client/index.mjs';
import { trustedPanel } from '../plugins/trusted-panel.mjs';
import { renderWorkspacePage } from './page.mjs';

const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
const PRODUCT_ROOT = resolve(fileURLToPath(new URL('.', import.meta.url)), '..');

// The trusted plugin registry is fixed product code. Neither the network, nor
// configuration, nor request bodies can name a plugin: the composition root
// alone decides what runs inside the UI runtime.
const TRUSTED_PLUGINS = Object.freeze({ panel: trustedPanel });

const ERROR_STATUS = {
  INVALID_ARGUMENT: 400, GUI_ARGUMENT_REJECTED: 400, INVALID_CURSOR: 400,
  BODY_TOO_LARGE: 413, GUI_FORBIDDEN: 403, INVALID_CONTEXT: 403,
  SESSION_NOT_FOUND: 404, COMMAND_NOT_FOUND: 404, UNKNOWN_OPERATION: 400,
  IDEMPOTENCY_CONFLICT: 409, GUI_REVISION_CONFLICT: 409, HOST_CLOSED: 503,
};

function sendJson(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  response.end(body);
}

async function readBody(request, limit = 262144) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) fail('BODY_TOO_LARGE', 'Request body exceeds 256 KiB');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

/**
 * Assemble and serve the GUI product composition root.
 *
 * The server is the trusted boundary: it owns the per-boot bearer token, and
 * `resolveContext` maps an authenticated request to the composition-root
 * identity. Request bodies can never name a principal (the Host port also
 * rejects identity fields), so a browser JSON payload cannot self-report who
 * it is.
 */
export async function createGuiServer({ transport, principalId, studio: studioOptions, listen = {}, token, assetsDir } = {}) {
  if (typeof principalId !== 'string' || !principalId.trim()) {
    fail('GUI_PRODUCT_CONFIG', 'createGuiServer requires a non-empty principalId');
  }
  const adapter = await createTransportDriver(transport);
  const studio = createStudio(studioOptions ?? { persistence: 'memory' });
  const bearer = token ?? randomUUID().replaceAll('-', '') + randomUUID().replaceAll('-', '');
  const context = Object.freeze({ source: 'user', principalId });
  const host = createHost({
    sessionAdapter: adapter,
    studio,
    async resolveContext(credentials) {
      if (!credentials || credentials.token !== bearer) fail('GUI_UNAUTHENTICATED', 'Request is not authenticated');
      return context;
    },
  });
  const cleanupLog = [];
  const stats = { hits: 0 };
  const ui = createUiRuntime({ trustedPlugins: TRUSTED_PLUGINS, configuration: { panel: { cleanupLog, stats } } });
  await ui.mount('panel');

  const assets = assetsDir ?? join(PRODUCT_ROOT, 'dist');
  const sourceAssets = join(PRODUCT_ROOT, 'product', 'assets');
  async function asset(name, type) {
    for (const directory of [assets, sourceAssets]) {
      try { return await readFile(join(directory, name)); } catch { /* fall through to the source asset */ }
    }
    fail('ASSET_NOT_FOUND', `GUI asset is not built: ${name}`);
  }

  async function pageData(sessionId) {
    const credentials = { token: bearer };
    const [sessions, capabilities, drafts] = await Promise.all([
      host.request({ service: 'sessions', operation: 'list' }, credentials),
      host.request({ service: 'capabilities', operation: 'get' }, credentials),
      host.request({ service: 'studio', operation: 'list' }, credentials),
    ]);
    let history = null;
    let terminal = null;
    if (typeof sessionId === 'string' && sessionId) {
      const page = await host.request({ service: 'sessions', operation: 'history', sessionId, args: { limit: 200 } }, credentials);
      history = page.events;
      const events = await host.request({ service: 'sessions', operation: 'events', sessionId, args: { since: 0 } }, credentials);
      terminal = events.terminal;
    }
    return { sessions, capabilities, draftCount: drafts.drafts?.length ?? 0, currentSessionId: sessionId || null, history, terminal };
  }

  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://localhost');
      if (request.method === 'GET' && url.pathname === '/healthz') {
        return sendJson(response, 200, { status: 'ok', product: '@hyprial/gui' });
      }
      if (request.method === 'GET' && url.pathname === '/assets/client.js') {
        const body = await asset('client.js', 'text/javascript');
        return response.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8' }).end(body);
      }
      if (request.method === 'GET' && url.pathname === '/assets/style.css') {
        const body = await asset('style.css', 'text/css');
        return response.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' }).end(body);
      }
      if (request.method === 'GET' && url.pathname === '/') {
        if (url.searchParams.get('token') !== bearer) {
          return sendJson(response, 401, { ok: false, error: { code: 'GUI_UNAUTHENTICATED', message: 'A valid token is required' } });
        }
        const html = renderWorkspacePage({ ...await pageData(url.searchParams.get('session')), token: bearer });
        return response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(html);
      }
      if (request.method === 'POST' && url.pathname === '/api/request') {
        const header = request.headers.authorization ?? '';
        const credentials = { token: header.startsWith('Bearer ') ? header.slice(7) : null };
        let input;
        try {
          input = JSON.parse(await readBody(request));
        } catch (error) {
          if (error.code === 'BODY_TOO_LARGE') throw error;
          return sendJson(response, 400, { ok: false, error: { code: 'INVALID_ARGUMENT', message: 'Request body must be JSON' } });
        }
        try {
          const value = await host.request(input, credentials);
          return sendJson(response, 200, { ok: true, value });
        } catch (error) {
          const status = ERROR_STATUS[error.code] ?? (error.code === 'GUI_UNAUTHENTICATED' ? 401 : 500);
          return sendJson(response, status, { ok: false, error: { code: error.code ?? 'INTERNAL', message: error.message } });
        }
      }
      if (request.method === 'POST' && url.pathname === '/send') {
        const form = new URLSearchParams(await readBody(request));
        if (form.get('token') !== bearer) {
          return sendJson(response, 401, { ok: false, error: { code: 'GUI_UNAUTHENTICATED', message: 'A valid token is required' } });
        }
        const sessionId = form.get('sessionId') ?? '';
        await host.request({
          service: 'sessions', operation: 'send', sessionId,
          args: { text: form.get('text') ?? '', idempotencyKey: `form-${randomUUID()}` },
        }, { token: bearer });
        response.writeHead(303, { Location: `/?token=${encodeURIComponent(bearer)}&session=${encodeURIComponent(sessionId)}` });
        return response.end();
      }
      sendJson(response, 404, { ok: false, error: { code: 'NOT_FOUND', message: 'Unknown GUI route' } });
    } catch (error) {
      const status = ERROR_STATUS[error.code] ?? 500;
      sendJson(response, status, { ok: false, error: { code: error.code ?? 'INTERNAL', message: error.message } });
    }
  });

  const listenHost = listen.host ?? '127.0.0.1';
  const listenPort = listen.port ?? 0;
  await new Promise((resolveListen, reject) => {
    server.once('error', reject);
    server.listen(listenPort, listenHost, resolveListen);
  });
  const address = server.address();
  const url = `http://${listenHost}:${address.port}/?token=${bearer}`;
  let closed = false;
  return {
    url,
    token: bearer,
    port: address.port,
    request: (input, credentials) => host.request(input, credentials),
    async close() {
      if (closed) return;
      closed = true;
      await new Promise(resolveClose => server.close(resolveClose));
      const results = await Promise.allSettled([ui.dispose(), host.close()]);
      const errors = results.filter(result => result.status === 'rejected').map(result => result.reason);
      if (errors.length === 1) throw errors[0];
      if (errors.length > 1) throw new AggregateError(errors, 'GUI composition root failed to close');
    },
  };
}
