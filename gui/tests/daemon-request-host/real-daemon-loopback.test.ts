// @vitest-environment node

/**
 * Decision record (M1 real daemon evidence, 2026-11-05)
- Start a disposable daemon and its authenticated request-http listener, then
  drive the same browser bootstrap, host and generated client used by the
  ordinary App. Synthetic files, sockets, ports and credentials stay under one
  temporary directory and never touch a resident daemon or user configuration.
- The assertions cover served ready/catalog identity, browser session framing,
  a real agent read, a real message read, org refresh and an upstream request id.

 * Decision record (UA1f, 2026-11-18; PR 2941 second review)
- The daemon creates the owner's user agent (hyprial-user) on its own thread
  after start, but agent.list hides it unless the request sets
  includeUserAgent: true. The GUI agent read sends no such flag, so the agent
  list of this owner, who created no agents, stays exactly [] and the
  assertion also guards that default.
- That guard holds only once the user agent exists: before it does, [] proves
  nothing. ps is not HTTP exposed and daemon.status has a closed three-key
  result, so the test waits through the existing agent.list route with
  includeUserAgent: true (bounded, USER_AGENT_SETTLE_MS) for the marked row,
  and only then reads the default list.
 */

import { spawn, type ChildProcess } from 'node:child_process';
import { once } from 'node:events';
import { closeSync, openSync } from 'node:fs';
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer, request as httpRequest, type IncomingMessage } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { connectBrowserBackend } from '../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { createContractAgentGateway } from '../../src/features/agents/impl/api';
import { HOST_TEST_TIMEOUT_MS, startPythonHost, type PythonHostOptions } from '../support/python-host';

vi.setConfig({ testTimeout: HOST_TEST_TIMEOUT_MS });

const REPO_ROOT = resolve(fileURLToPath(new URL('../../..', import.meta.url)));
const OWNER = 'm1-real-owner';
const NODE = 'm1-real-node';
const ACTOR = `user:${OWNER}`;
const GRANT = 'm1-real-launch-grant-'.padEnd(64, 'r');
const cleanups: Array<() => Promise<void>> = [];

afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
}, 60_000);

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

const USER_AGENT_SETTLE_MS = 30_000;

async function waitForUserAgent(origin: string, headers: Record<string, string>): Promise<void> {
  const deadline = Date.now() + USER_AGENT_SETTLE_MS;
  let last: unknown = null;
  while (Date.now() < deadline) {
    const listed = await call(origin, '/daemon/request/agent.list', 'POST',
      { includeUserAgent: true }, headers);
    last = listed.json;
    const result = listed.json.result as { agents?: Array<Record<string, unknown>> } | undefined;
    if (listed.status === 200 && result?.agents?.some((row) => row.userAgent === true)) return;
    await delay(200);
  }
  throw new Error(`the daemon never reported its user agent: ${JSON.stringify(last)}`);
}

function cleanEnvironment(root: string): NodeJS.ProcessEnv {
  const env = Object.fromEntries(Object.entries(process.env)
    .filter(([name]) => !name.startsWith('HYPRIAL_') && !name.startsWith('HARNESS_')));
  return {
    ...env,
    HOME: join(root, 'user'),
    HYPRIAL_HOME: join(root, 'home'),
    HARNESS_STATE_DIR: join(root, 'state'),
    // gate-gui does not build the native extension; this request-path test
    // starts the isolated daemon on the packaged pure-Python ABI backend.
    HYPRIAL_ACTOR_BACKEND: 'reference-abi',
    HYPRIAL_NETWORK_ISOLATED: '1',
    HYPRIAL_ZENOH_LISTEN: '',
    HYPRIAL_ZENOH_CONNECT: '',
    HYPRIAL_PEER_DISCOVERY: '0',
    HYPRIAL_OWNER: OWNER,
    HYPRIAL_NODE_ID: NODE,
    NO_PROXY: '127.0.0.1,localhost,::1',
    no_proxy: '127.0.0.1,localhost,::1',
    UV_CACHE_DIR: join(root, 'uv-cache'),
    UV_PYTHON_DOWNLOADS: 'never',
  };
}

