/**
 * Decision record (host conformance harness, 2026-11-15)
- These helpers were extracted from host.test.ts so the conformance suites can
  share one upstream stub, one raw HTTP caller and one cleanup list. They speak
  HTTP to the Python host started through ../support/python-host.ts and import no host
  implementation.
- The `upstream` stub records every request it sees; cases assert on `captured`
  to prove that a refusal never reached the upstream.
- The one HTTP listener for upstream stubs lives in host.test.ts and is bound
  here through bindListen(); the conformance modules are registered from
  host.test.ts as functions, so the request-path ledger keeps its single
  createServer finding at its original path.
- `rawCall` writes the request line byte for byte because the normal caller
  normalises the target through WHATWG URL before sending.
 */

import { expect } from 'vitest';
import { request as httpRequest, type IncomingMessage, type ServerResponse } from 'node:http';
import { connect } from 'node:net';
import { startPythonHost, type PythonHost, type PythonHostOptions } from '../support/python-host';
import { loadSessionContract, validateSchema, type Json, type JsonObject } from './session-corpus';

export const GRANT = 'launch-grant-'.padEnd(64, 'x');
export const UPSTREAM_TOKEN = 'upstream-secret-never-for-browser';
export const PRINCIPAL = { owner: 'owner', node: 'node', actor: 'user:owner' } as const;
export const DIGEST = 'sha256:catalog';
export const DECLARATION = 'sha256:declarations';
export const EPOCH = 'daemon-epoch';
export const ANONYMOUS = { kind: 'anonymous-bootstrap', owner: null, node: null, actor: null } as const;
export const cleanups: Array<() => Promise<void> | void> = [];
export const SCHEMAS = loadSessionContract().schemas as JsonObject;

export function conforms(schema: string, value: unknown): void {
  expect(validateSchema(SCHEMAS[schema] as JsonObject, value as Json), schema).toEqual([]);
}

export async function runCleanups(): Promise<void> {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
}

export interface Captured {
  readonly method?: string;
  readonly url?: string;
  readonly headers: IncomingMessage['headers'];
  readonly body: string;
}

export function config(endpoint: string, overrides: Partial<PythonHostOptions> = {}): PythonHostOptions {
  return {
    driver: 'daemon',
    principal: PRINCIPAL,
    endpoint,
    upstreamToken: UPSTREAM_TOKEN,
    launchGrant: GRANT,
    launchTtlMs: 60_000,
    sessionTtlMs: 60_000,
    maxSessions: 8,
    ...overrides,
  };
}

export type ListenHandler = (request: IncomingMessage, response: ServerResponse) => void | Promise<void>;

let boundListen: ((handler: ListenHandler) => Promise<string>) | null = null;

export function bindListen(implementation: (handler: ListenHandler) => Promise<string>): void {
  boundListen = implementation;
}

export function listen(handler: ListenHandler): Promise<string> {
  if (boundListen === null) throw new Error('host.test.ts binds the upstream listener before any suite runs');
  return boundListen(handler);
}

export async function bodyOf(request: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString('utf8');
}

export function sendJson(response: ServerResponse, status: number, payload: unknown, headers: Record<string, string> = {}): void {
  const bytes = Buffer.from(JSON.stringify(payload));
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(bytes.length),
    ...headers,
  });
  response.end(bytes);
}

export function ready(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ok: true,
    ready: true,
    transport: 'ready',
    backend: { available: true, owner: PRINCIPAL.owner, nodeId: PRINCIPAL.node, epoch: EPOCH },
    catalog: { fresh: true, catalogDigest: DIGEST, declarationDigest: DECLARATION },
    notReady: [],
    ...overrides,
  };
}

export function catalog(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schemaVersion: 1,
    ok: true,
    httpContractVersion: 1,
    catalogVersion: '1',
    catalogDigest: DIGEST,
    declarationDigest: DECLARATION,
    generatedFrom: 'daemon.catalog',
    backend: { kind: 'daemon', owner: PRINCIPAL.owner, nodeId: PRINCIPAL.node, epoch: EPOCH },
    principal: { kind: 'local-operator', owner: PRINCIPAL.owner, actor: PRINCIPAL.actor },
    methods: [
      { name: 'agent.list', path: '/daemon/request/agent.list', state: 'available', callable: true, mutates: false },
      { name: 'agent.create', path: '/daemon/request/agent.create', state: 'available', callable: true, mutates: true },
      { name: 'message.query', path: '/daemon/request/message.query', state: 'available', callable: true, mutates: false },
    ],
    ...overrides,
  };
}

