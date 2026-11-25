// @vitest-environment node

import { request as httpRequest, type IncomingMessage } from 'node:http';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { startContractMockServer } from '../../contract-mock/serve.ts';
import { connectBrowserBackend } from '../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { DomainError } from '../../src/client';
import { HOST_TEST_TIMEOUT_MS, startPythonHost, type PythonHostOptions } from '../support/python-host';

vi.setConfig({ testTimeout: HOST_TEST_TIMEOUT_MS });

const TOKEN = 'isolated-contract-mock-token';
const GRANT = 'isolated-launch-grant-'.padEnd(64, 'g');
const PRINCIPAL = { owner: 'owner', node: 'node', actor: 'user:owner' } as const;
const cleanups: Array<() => Promise<void>> = [];

afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
});

async function startContractMock(): Promise<string> {
  const server = await startContractMockServer({ scenario: 'empty', token: TOKEN, port: 0 });
  cleanups.push(() => server.close());
  return server.endpoint;
}

async function startHost(settings: PythonHostOptions): Promise<string> {
  const running = await startPythonHost(settings);
  cleanups.push(running.close);
  return running.origin;
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

function cookiesFrom(headers: IncomingMessage['headers']): string {
  const values = headers['set-cookie'];
  const list = Array.isArray(values) ? values : values === undefined ? [] : [values];
  return list.map((value) => value.split(';', 1)[0]).join('; ');
}

function browserTransport(origin: string): {
  readonly fetch: typeof fetch;
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
  return { fetch: fetcher as typeof fetch, requests, authorizationHeaders };
}

describe('real Contract Mock loopback', () => {
  it('relays a read without CSRF and exposes a missing scenario as 409', async () => {
    const endpoint = await startContractMock();
    const origin = await startHost({
      driver: 'contract-mock',
      principal: PRINCIPAL,
      endpoint,
      upstreamToken: TOKEN,
      launchGrant: GRANT,
      launchTtlMs: 60_000,
      sessionTtlMs: 60_000,
      maxSessions: 8,
    });

    const opened = await call(origin, '/daemon/session', 'POST', { grant: GRANT });
    expect(opened.status).toBe(200);
    const cookies = cookiesFrom(opened.headers);
    const sessionHeaders = {
      cookie: cookies,
      'x-hyprial-view': String(opened.json.view),
      'x-hyprial-generation': String(opened.json.generation),
    };

    const catalog = await call(origin, '/daemon/request', 'GET', undefined, sessionHeaders);
    expect(catalog.status).toBe(200);
    expect(catalog.json.methods).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'agent.list', mutates: false }),
      expect.objectContaining({ name: 'agent.create', mutates: true }),
    ]));

    const read = await call(origin, '/daemon/request/agent.list', 'POST', {}, sessionHeaders);
    expect(read.status).toBe(200);
    expect(read.json).toMatchObject({
      ok: true,
      method: 'agent.list',
      dispatch: 'dispatched',
    });

    const missing = await call(origin, '/daemon/request/agent.create', 'POST', { name: 'worker' }, {
      ...sessionHeaders,
      'x-hyprial-csrf': String(opened.json.csrf),
    });
    expect(missing.status).toBe(409);
    expect(missing.json).toMatchObject({
      ok: false,
      method: 'agent.create',
      code: 'MOCK_SCENARIO_MISSING',
      dispatch: 'not-dispatched',
    });
  }, 30_000);

  it('drives the ordinary generated client and four adapters through the same host', async () => {
    const endpoint = await startContractMock();
    const origin = await startHost({
      driver: 'contract-mock',
      principal: PRINCIPAL,
      endpoint,
      upstreamToken: TOKEN,
      launchGrant: GRANT,
      launchTtlMs: 60_000,
      sessionTtlMs: 60_000,
      maxSessions: 8,
    });
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
    expect(bootstrap.session.driver).toBe('contract-mock');
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

    let refusal: unknown;
    try { await bootstrap.connection.client.agent.provider.default({}); } catch (error) { refusal = error; }
    expect(refusal).toBeInstanceOf(DomainError);
    expect(refusal).toMatchObject({ code: 'MOCK_SCENARIO_MISSING', dispatch: 'not-dispatched' });
    expect((refusal as DomainError).diagnostics.requestId).toMatch(/^mock-/);

    expect(transport.requests.every((path) => path === '/daemon/session' || path.startsWith('/daemon/request')))
      .toBe(true);
    expect(transport.authorizationHeaders.every((value) => value === null)).toBe(true);
    expect(JSON.stringify([...storage])).not.toContain(TOKEN);
    expect(JSON.stringify(bootstrap.session)).not.toContain(TOKEN);
  }, 30_000);
});