async function stopChild(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null) return;
  if (process.platform !== 'win32' && child.pid !== undefined) {
    try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill('SIGTERM'); }
  } else {
    child.kill('SIGTERM');
  }
  await Promise.race([once(child, 'exit'), delay(10_000)]);
  if (child.exitCode !== null) return;
  if (process.platform !== 'win32' && child.pid !== undefined) {
    try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
  } else {
    child.kill('SIGKILL');
  }
  await Promise.race([once(child, 'exit'), delay(3_000)]);
}

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

async function waitFor<T>(read: () => Promise<T>, timeoutMs: number): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      return await read();
    } catch (error) {
      lastError = error;
      await delay(100);
    }
  }
  throw lastError instanceof Error ? lastError : new Error('timed out waiting for isolated services');
}

async function failureTail(logPath: string): Promise<string> {
  try {
    const output = await readFile(logPath, 'utf8');
    return output.slice(-8_000);
  } catch {
    return '(no process log)';
  }
}

async function waitForChildFile(
  child: ChildProcess,
  path: string,
  logPath: string,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`isolated process exited before ${path}:\n${await failureTail(logPath)}`);
    }
    if (await exists(path)) return;
    await delay(100);
  }
  throw new Error(`timed out waiting for ${path}:\n${await failureTail(logPath)}`);
}

function startPython(args: readonly string[], env: NodeJS.ProcessEnv, logPath: string): ChildProcess {
  const log = openSync(logPath, 'a');
  const child = spawn('uv', ['run', '--no-sync', 'python', ...args], {
    cwd: REPO_ROOT,
    detached: process.platform !== 'win32',
    env,
    stdio: ['ignore', log, log],
  });
  closeSync(log);
  return child;
}

async function startRealServices(): Promise<{ readonly endpoint: string; readonly token: string }> {
  const root = await mkdtemp(join(process.platform === 'win32' ? tmpdir() : '/tmp', 'hyprial-gui-'));
  cleanups.push(() => rm(root, { recursive: true, force: true }));
  for (const name of ['home', 'state', 'user', 'runtime']) {
    await mkdir(join(root, name), { recursive: true, mode: 0o700 });
  }
  await writeFile(join(root, 'home', 'settings.json'), JSON.stringify({ version: 1, owner: OWNER }));
  const configPath = join(root, 'request-http.json');
  await writeFile(configPath, JSON.stringify({
    schemaVersion: 1,
    backend: { kind: 'local-daemon', socket: join(root, 'state', 'daemon.sock') },
    principal: { kind: 'local-operator', owner: OWNER, actor: ACTOR },
    listen: { host: '127.0.0.1', port: 0 },
    security: {
      allowedOrigins: ['http://127.0.0.1:4173'],
      allowedHosts: ['127.0.0.1', 'localhost'],
    },
    runtime: { dir: join(root, 'runtime') },
  }), { mode: 0o600 });
  const env = cleanEnvironment(root);
  const daemonLog = join(root, 'daemon.log');
  const daemon = startPython(['-m', 'hyprial.cli', 'daemon', 'run'], env, daemonLog);
  cleanups.push(() => stopChild(daemon));
  const socketPath = join(root, 'state', 'daemon.sock');
  await waitForChildFile(daemon, socketPath, daemonLog, 60_000);

  const httpLog = join(root, 'request-http.log');
  const http = startPython([
    '-m', 'hyprial.shell.impl.request_http', 'serve', '--config', configPath,
  ], env, httpLog);
  cleanups.push(() => stopChild(http));
  const serverJson = join(root, 'runtime', 'server.json');
  await waitForChildFile(http, serverJson, httpLog, 60_000);
  const document = JSON.parse(await readFile(serverJson, 'utf8')) as {
      readonly endpoint?: unknown;
      readonly token?: unknown;
    };
  if (typeof document.endpoint !== 'string' || typeof document.token !== 'string') {
    throw new Error('isolated request-http published an incomplete server.json');
  }
  await waitFor(async () => {
    const response = await fetch(`${document.endpoint}/readyz`, {
      headers: { authorization: `Bearer ${document.token}` },
      redirect: 'error',
    });
    if (response.status !== 200) throw new Error(`isolated request-http readiness returned ${response.status}`);
    return true;
  }, 60_000);
  return { endpoint: document.endpoint, token: document.token };
}

