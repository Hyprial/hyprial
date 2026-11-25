/**
 * Contract Mock in-browser form: a `fetch`-shaped function over the core.
 *
 * Decision record (1e-2, 2026-11-15)
 * - This form stands in for the whole same-origin host plus the Node mock for
 *   front-end development without a backend. It answers the host's browser
 *   surface (`POST /daemon/session`, `POST /daemon/session/status`,
 *   `POST /daemon/session/logout`, `GET /daemon/readyz`, `GET /daemon/request`
 *   and `POST /daemon/request/<method>`) and nothing else. Every business
 *   answer comes from `ContractMock.handle`; this file only frames it.
 * - It is a double of the host, so it must not drift from it: the conformance
 *   test drives the real host in front of the Node form and compares status,
 *   envelope and catalog-digest header with this function row by row.
 * - A `Response` cannot carry cookies the page could use, so the session is
 *   the (view, generation) pair the client already sends as headers; the host's
 *   cookie checks have no counterpart here. Time and randomness are injectable
 *   so tests are deterministic.
 * - A launch grant is accepted when it is the only key of a non-empty string
 *   and was not used before by this function; there is no upstream bearer, so
 *   no credential exists to leak into the page.
 * - Host refusals use the host's envelope, codes and messages and carry the
 *   method name on method routes, as the host does.
 */

import type { ContractMock, MockResponse } from './respond.ts';

export const MOCK_ORIGIN = 'http://contract-mock.invalid';

export interface ContractMockPrincipal {
  owner: string;
  node: string;
  actor: string;
}

export interface ContractMockFetchOptions {
  origin?: string;
  now?: () => number;
  principal?: ContractMockPrincipal;
  sessionTtlMs?: number;
  random?: () => string;
}

interface Refusal {
  status: number;
  code: string;
  message: string;
  method?: string;
}

interface BrowserSessionState {
  view: string;
  generation: string;
  csrf: string;
  expiresAt: number;
  controlExpiresAt: number;
}

const DEFAULT_PRINCIPAL: ContractMockPrincipal = { owner: 'owner', node: 'node', actor: 'user:owner' };
const DEFAULT_TTL_MS = 12 * 60 * 60_000;
const MAX_SESSION_BYTES = 8_192;
const MAX_REQUEST_BYTES = 131_072;
const METHOD_NAME = /^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$/;
const REQUEST_PREFIX = '/daemon/request';

function randomToken(): string {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(24));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function sameText(left: string | null, right: string | null): boolean {
  return left !== null && right !== null && left === right;
}

function respond(status: number, payload: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
      ...extra,
    },
  });
}

function refusal(id: string, error: Refusal): Response {
  return respond(
    error.status,
    {
      schemaVersion: 1,
      ok: false,
      requestId: id,
      code: error.code,
      message: error.message,
      dispatch: 'not-dispatched',
      ...(error.method === undefined ? {} : { method: error.method }),
    },
    { 'x-hyprial-request-id': id },
  );
}

class Refused extends Error {
  readonly detail: Refusal;

  constructor(detail: Refusal) {
    super(detail.message);
    this.detail = detail;
  }
}

function refuse(status: number, code: string, message: string, method?: string): Refused {
  return new Refused({ status, code, message, method });
}

async function readText(input: RequestInfo | URL, init: RequestInit | undefined): Promise<string> {
  const body = init?.body;
  if (typeof body === 'string') return body;
  if (body instanceof Uint8Array) return new TextDecoder().decode(body);
  if (body instanceof ArrayBuffer) return new TextDecoder().decode(body);
  if (body === undefined || body === null) {
    return typeof Request !== 'undefined' && input instanceof Request ? input.text() : '';
  }
  throw refuse(400, 'INVALID_REQUEST', 'request body is not valid JSON');
}

async function readObject(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  headers: Headers,
  limit: number,
  method?: string,
): Promise<Record<string, unknown>> {
  const type = headers.get('content-type');
  if (type === null || type.split(';', 1)[0].trim().toLowerCase() !== 'application/json') {
    throw refuse(415, 'HTTP_UNSUPPORTED_MEDIA_TYPE', 'Content-Type must be application/json', method);
  }
  const text = await readText(input, init);
  if (new TextEncoder().encode(text).length > limit) {
    throw refuse(413, 'HTTP_REQUEST_TOO_LARGE', 'request body is too large', method);
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw refuse(400, 'INVALID_REQUEST', 'request body is not valid JSON', method);
  }
  if (!isObject(value)) throw refuse(400, 'INVALID_REQUEST', 'request body must be a JSON object', method);
  return value;
}

function targetOf(input: RequestInfo | URL): URL {
  if (input instanceof URL) return input;
  if (typeof input === 'string') return new URL(input);
  return new URL(input.url);
}

