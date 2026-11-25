/**
 * Decision record (M1 same-origin host, 2026-11-05)
- The launch grant is accepted only from the URL fragment and is removed with
  replaceState before any network request. It is never copied to a query string,
  storage, a header or a log.
- The independent view proof is the only browser value kept across reloads.
  Generation and CSRF rotate on refresh and remain in this module's transport
  generation, so retained adapters cannot silently use a retired session.
- Exactly one HttpTransport and GeneratedClient are constructed for one host
  generation. The browser never selects an endpoint or handles the upstream
  bearer.
- Whether a call needs the CSRF header is decided from the session's pinned
  method list (the host decides from the same list). The generated metadata
  only answers for a name the session does not list, and unknown means write.
- The handshake declares the session contract version it speaks (contract/
  gui-session, absent means 1). A response may carry sessionContract; the
  client ignores it, so an older host and a newer one both connect.
- The developer tool capability is an optional session field,
  `devTools: { apiDocs: boolean }`. It is read only from the host's response:
  an absent or malformed value is `undefined` (no tools), never an error, so
  an older host and a newer one both connect. Nothing in the browser (URL,
  storage, globals, build flags) can set it.
 */

import { HttpTransport } from '../../client';
import { methodMetadata } from '../../client/generated';
import { createBackendConnection } from './connection';
import type { BackendDriver, BackendDescriptor, BackendMethod, BackendTransition, ManagedBackendConnection } from './types';

const VIEW_STORAGE_KEY = 'hyprial.gui.session-view';
const HANDSHAKE_PATH = '/daemon/session';
const STATUS_PATH = '/daemon/session/status';
const SESSION_CONTRACT_HEADER = 'x-hyprial-session-contract';
const SESSION_CONTRACT_VERSION = 1;

function generatedMutates(method: string): boolean {
  const metadata = (methodMetadata as Readonly<Record<string, { readonly mutates: boolean }>>)[method];
  return metadata?.mutates ?? true;
}

function methodMutates(methods: readonly BackendMethod[]): (method: string) => boolean {
  const pinned = new Map(methods.map((method) => [method.name, method.mutates]));
  return (method) => pinned.get(method) ?? generatedMutates(method);
}

export interface VerifiedPrincipal {
  readonly owner: string;
  readonly node: string;
  readonly actor: string;
}

export interface AnonymousBootstrapPrincipalView {
  readonly owner: null;
  readonly node: null;
  readonly actor: null;
}

export interface BrowserSession {
  readonly driver: BackendDriver;
  /** `bootstrap` carries explicit nulls; `ready` carries the verified account. */
  readonly phase: 'bootstrap' | 'ready';
  readonly view: string;
  readonly generation: string;
  readonly csrf: string;
  readonly expiresAt: number;
  readonly devTools?: { readonly apiDocs: boolean };
  readonly principal: VerifiedPrincipal | AnonymousBootstrapPrincipalView;
  readonly catalog: {
    readonly digest: string;
    readonly declarationDigest: string;
    readonly backendEpoch: string;
    readonly httpContractVersion: number;
    readonly catalogVersion: string;
    readonly methodCount: number;
    readonly methods: readonly BackendMethod[];
  };
}

export interface BrowserBackendBootstrap {
  readonly connection: ManagedBackendConnection;
  readonly session: BrowserSession;
  readonly subscribeTransition: (listener: (reason: BackendTransition) => void) => () => void;
}

export interface BrowserSessionOptions {
  readonly fetch?: typeof fetch;
  readonly storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
  readonly location?: Pick<Location, 'hash' | 'pathname' | 'search' | 'origin'>;
  readonly history?: Pick<History, 'replaceState'>;
}

export class BrowserBootstrapError extends Error {
  readonly code: string;
  readonly status: number | null;

  constructor(code: string, message: string, status: number | null = null) {
    super(message);
    this.name = 'BrowserBootstrapError';
    this.code = code;
    this.status = status;
  }
}

type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonRecord
    : null;
}