async function startHost(settings: PythonHostOptions): Promise<string> {
  const running = await startPythonHost(settings);
  cleanups.push(running.close);
  return running.origin;
}

interface DroppedWrite {
  readonly path: string;
  readonly request: unknown;
  readonly upstreamStatus: number;
  readonly upstreamBody: unknown;
}

interface DropProxy {
  readonly endpoint: string;
  readonly posts: Array<{ readonly path: string; readonly body: unknown }>;
  readonly dropped: DroppedWrite[];
}

async function startDropProxy(upstreamEndpoint: string, dropPath: string): Promise<DropProxy> {
  const posts: Array<{ path: string; body: unknown }> = [];
  const dropped: DroppedWrite[] = [];
  let armed = true;
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    request.on('end', () => {
      const requestBody = Buffer.concat(chunks);
      const incoming = new URL(request.url ?? '/', 'http://proxy.invalid');
      const path = incoming.pathname;
      let body: unknown = null;
      try { body = JSON.parse(requestBody.toString('utf8')); } catch { body = null; }
      if (request.method === 'POST') posts.push({ path, body });
      const target = new URL(`${path}${incoming.search}`, upstreamEndpoint);
      const headers: Record<string, string | string[] | undefined> = { ...request.headers };
      headers.host = target.host;
      const upstream = httpRequest(target, { method: request.method, headers }, (upstreamResponse) => {
        const upstreamChunks: Buffer[] = [];
        upstreamResponse.on('data', (chunk) => upstreamChunks.push(Buffer.from(chunk)));
        upstreamResponse.on('end', () => {
          const upstreamBytes = Buffer.concat(upstreamChunks);
          if (armed && path === dropPath && request.method === 'POST') {
            armed = false;
            let parsed: unknown = null;
            try { parsed = JSON.parse(upstreamBytes.toString('utf8')); } catch { parsed = null; }
            dropped.push({
              path,
              request: body,
              upstreamStatus: upstreamResponse.statusCode ?? 0,
              upstreamBody: parsed,
            });
            response.destroy();
            return;
          }
          response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
          response.end(upstreamBytes);
        });
      });
      upstream.on('error', () => {
        if (!response.headersSent) response.destroy();
        else response.end();
      });
      if (requestBody.length > 0) upstream.write(requestBody);
      upstream.end();
    });
  });
  await new Promise<void>((resolveListen) => server.listen(0, '127.0.0.1', resolveListen));
  cleanups.push(async () => {
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
  });
  return { endpoint: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, posts, dropped };
}

interface ResponseRecord {
  readonly status: number;
  readonly headers: IncomingMessage['headers'];
  readonly json: Record<string, unknown>;
}

async function call(
  origin: string,
  path: string,
  method: 'GET' | 'POST',
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<ResponseRecord> {
  const bytes = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
  const target = new URL(path, origin);
  const requestHeaders: Record<string, string> = {
    host: target.host,
    origin,
    ...(bytes === undefined ? {} : {
      'content-type': 'application/json',
      'content-length': String(bytes.length),
    }),
    ...headers,
  };
  return new Promise<ResponseRecord>((resolveCall, rejectCall) => {
    const request = httpRequest(target, { method, headers: requestHeaders }, (response) => {
      const chunks: Buffer[] = [];
      response.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
      response.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let json: Record<string, unknown>;
        try { json = JSON.parse(raw) as Record<string, unknown>; }
        catch { json = { raw }; }
        resolveCall({ status: response.statusCode ?? 0, headers: response.headers, json });
      });
    });
    request.on('error', rejectCall);
    if (bytes !== undefined) request.write(bytes);
    request.end();
  });
}