export function createContractMockFetch(
  mock: ContractMock,
  options: ContractMockFetchOptions = {},
): (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> {
  const origin = options.origin ?? MOCK_ORIGIN;
  const now = options.now ?? Date.now;
  const principal = options.principal ?? DEFAULT_PRINCIPAL;
  const ttl = options.sessionTtlMs ?? DEFAULT_TTL_MS;
  const token = options.random ?? randomToken;
  const table = mock.table;
  const names = Object.keys(table.methods);
  const sessions = new Map<string, BrowserSessionState>();
  const grants = new Set<string>();

  const methodList = () =>
    names.map((name) => ({
      name,
      callable: true,
      state: 'available',
      mutates: table.methods[name].mutates,
    }));
  const principalView = { kind: 'serving', owner: principal.owner, node: principal.node, actor: principal.actor };

  const ok = (id: string, fields: Record<string, unknown>, extra: Record<string, string> = {}) =>
    respond(200, { schemaVersion: 1, ok: true, requestId: id, ...fields }, { 'x-hyprial-request-id': id, ...extra });

  const sessionBody = (id: string, state: BrowserSessionState) => ({
    driver: 'contract-mock',
    phase: 'ready',
    view: state.view,
    generation: state.generation,
    csrf: state.csrf,
    expiresAt: state.expiresAt,
    principal: principalView,
    catalog: {
      digest: table.digest,
      declarationDigest: table.digest,
      backendEpoch: `contract-mock:${table.digest}`,
      httpContractVersion: 1,
      catalogVersion: table.contractVersion || '1',
      methodCount: names.length,
      methods: methodList(),
    },
    requestId: id,
  });

  const live = (view: string | null): BrowserSessionState | null => {
    if (view === null) return null;
    const state = sessions.get(view);
    if (state === undefined) return null;
    if (state.controlExpiresAt <= now()) {
      sessions.delete(view);
      return null;
    }
    return state;
  };

  const authenticate = (headers: Headers): BrowserSessionState => {
    const state = live(headers.get('x-hyprial-view'));
    if (state === null || state.expiresAt <= now()) {
      throw refuse(401, 'SESSION_REQUIRED', 'a live business session and view proof are required');
    }
    if (!sameText(state.generation, headers.get('x-hyprial-generation'))) {
      throw refuse(403, 'GENERATION_REJECTED', 'the request generation is no longer current');
    }
    return state;
  };

  const requirePin = (headers: Headers, method?: string) => {
    const candidate = headers.get('x-hyprial-catalog-digest');
    if (candidate !== null && candidate !== table.digest) {
      throw refuse(409, 'CATALOG_MISMATCH', 'the browser catalog digest differs from the session pin', method);
    }
  };

  const mint = (id: string, view: string, controlExpiresAt: number): Response => {
    const at = now();
    const state: BrowserSessionState = {
      view,
      generation: token(),
      csrf: token(),
      expiresAt: Math.min(at + ttl, controlExpiresAt),
      controlExpiresAt,
    };
    sessions.set(view, state);
    return ok(id, sessionBody(id, state));
  };

  const openSession = async (
    id: string,
    command: string,
    input: RequestInfo | URL,
    init: RequestInit | undefined,
    headers: Headers,
  ): Promise<Response> => {
    if (command !== 'POST') throw refuse(405, 'HTTP_METHOD_NOT_ALLOWED', 'session establishment requires POST');
    const body = await readObject(input, init, headers, MAX_SESSION_BYTES);
    const keys = Object.keys(body);
    if (Object.hasOwn(body, 'grant')) {
      const grant = body.grant;
      if (keys.length !== 1 || typeof grant !== 'string' || grant.length === 0) {
        throw refuse(400, 'INVALID_REQUEST', 'an initial session body is exactly {"grant":"<one-time grant>"}');
      }
      if (grants.has(grant)) {
        throw refuse(409, 'LAUNCH_GRANT_USED', 'the one-time launch grant was already consumed');
      }
      grants.add(grant);
      return mint(id, token(), now() + ttl);
    }
    if (keys.length !== 0) throw refuse(400, 'INVALID_REQUEST', 'a refresh body must be an empty JSON object');
    const view = headers.get('x-hyprial-view');
    const control = live(view);
    if (view === null || control === null) {
      throw refuse(401, 'SESSION_REQUIRED', 'refresh requires the control cookie and view proof');
    }
    return mint(id, view, control.controlExpiresAt);
  };

  const callMethod = async (
    method: string,
    command: string,
    input: RequestInfo | URL,
    init: RequestInit | undefined,
    headers: Headers,
  ): Promise<Response> => {
    if (command !== 'POST') throw refuse(405, 'HTTP_METHOD_NOT_ALLOWED', 'daemon methods require POST', method);
    const state = authenticate(headers);
    const entry = table.methods[method];
    if (entry === undefined) {
      throw refuse(404, 'METHOD_NOT_FOUND', 'the method is not present in the pinned catalog', method);
    }
    requirePin(headers, method);
    if (entry.mutates && !sameText(state.csrf, headers.get('x-hyprial-csrf'))) {
      throw refuse(403, 'CSRF_REJECTED', 'the mutation CSRF value is missing or no longer current', method);
    }
    const params = await readObject(input, init, headers, MAX_REQUEST_BYTES, method);
    authenticate(headers);
    const reply: MockResponse = mock.handle(method, params);
    const headersOut: Record<string, string> = { 'x-hyprial-catalog-digest': table.digest };
    const requestId = reply.payload.requestId;
    if (typeof requestId === 'string') headersOut['x-hyprial-request-id'] = requestId;
    if (typeof reply.payload.dispatch === 'string') headersOut['x-hyprial-dispatch'] = reply.payload.dispatch;
    return respond(reply.status, reply.payload, headersOut);
  };

  const route = async (input: RequestInfo | URL, init: RequestInit | undefined, id: string): Promise<Response> => {
    const target = targetOf(input);
    if (target.origin !== origin) throw new TypeError('Failed to fetch');
    const headers = new Headers(
      init?.headers ?? (typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined),
    );
    const command = (init?.method ?? (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET')).toUpperCase();
    const path = target.pathname;
    if (!path.startsWith('/daemon/')) throw refuse(404, 'METHOD_NOT_FOUND', 'unknown daemon request path');
    if (path === '/daemon/session') return openSession(id, command, input, init, headers);
    if (path === '/daemon/session/status') {
      if (command !== 'POST') throw refuse(405, 'HTTP_METHOD_NOT_ALLOWED', 'session status requires POST');
      const body = await readObject(input, init, headers, MAX_SESSION_BYTES);
      if (Object.keys(body).length !== 0) throw refuse(400, 'INVALID_REQUEST', 'session status takes no authority fields');
      const state = live(headers.get('x-hyprial-view'));
      const current = state !== null && state.expiresAt > now() && sameText(state.generation, headers.get('x-hyprial-generation'));
      return ok(id, { current });
    }
    if (path === '/daemon/session/logout') {
      if (command !== 'POST') throw refuse(405, 'HTTP_METHOD_NOT_ALLOWED', 'logout requires POST');
      await readObject(input, init, headers, MAX_SESSION_BYTES);
      const state = authenticate(headers);
      if (!sameText(state.csrf, headers.get('x-hyprial-csrf'))) {
        throw refuse(403, 'CSRF_REJECTED', 'the mutation CSRF value is missing or no longer current');
      }
      sessions.delete(state.view);
      return ok(id, { revoked: true });
    }
    if (path === '/daemon/readyz') {
      if (command !== 'GET') throw refuse(405, 'HTTP_METHOD_NOT_ALLOWED', 'readyz requires GET');
      authenticate(headers);
      requirePin(headers);
      return ok(
        id,
        {
          ready: true,
          transport: 'ready',
          driver: 'contract-mock',
          backend: { available: true, epoch: `contract-mock:${table.digest}` },
          catalog: { fresh: true, catalogDigest: table.digest, declarationDigest: table.digest },
        },
        { 'x-hyprial-catalog-digest': table.digest },
      );
    }
    if (path === REQUEST_PREFIX) {
      if (command !== 'GET') throw refuse(405, 'HTTP_METHOD_NOT_ALLOWED', 'catalog requires GET');
      authenticate(headers);
      requirePin(headers);
      return respond(
        200,
        {
          schemaVersion: 1,
          ok: true,
          requestId: id,
          httpContractVersion: 1,
          catalogVersion: table.contractVersion || '1',
          catalogDigest: table.digest,
          declarationDigest: table.digest,
          generatedFrom: 'contract-mock',
          backend: { kind: 'contract-mock', epoch: `contract-mock:${table.digest}` },
          principal: principalView,
          methods: names.map((name) => ({
            name,
            path: `${REQUEST_PREFIX}/${name}`,
            state: 'available',
            callable: true,
            mutates: table.methods[name].mutates,
          })),
        },
        { 'x-hyprial-request-id': id, 'x-hyprial-catalog-digest': table.digest },
      );
    }
    if (path.startsWith(`${REQUEST_PREFIX}/`)) {
      const method = path.slice(REQUEST_PREFIX.length + 1);
      if (method.includes('/') || !METHOD_NAME.test(method)) {
        throw refuse(404, 'METHOD_NOT_FOUND', 'unknown daemon request path', method || undefined);
      }
      if (target.search !== '') throw refuse(400, 'INVALID_REQUEST', 'query and fragment are not accepted', method);
      try {
        return await callMethod(method, command, input, init, headers);
      } catch (error) {
        if (error instanceof Refused && error.detail.method === undefined) {
          throw new Refused({ ...error.detail, method });
        }
        throw error;
      }
    }
    throw refuse(404, 'METHOD_NOT_FOUND', 'unknown daemon request path');
  };

  return async (input, init) => {
    if (init?.signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
    const id = `gui-${token()}`;
    try {
      return await route(input, init, id);
    } catch (error) {
      if (error instanceof Refused) return refusal(id, error.detail);
      throw error;
    }
  };
}