function nonempty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function takeLaunchGrant(
  location: Pick<Location, 'hash' | 'pathname' | 'search'>,
  history: Pick<History, 'replaceState'>,
): string | null {
  const raw = location.hash.startsWith('#') ? location.hash.slice(1) : location.hash;
  if (!raw) return null;
  const params = new URLSearchParams(raw);
  const grants = params.getAll('grant');
  if (grants.length === 0) return null;
  params.delete('grant');
  const remainder = params.toString();
  history.replaceState(null, '', `${location.pathname}${location.search}${remainder ? `#${remainder}` : ''}`);
  if (grants.length !== 1 || !nonempty(grants[0])) {
    throw new BrowserBootstrapError('LAUNCH_GRANT_INVALID', 'the launch fragment must carry one non-empty grant');
  }
  return grants[0];
}

/**
 * The principal must match the phase exactly: the anonymous bootstrap carries
 * explicit `null`s and never an empty string or a synthesised operator, and a
 * ready session never degrades to `null`.
 */
function principalMatchesPhase(phase: unknown, principal: JsonRecord): boolean {
  if (phase === 'bootstrap') {
    return principal.owner === null && principal.node === null && principal.actor === null;
  }
  return nonempty(principal.owner) && nonempty(principal.node) && nonempty(principal.actor);
}

function parseDevTools(value: unknown): BrowserSession['devTools'] {
  const capability = record(value);
  return capability && typeof capability.apiDocs === 'boolean'
    ? Object.freeze({ apiDocs: capability.apiDocs })
    : undefined;
}

function parseSession(value: JsonRecord): BrowserSession {
  const principal = record(value.principal);
  const catalog = record(value.catalog);
  const rawMethods = catalog ? catalog.methods : null;
  if (
    value.schemaVersion !== 1
    || value.ok !== true
    || !nonempty(value.requestId)
    || (value.driver !== 'daemon' && value.driver !== 'contract-mock')
    || (value.phase !== 'ready' && value.phase !== 'bootstrap')
    || !nonempty(value.view)
    || !nonempty(value.generation)
    || !nonempty(value.csrf)
    || typeof value.expiresAt !== 'number'
    || !Number.isSafeInteger(value.expiresAt)
    || !principal
    || !principalMatchesPhase(value.phase, principal)
    || !catalog || !nonempty(catalog.digest) || !nonempty(catalog.declarationDigest)
    || !nonempty(catalog.backendEpoch) || typeof catalog.httpContractVersion !== 'number'
    || !nonempty(catalog.catalogVersion) || typeof catalog.methodCount !== 'number'
    || !Array.isArray(rawMethods) || rawMethods.length !== catalog.methodCount
  ) {
    throw new BrowserBootstrapError('BOOTSTRAP_INVALID_RESPONSE', 'the host returned an incomplete session');
  }
  const seenMethods = new Set<string>();
  const methods: BackendMethod[] = rawMethods.map((entry) => {
    const method = record(entry);
    if (!method || !nonempty(method.name) || typeof method.callable !== 'boolean'
      || !nonempty(method.state) || typeof method.mutates !== 'boolean'
      || seenMethods.has(method.name)) {
      throw new BrowserBootstrapError('BOOTSTRAP_INVALID_RESPONSE', 'the host returned invalid catalog method metadata');
    }
    seenMethods.add(method.name);
    return Object.freeze({
      name: method.name,
      callable: method.callable,
      state: method.state,
      mutates: method.mutates,
    });
  });
  const devTools = parseDevTools(value.devTools);
  return Object.freeze({
    driver: value.driver,
    phase: value.phase as BrowserSession['phase'],
    view: value.view,
    generation: value.generation,
    csrf: value.csrf,
    expiresAt: value.expiresAt,
    ...(devTools ? { devTools } : {}),
    principal: Object.freeze(
      value.phase === 'bootstrap'
        ? { owner: null, node: null, actor: null }
        : { owner: principal.owner as string, node: principal.node as string, actor: principal.actor as string },
    ),
    catalog: Object.freeze({
      digest: catalog.digest,
      declarationDigest: catalog.declarationDigest,
      backendEpoch: catalog.backendEpoch,
      httpContractVersion: catalog.httpContractVersion,
      catalogVersion: catalog.catalogVersion,
      methodCount: catalog.methodCount,
      methods: Object.freeze(methods),
    }),
  });
}

function clientError(value: unknown, status: number): BrowserBootstrapError {
  const body = record(value);
  const code = body && nonempty(body.code) ? body.code : 'BOOTSTRAP_REFUSED';
  const message = body && nonempty(body.message) ? body.message : `the host refused session establishment (${status})`;
  return new BrowserBootstrapError(code, message, status);
}

export function requestSessionStatus(
  session: Pick<BrowserSession, 'view' | 'generation'>,
  options: BrowserSessionOptions = {},
): Promise<Response> {
  return (options.fetch ?? globalThis.fetch)(
    new URL(STATUS_PATH, (options.location ?? window.location).origin), {
      method: 'POST',
      credentials: 'include',
      redirect: 'error',
      cache: 'no-store',
      headers: { 'content-type': 'application/json', 'x-hyprial-view': session.view, 'x-hyprial-generation': session.generation },
      body: '{}',
      signal: AbortSignal.timeout(10_000),
    },
  );
}

export async function connectBrowserBackend(options: BrowserSessionOptions = {}): Promise<BrowserBackendBootstrap> {
  const location = options.location ?? window.location;
  const history = options.history ?? window.history;
  const storage = options.storage ?? window.sessionStorage;
  const fetcher = options.fetch ?? globalThis.fetch;
  if (typeof fetcher !== 'function') {
    throw new BrowserBootstrapError('BOOTSTRAP_UNAVAILABLE', 'the browser has no fetch implementation');
  }
  const grant = takeLaunchGrant(location, history);
  const storedView = storage.getItem(VIEW_STORAGE_KEY);
  if (grant === null && !nonempty(storedView)) {
    throw new BrowserBootstrapError('SESSION_REQUIRED', 'open the GUI from its trusted launcher to establish a session');
  }
  const headers = new Headers({
    accept: 'application/json',
    'content-type': 'application/json',
    [SESSION_CONTRACT_HEADER]: String(SESSION_CONTRACT_VERSION),
  });
  if (grant === null && storedView) headers.set('x-hyprial-view', storedView);
  let response: Response;
  try {
    response = await fetcher(new URL(HANDSHAKE_PATH, location.origin), {
      method: 'POST',
      headers,
      body: JSON.stringify(grant === null ? {} : { grant }),
      credentials: 'include',
      redirect: 'error',
      cache: 'no-store',
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new BrowserBootstrapError('BOOTSTRAP_UNAVAILABLE', 'the same-origin host could not be reached');
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new BrowserBootstrapError('BOOTSTRAP_INVALID_RESPONSE', 'the host returned an unreadable session response', response.status);
  }
  if (!response.ok) {
    const error = clientError(body, response.status);
    if (grant === null && (response.status === 401 || response.status === 403)) storage.removeItem(VIEW_STORAGE_KEY);
    throw error;
  }
  const payload = record(body);
  if (!payload) throw new BrowserBootstrapError('BOOTSTRAP_INVALID_RESPONSE', 'the host returned an invalid session');
  const session = parseSession(payload);
  if (grant === null && session.view !== storedView) {
    storage.removeItem(VIEW_STORAGE_KEY);
    throw new BrowserBootstrapError('SESSION_VIEW_MISMATCH', 'the host refreshed a different view');
  }
  try {
    storage.setItem(VIEW_STORAGE_KEY, session.view);
  } catch {
    throw new BrowserBootstrapError('BOOTSTRAP_STORAGE_UNAVAILABLE', 'the browser cannot retain the per-view session proof');
  }
  const transport = new HttpTransport({
    baseUrl: location.origin,
    auth: {
      mode: 'same-origin-session',
      view: () => session.view,
      generation: () => session.generation,
      csrf: () => session.csrf,
      mutates: methodMutates(session.catalog.methods),
    },
    fetch: fetcher,
    expectedCatalogDigest: session.catalog.digest,
  });
  const descriptor: BackendDescriptor = {
    interfaceVersion: 1,
    driver: session.driver,
    phase: session.phase,
    generation: session.generation,
    catalogDigest: session.catalog.digest,
    methods: session.catalog.methods,
    // The authenticated same-origin host catalog is the verification source.
    // Keep the mutating subset explicit so the common connection fails closed
    // if a future bootstrap path omits that admission decision.
    verifiedWrites: session.catalog.methods
      .filter((method) => method.mutates)
      .map((method) => method.name),
    ...(session.phase === 'ready'
      ? {
        principalActor: session.principal.actor as string,
        principalNode: session.principal.node as string,
      }
      : {}),
  };
  const listeners = new Set<(reason: BackendTransition) => void>();
  return Object.freeze({
    connection: createBackendConnection(transport, descriptor, {
      ...(session.driver === 'daemon' ? {
        beginTransition: (reason: BackendTransition) => { for (const listener of listeners) listener(reason); },
      } : {}),
    }),
    session,
    subscribeTransition: (listener: (reason: BackendTransition) => void) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  });
}