function browserTransport(origin: string): {
  readonly fetch: typeof fetch;
  readonly cookieHeader: () => string;
  readonly requests: string[];
  readonly authorizationHeaders: Array<string | null>;
} {
  const cookies = new Map<string, string>();
  const requests: string[] = [];
  const authorizationHeaders: Array<string | null> = [];
  const fetcher = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    requests.push(`${url.pathname}${url.search}`);
    const headers = new Headers(init?.headers);
    headers.set('origin', origin);
    authorizationHeaders.push(headers.get('authorization'));
    if (cookies.size > 0) headers.set('cookie', [...cookies].map(([name, value]) => `${name}=${value}`).join('; '));
    const response = await fetch(url, { ...init, headers, redirect: 'error' });
    const setCookie = (response.headers as Headers & { getSetCookie?: () => string[] }).getSetCookie?.() ?? [];
    for (const line of setCookie) {
      const [pair, ...attributes] = line.split(';');
      const separator = pair.indexOf('=');
      if (separator <= 0) continue;
      const name = pair.slice(0, separator);
      const value = pair.slice(separator + 1);
      if (attributes.some((attribute) => attribute.trim().toLowerCase() === 'max-age=0')) cookies.delete(name);
      else cookies.set(name, value);
    }
    return response;
  };
  return {
    fetch: fetcher as typeof fetch,
    cookieHeader: () => [...cookies].map(([name, value]) => `${name}=${value}`).join('; '),
    requests,
    authorizationHeaders,
  };
}

function configFor(endpoint: string, token: string, owner = OWNER, grant = GRANT): PythonHostOptions {
  return {
    driver: 'daemon',
    principal: { owner, node: NODE, actor: `user:${owner}` },
    endpoint,
    upstreamToken: token,
    launchGrant: grant,
    launchTtlMs: 60_000,
    sessionTtlMs: 60_000,
    maxSessions: 8,
  };
}