export function methodSuccess(method: string, requestIdValue = 'upstream-request'): Record<string, unknown> {
  return {
    schemaVersion: 1,
    ok: true,
    method,
    requestId: requestIdValue,
    dispatch: 'dispatched',
    result: { source: 'upstream' },
  };
}

export interface MethodResult {
  readonly status: number;
  readonly payload?: unknown;
  readonly raw?: string;
  readonly headers?: Record<string, string>;
  readonly destroy?: boolean;
}

export interface UpstreamOptions {
  readonly readyPayload?: Record<string, unknown>;
  readonly readyStatus?: number;
  readonly readyRaw?: string;
  readonly readyHeaders?: Record<string, string>;
  readonly catalogHeaders?: Record<string, string>;
  readonly catalogPayload?: Record<string, unknown>;
  readonly catalogStatus?: number;
  readonly method?: (request: IncomingMessage, body: string) => MethodResult | Promise<MethodResult>;
}

export type Mutable<T> = { -readonly [K in keyof T]: T[K] };

export async function upstream(options: Mutable<UpstreamOptions> = {}): Promise<{ endpoint: string; captured: Captured[] }> {
  const captured: Captured[] = [];
  const endpoint = await listen(async (request, response) => {
    const body = await bodyOf(request);
    captured.push({ method: request.method, url: request.url, headers: request.headers, body });
    const path = (request.url ?? '').split('?', 1)[0];
    if (path === '/readyz') {
      if (options.readyRaw !== undefined) {
        response.writeHead(options.readyStatus ?? 200, { 'content-type': 'text/plain' });
        response.end(options.readyRaw);
        return;
      }
      sendJson(response, options.readyStatus ?? 200, options.readyPayload ?? ready(), options.readyHeaders);
      return;
    }
    if (path === '/daemon/request') {
      sendJson(response, options.catalogStatus ?? 200, options.catalogPayload ?? catalog(), {
        'x-hyprial-catalog-digest': String((options.catalogPayload ?? catalog()).catalogDigest ?? DIGEST),
        ...(options.catalogHeaders ?? {}),
      });
      return;
    }
    if (request.url?.startsWith('/daemon/request/')) {
      const name = request.url.slice('/daemon/request/'.length).split('?', 1)[0];
      const result = await options.method?.(request, body) ?? { status: 200, payload: methodSuccess(name) };
      if (result.destroy) {
        request.socket.destroy();
      } else if (result.raw !== undefined) {
        response.writeHead(result.status, { 'content-type': 'application/json', ...(result.headers ?? {}) });
        response.end(result.raw);
      } else {
        sendJson(response, result.status, result.payload ?? null, result.headers);
      }
      return;
    }
    sendJson(response, 404, { code: 'NOT_FOUND' });
  });
  return { endpoint, captured };
}

export async function host(settings: PythonHostOptions): Promise<PythonHost> {
  const running = await startPythonHost(settings);
  cleanups.push(running.close);
  return running;
}

export interface CallOptions {
  readonly method?: string;
  readonly body?: unknown;
  readonly headers?: Record<string, string | string[]>;
  readonly omitOrigin?: boolean;
  readonly origin?: string;
  readonly host?: string;
}

export async function call(origin: string, path: string, options: CallOptions = {}): Promise<{
  status: number;
  headers: IncomingMessage['headers'];
  raw: string;
  json: Record<string, unknown> | null;
}> {
  const target = new URL(path, origin);
  const body = options.body === undefined ? undefined : JSON.stringify(options.body);
  const headers: Record<string, string | string[]> = {
    host: options.host ?? target.host,
    ...(options.omitOrigin ? {} : { origin: options.origin ?? origin }),
    ...(body === undefined ? {} : { 'content-type': 'application/json', 'content-length': String(Buffer.byteLength(body)) }),
    ...(options.headers ?? {}),
  };
  return new Promise((resolve, reject) => {
    const request = httpRequest(target, { method: options.method ?? 'GET', headers }, (response) => {
      const chunks: Buffer[] = [];
      response.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
      response.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let json: Record<string, unknown> | null = null;
        try { json = JSON.parse(raw) as Record<string, unknown>; } catch { json = null; }
        resolve({ status: response.statusCode ?? 0, headers: response.headers, raw, json });
      });
    });
    request.on('error', reject);
    if (body !== undefined) request.write(body);
    request.end();
  });
}