describe('real isolated daemon loopback', () => {
  it('serves ready, catalog and four-line adapters through the ordinary same-origin path', async () => {
    const services = await startRealServices();
    const origin = await startHost(configFor(services.endpoint, services.token));
    const transport = browserTransport(origin);
    const storage = new Map<string, string>();
    const bootstrap = await connectBrowserBackend({
      fetch: transport.fetch,
      location: { origin, hash: `#grant=${GRANT}`, pathname: '/', search: '' },
      history: { replaceState() {} },
      storage: {
        getItem: (key) => storage.get(key) ?? null,
        setItem: (key, value) => { storage.set(key, value); },
        removeItem: (key) => { storage.delete(key); },
      },
    });
    expect(bootstrap.session).toMatchObject({
      driver: 'daemon',
      phase: 'ready',
      principal: { owner: OWNER, node: NODE, actor: ACTOR },
    });
    expect(bootstrap.session.catalog.digest).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(bootstrap.session.catalog.methodCount).toBeGreaterThan(0);

    const authenticated = {
      cookie: transport.cookieHeader(),
      'x-hyprial-view': bootstrap.session.view,
      'x-hyprial-generation': bootstrap.session.generation,
    };
    const ready = await call(origin, '/daemon/readyz', 'GET', undefined, authenticated);
    expect(ready.status).toBe(200);
    expect(ready.json).toMatchObject({ ready: true, backend: { owner: OWNER, nodeId: NODE } });

    const catalog = await call(origin, '/daemon/request', 'GET', undefined, authenticated);
    expect(catalog.status).toBe(200);
    expect(catalog.json).toMatchObject({
      generatedFrom: 'daemon.catalog',
      catalogDigest: bootstrap.session.catalog.digest,
      principal: { owner: OWNER, actor: ACTOR },
    });

    const rawRead = await call(origin, '/daemon/request/agent.list', 'POST', {}, authenticated);
    expect(rawRead.status).toBe(200);
    expect(rawRead.json).toMatchObject({ method: 'agent.list', dispatch: 'dispatched' });
    expect(rawRead.json.requestId).toMatch(/^http-/);

    await waitForUserAgent(origin, authenticated);
    const capabilities = createConnectedGuiCapabilities(bootstrap.connection);
    await expect(capabilities.agents?.list?.()).resolves.toEqual([]);
    await expect(capabilities.messageRead?.query('outbox')).resolves.toEqual([]);
    const source = capabilities.orgSource;
    expect(source).toBeDefined();
    let snapshot = source!.initial;
    const gateway = source!.createGateway({
      get: () => snapshot,
      update: (next) => { snapshot = next(snapshot); },
    });
    await expect(gateway.refresh(null)).resolves.toMatchObject({ kind: 'ok' });

    expect(transport.requests.every((path) => path === '/daemon/session' || path.startsWith('/daemon/request')))
      .toBe(true);
    expect(transport.authorizationHeaders.every((value) => value === null)).toBe(true);
    expect(JSON.stringify([...storage])).not.toContain(services.token);
    expect(JSON.stringify(bootstrap.session)).not.toContain(services.token);
  }, 90_000);

  it('refuses a trusted principal that differs from the real daemon owner', async () => {
    const services = await startRealServices();
    const origin = await startHost(configFor(services.endpoint, services.token, 'different-owner'));
    const opened = await call(origin, '/daemon/session', 'POST', { grant: GRANT });
    expect(opened.status).toBe(502);
    expect(opened.json).toMatchObject({
      ok: false,
      code: 'PRINCIPAL_MISMATCH',
      dispatch: 'not-dispatched',
    });
    expect(opened.json.requestId).toMatch(/^gui-/);
  }, 90_000);

  it('commits one agent.create when its response is lost and reconciles by business key', async () => {
    const services = await startRealServices();
    const proxy = await startDropProxy(services.endpoint, '/daemon/request/agent.create');
    const origin = await startHost(configFor(proxy.endpoint, services.token));
    const transport = browserTransport(origin);
    const storage = new Map<string, string>();
    const bootstrap = await connectBrowserBackend({
      fetch: transport.fetch,
      location: { origin, hash: `#grant=${GRANT}`, pathname: '/', search: '' },
      history: { replaceState() {} },
      storage: {
        getItem: (key) => storage.get(key) ?? null,
        setItem: (key, value) => { storage.set(key, value); },
        removeItem: (key) => { storage.delete(key); },
      },
    });
    const capabilities = createConnectedGuiCapabilities(bootstrap.connection);
    const api = capabilities.agents;
    expect(api).toBeDefined();
    const unique = `m1-g6-${process.pid}-${Date.now()}`;
    let reloads = 0;
    const gateway = createContractAgentGateway({
      api: api!,
      afterCreate: () => { reloads += 1; },
    });

    const result = await gateway!.create({
      displayName: unique,
      role: '',
      modelServiceId: '',
      organizationId: null,
      provider: null,
      model: null,
      preferredHarness: null,
    });

    expect(result.kind).toBe('unknown');
    if (result.kind !== 'unknown') throw new Error(`expected unknown, received ${result.kind}`);
    expect(result.message).toContain('不会自动重试');
    expect(result.message).toContain('核对列表');
    expect(result.diagnostics?.dispatch).toBe('unknown');
    expect(reloads).toBe(1);

    const creates = proxy.posts.filter((entry) => entry.path === '/daemon/request/agent.create');
    expect(creates).toHaveLength(1);
    expect(creates[0].body).toMatchObject({ name: unique });
    expect(proxy.dropped).toHaveLength(1);
    expect(proxy.dropped[0].upstreamStatus).toBe(200);
    expect(proxy.dropped[0].upstreamBody).toMatchObject({
      ok: true,
      method: 'agent.create',
      dispatch: 'dispatched',
      result: { created: true },
    });

    await delay(400);
    expect(proxy.posts.filter((entry) => entry.path === '/daemon/request/agent.create')).toHaveLength(1);

    const first = await api!.list!();
    const matching = first.filter((row) => row.actor === unique);
    expect(matching).toHaveLength(1);
    const second = await api!.list!();
    expect(second.filter((row) => row.actor === unique)).toHaveLength(1);
    expect(transport.requests.filter((path) => path === '/daemon/request/agent.create')).toHaveLength(1);
  }, 120_000);
});