export function cookiesFrom(response: { headers: IncomingMessage['headers'] }): string {
  const values = response.headers['set-cookie'];
  const list = Array.isArray(values) ? values : values === undefined ? [] : [values];
  return list.map((value) => value.split(';', 1)[0]).join('; ');
}

export async function open(origin: string, options: CallOptions = {}): Promise<{
  response: Awaited<ReturnType<typeof call>>;
  cookies: string;
  session: Record<string, unknown>;
}> {
  const response = await call(origin, '/daemon/session', {
    method: 'POST',
    body: { grant: GRANT },
    ...options,
  });
  return {
    response,
    cookies: cookiesFrom(response),
    session: response.json ?? {},
  };
}

export function browserHeaders(session: Record<string, unknown>, cookies: string, csrf = session.csrf): Record<string, string> {
  return {
    cookie: cookies,
    'x-hyprial-view': String(session.view),
    'x-hyprial-generation': String(session.generation),
    'x-hyprial-csrf': String(csrf),
    'x-hyprial-catalog-digest': String((session.catalog as Record<string, unknown>).digest),
  };
}

export interface RawResponse {
  readonly status: number;
  readonly headers: Record<string, string>;
  readonly raw: string;
  readonly json: Record<string, unknown> | null;
}

export async function rawCall(origin: string, requestLine: string, headers: Record<string, string>, body = ''): Promise<RawResponse> {
  const target = new URL(origin);
  const lines = [requestLine + ' HTTP/1.1'];
  for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`);
  if (body) lines.push(`content-length: ${Buffer.byteLength(body)}`);
  lines.push('connection: close', '', body);
  return new Promise((resolve, reject) => {
    const socket = connect(Number(target.port), target.hostname);
    const chunks: Buffer[] = [];
    socket.on('connect', () => socket.write(lines.join('\r\n')));
    socket.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    socket.on('error', reject);
    socket.on('close', () => {
      const text = Buffer.concat(chunks).toString('utf8');
      const split = text.indexOf('\r\n\r\n');
      const head = text.slice(0, split).split('\r\n');
      const status = Number(/^HTTP\/1\.1 (\d+)/.exec(head[0])?.[1] ?? 0);
      const parsed: Record<string, string> = {};
      for (const line of head.slice(1)) {
        const index = line.indexOf(':');
        parsed[line.slice(0, index).toLowerCase()] = line.slice(index + 1).trim();
      }
      let payload = text.slice(split + 4);
      if (parsed['transfer-encoding'] === 'chunked') {
        let rest = payload;
        payload = '';
        for (;;) {
          const eol = rest.indexOf('\r\n');
          const size = Number.parseInt(rest.slice(0, eol), 16);
          if (!size) break;
          payload += rest.slice(eol + 2, eol + 2 + size);
          rest = rest.slice(eol + 2 + size + 2);
        }
      }
      let json: Record<string, unknown> | null = null;
      try { json = JSON.parse(payload) as Record<string, unknown>; } catch { json = null; }
      resolve({ status, headers: parsed, raw: payload, json });
    });
  });
}

export async function mockUpstream(
  methods: Record<string, unknown> = {
    'agent.list': { scenarios: ['success'], mutates: false },
    'agent.create': { scenarios: ['success'], mutates: true },
  },
  overrides: { readonly health?: Record<string, unknown>; readonly contract?: Record<string, unknown> } = {},
): Promise<{ endpoint: string; captured: Captured[] }> {
  const captured: Captured[] = [];
  const endpoint = await listen(async (request, response) => {
    const body = await bodyOf(request);
    captured.push({ method: request.method, url: request.url, headers: request.headers, body });
    if (request.url === '/healthz') return sendJson(response, 200, overrides.health ?? { ok: true });
    if (request.url === '/mock/contract') {
      return sendJson(response, 200, overrides.contract ?? {
        mock: true, contractVersion: '2', contractDigest: 'sha256:mock-contract', methods,
      });
    }
    if (request.url?.startsWith('/daemon/request/')) {
      return sendJson(response, 200, methodSuccess(request.url.slice('/daemon/request/'.length)));
    }
    return sendJson(response, 404, { ok: false });
  });
  return { endpoint, captured };
}

export function maxAge(response: { headers: IncomingMessage['headers'] }, name: string): number {
  const values = response.headers['set-cookie'] ?? [];
  const line = values.find((value) => value.startsWith(`${name}=`)) ?? '';
  return Number(/Max-Age=(\d+)/.exec(line)?.[1] ?? -1);
}

export function cookieOf(cookies: string, name: string): string {
  return cookies.split('; ').find((part) => part.startsWith(`${name}=`)) ?? '';
}
