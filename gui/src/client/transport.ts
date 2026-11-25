import {
  CatalogDriftError,
  DomainError,
  EnvelopeError,
  HttpError,
  TransportError,
} from './errors';
import type {
  CatalogMethod,
  CatalogSnapshot,
  DispatchState,
  HttpTransportAuth,
  HttpTransportConfig,
  InvocationResult,
  RequestDiagnostics,
  RequestOptions,
} from './types';

const ENVELOPE_SCHEMA_VERSION = 1;
const DEFAULT_REQUEST_BUDGET_MS = 15_000;
const METHOD_PATTERN = /^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$/;
const DISPATCH_STATES: ReadonlySet<string> = new Set(['not-dispatched', 'dispatched', 'unknown']);
const TRANSPORT_HTTP_CODES: ReadonlySet<number> = new Set([401, 403, 404, 405, 413, 415, 429, 431]);

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isDispatchState(value: unknown): value is DispatchState {
  return typeof value === 'string' && DISPATCH_STATES.has(value);
}

function abortReason(signal: AbortSignal | undefined, timedOut: boolean): 'timeout' | 'aborted' | 'network' {
  if (timedOut) return 'timeout';
  if (signal?.aborted) return 'aborted';
  return 'network';
}

function isAbortError(error: unknown): boolean {
  return isRecord(error) && (error.name === 'AbortError' || error.name === 'TimeoutError');
}

/**
 * HTTP transport for the canonical `POST /daemon/request/<method>` surface.
 *
 * One invocation performs at most one network attempt. Writes are never retried
 * automatically: an aborted or failed call after dispatch reports
 * `dispatch: "unknown"` and does not claim a rollback or a definite non-send.
 */
export class HttpTransport {
  readonly #config: HttpTransportConfig;
  readonly #baseUrl: string;
  readonly #auth: HttpTransportAuth;
  #lastDiagnostics: RequestDiagnostics | null = null;

  constructor(config: HttpTransportConfig) {
    const baseUrl = config.baseUrl.replace(/\/+$/, '');
    if (!/^https?:\/\//.test(baseUrl)) {
      throw new TypeError('baseUrl must be an absolute http(s) origin');
    }
    this.#config = config;
    this.#baseUrl = baseUrl;
    if (config.auth !== undefined && config.token !== undefined) {
      throw new TypeError('choose either auth or token, not both');
    }
    if (config.auth !== undefined) {
      this.#auth = config.auth;
    } else if (config.token !== undefined) {
      this.#auth = { mode: 'bearer', token: config.token };
    } else {
      throw new TypeError('an auth mode or bearer token is required');
    }
  }

  /** Diagnostics of the most recent invocation (success or failure). */
  get lastDiagnostics(): RequestDiagnostics | null {
    return this.#lastDiagnostics;
  }

  async invoke<TResult>(
    method: string,
    params: object = {},
    options: RequestOptions = {},
  ): Promise<TResult> {
    return (await this.invokeDetailed<TResult>(method, params, options)).result;
  }

  async invokeDetailed<TResult>(
    method: string,
    params: object = {},
    options: RequestOptions = {},
  ): Promise<InvocationResult<TResult>> {
    const startedAt = Date.now();
    const startedPerf = performance.now();
    const diagnostics = (
      extra: Partial<RequestDiagnostics> = {},
    ): RequestDiagnostics => ({
      method,
      requestId: null,
      status: null,
      dispatch: 'not-dispatched',
      catalogDigest: null,
      attempts: 0,
      startedAt,
      durationMs: Math.round(performance.now() - startedPerf),
      ...extra,
    });

    if (typeof method !== 'string' || !METHOD_PATTERN.test(method)) {
      throw this.#remember(
        new TransportError(
          'configuration',
          'INVALID_METHOD',
          `refusing a non-canonical method name: ${String(method)}`,
          diagnostics(),
        ),
      );
    }
    if (options.signal?.aborted) {
      throw this.#remember(
        new TransportError('aborted', 'REQUEST_ABORTED', 'caller aborted before dispatch', diagnostics()),
      );
    }
    let budgetMs: number;
    try {
      budgetMs = this.#budget(options.budgetMs);
    } catch (error) {
      throw this.#remember(
        new TransportError(
          'configuration',
          'INVALID_BUDGET',
          error instanceof Error ? error.message : 'invalid request budget',
          diagnostics(),
        ),
      );
    }
    const expectedDigest = options.catalogDigest ?? this.#config.expectedCatalogDigest;

    const controller = new AbortController();
    let timedOut = false;
    const caller = options.signal;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, budgetMs);
    const onCallerAbort = () => controller.abort();
    if (caller) {
      if (caller.aborted) controller.abort();
      else caller.addEventListener('abort', onCallerAbort, { once: true });
    }

    let response: Response;
    let text: string;
    try {
      let authHeaders: Readonly<Record<string, string>>;
      let body: string;
      let fetchImpl: typeof fetch;
      try {
        const includeCsrf = this.#auth.mode === 'same-origin-session' && this.#auth.mutates(method);
        authHeaders = await this.#untilAborted(this.#resolveAuthHeaders(includeCsrf), controller.signal, () =>
          this.#beforeDispatchFailure(caller, timedOut, budgetMs, diagnostics()),
        );
        fetchImpl = this.#resolveFetch();
        body = JSON.stringify(params ?? {});
        if (typeof body !== 'string') throw new Error('request parameters are not JSON-serializable');
        if (controller.signal.aborted) {
          throw this.#beforeDispatchFailure(caller, timedOut, budgetMs, diagnostics());
        }
      } catch (error) {
        if (error instanceof TransportError) throw this.#remember(error);
        throw this.#remember(
          new TransportError(
            'configuration',
            'CLIENT_CONFIGURATION',
            error instanceof Error ? error.message : 'client configuration failed',
            diagnostics(),
          ),
        );
      }

      const headers = new Headers();
      headers.set('accept', 'application/json');
      headers.set('content-type', 'application/json');
      for (const [name, value] of Object.entries(options.headers ?? {})) {
        headers.set(name, value);
      }
      // Transport-owned headers always win over caller-supplied values.
      headers.delete('authorization');
      headers.delete('x-hyprial-view');
      headers.delete('x-hyprial-generation');
      headers.delete('x-hyprial-csrf');
      headers.delete('x-hyprial-catalog-digest');
      for (const [name, value] of Object.entries(authHeaders)) headers.set(name, value);
      if (expectedDigest) headers.set('x-hyprial-catalog-digest', expectedDigest);

      try {
        response = await fetchImpl(`${this.#baseUrl}/daemon/request/${method}`, {
          method: 'POST',
          headers,
          body,
          signal: controller.signal,
          credentials: this.#credentials(),
          redirect: 'error',
          cache: 'no-store',
        });
        text = await response.text();
      } catch (error) {
        const kind = isAbortError(error) ? abortReason(caller, timedOut) : 'network';
        const code = kind === 'timeout' ? 'REQUEST_TIMEOUT' : kind === 'aborted' ? 'REQUEST_ABORTED' : 'NETWORK_ERROR';
        const message =
          kind === 'timeout'
            ? `request exceeded its ${budgetMs}ms budget after dispatch`
            : kind === 'aborted'
              ? 'caller aborted after the request was sent'
              : error instanceof Error
                ? error.message
                : 'network failure';
        throw this.#remember(
          new TransportError(kind, code, message, diagnostics({ dispatch: 'unknown', attempts: 1 })),
        );
      }
    } finally {
      clearTimeout(timer);
      if (caller) caller.removeEventListener('abort', onCallerAbort);
    }

    const actualDigest = response.headers.get('x-hyprial-catalog-digest');
    const requestIdHeader = response.headers.get('x-hyprial-request-id');
    const status = response.status;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      if (status === 401 || status === 403) {
        throw this.#remember(
          new HttpError(
            status === 401 ? 'HTTP_UNAUTHORIZED' : 'HTTP_FORBIDDEN',
            'the request entry refused the bearer credential',
            status,
            diagnostics({ status, dispatch: 'unknown', attempts: 1, catalogDigest: actualDigest }),
          ),
        );
      }
      throw this.#remember(
        new EnvelopeError(
          'ENVELOPE_NOT_JSON',
          'response body is not JSON; dispatch cannot be determined',
          diagnostics({ status, dispatch: 'unknown', attempts: 1, catalogDigest: actualDigest }),
        ),
      );
    }

    if (!isRecord(parsed)) {
      throw new EnvelopeError(
        'ENVELOPE_INVALID',
        'response is not a transport envelope object',
        diagnostics({ status, dispatch: 'unknown', attempts: 1, catalogDigest: actualDigest }),
      );
    }
    if (parsed.schemaVersion !== ENVELOPE_SCHEMA_VERSION) {
      throw this.#remember(
        new EnvelopeError(
          'ENVELOPE_SCHEMA_VERSION',
          `unsupported envelope schemaVersion ${String(parsed.schemaVersion)}`,
          diagnostics({ status, dispatch: 'unknown', attempts: 1, catalogDigest: actualDigest }),
        ),
      );
    }

    const reportedRequestId = typeof parsed.requestId === 'string' && parsed.requestId.length > 0
      ? parsed.requestId
      : requestIdHeader;
    const reportedDispatch = isDispatchState(parsed.dispatch) ? parsed.dispatch : 'unknown';
    const base = diagnostics({
      requestId: reportedRequestId,
      status,
      dispatch: reportedDispatch,
      attempts: 1,
      catalogDigest: actualDigest,
    });
    // A malformed envelope is not a trustworthy dispatch receipt.
    const envelopeFault = { ...base, dispatch: 'unknown' as const };

    if (typeof parsed.ok !== 'boolean') {
      throw this.#remember(new EnvelopeError('ENVELOPE_INVALID', 'envelope has no boolean ok', envelopeFault));
    }

    if (parsed.ok === false) {
      if (parsed.method !== undefined && parsed.method !== null) {
        if (typeof parsed.method !== 'string' || parsed.method !== method) {
          throw this.#remember(
            new EnvelopeError(
              'ENVELOPE_METHOD_MISMATCH',
              `error envelope method ${String(parsed.method)} does not match ${method}`,
              envelopeFault,
            ),
          );
        }
      }
      const code = typeof parsed.code === 'string' && parsed.code.length > 0 ? parsed.code : null;
      const message = typeof parsed.message === 'string' ? parsed.message : 'request failed';
      if (code === null || !isDispatchState(parsed.dispatch) || base.requestId === null) {
        throw this.#remember(
          new EnvelopeError('ENVELOPE_INVALID_ERROR', 'error envelope is incomplete', envelopeFault),
        );
      }
      if (expectedDigest && actualDigest !== null && actualDigest !== expectedDigest) {
        throw this.#remember(new CatalogDriftError(expectedDigest, actualDigest, base));
      }
      if (expectedDigest && code === 'CATALOG_MISMATCH') {
        throw this.#remember(new CatalogDriftError(expectedDigest, actualDigest, base));
      }
      const errorDiagnostics = { ...base, dispatch: reportedDispatch };
      if (status === 408) {
        throw this.#remember(new TransportError('timeout', code, message, errorDiagnostics));
      }
      if (status === 401 || (TRANSPORT_HTTP_CODES.has(status) && reportedDispatch === 'not-dispatched')) {
        throw this.#remember(new HttpError(code, message, status, errorDiagnostics));
      }
      throw this.#remember(new DomainError(code, message, parsed.data, errorDiagnostics));
    }

    if (parsed.method !== method) {
      throw this.#remember(
        new EnvelopeError(
          'ENVELOPE_METHOD_MISMATCH',
          `envelope method ${String(parsed.method)} does not match ${method}`,
          envelopeFault,
        ),
      );
    }
    if (!isDispatchState(parsed.dispatch) || base.requestId === null) {
      throw this.#remember(
        new EnvelopeError('ENVELOPE_INVALID_SUCCESS', 'success envelope is incomplete', envelopeFault),
      );
    }
    if (parsed.dispatch !== 'dispatched') {
      throw this.#remember(
        new EnvelopeError(
          'ENVELOPE_DISPATCH_MISMATCH',
          `success envelope reported dispatch ${parsed.dispatch}`,
          envelopeFault,
        ),
      );
    }
    if (status < 200 || status > 299) {
      throw this.#remember(
        new EnvelopeError(
          'ENVELOPE_STATUS_MISMATCH',
          `success envelope carried HTTP status ${status}`,
          envelopeFault,
        ),
      );
    }
    if (!Object.prototype.hasOwnProperty.call(parsed, 'result')) {
      throw this.#remember(new EnvelopeError('ENVELOPE_MISSING_RESULT', 'success envelope has no result', envelopeFault));
    }
    if (expectedDigest && actualDigest !== expectedDigest) {
      throw this.#remember(new CatalogDriftError(expectedDigest, actualDigest, base));
    }
    this.#lastDiagnostics = base;
    return { result: parsed.result as TResult, diagnostics: base };
  }

  /** Fetch the served catalog snapshot through the same auth and budget boundary. */
  async catalog(options: RequestOptions = {}): Promise<CatalogSnapshot> {
    const startedAt = Date.now();
    const startedPerf = performance.now();
    const catalogDiagnostics = (extra: Partial<RequestDiagnostics> = {}): RequestDiagnostics => ({
      method: 'catalog',
      requestId: null,
      status: null,
      dispatch: 'not-dispatched',
      catalogDigest: null,
      attempts: 0,
      startedAt,
      durationMs: Math.round(performance.now() - startedPerf),
      ...extra,
    });
    if (options.signal?.aborted) {
      throw this.#remember(
        new TransportError('aborted', 'REQUEST_ABORTED', 'caller aborted before the catalog request', catalogDiagnostics()),
      );
    }
    let budgetMs: number;
    try {
      budgetMs = this.#budget(options.budgetMs);
    } catch (error) {
      throw this.#remember(
        new TransportError(
          'configuration',
          'INVALID_BUDGET',
          error instanceof Error ? error.message : 'invalid request budget',
          catalogDiagnostics(),
        ),
      );
    }
    const controller = new AbortController();
    let timedOut = false;
    const caller = options.signal;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, budgetMs);
    const onCallerAbort = () => controller.abort();
    if (caller) {
      if (caller.aborted) controller.abort();
      else caller.addEventListener('abort', onCallerAbort, { once: true });
    }

    let response: Response;
    let text: string;
    try {
      let authHeaders: Readonly<Record<string, string>>;
      let fetchImpl: typeof fetch;
      try {
        authHeaders = await this.#untilAborted(this.#resolveAuthHeaders(false), controller.signal, () =>
          this.#beforeDispatchFailure(caller, timedOut, budgetMs, catalogDiagnostics(), 'the catalog request'),
        );
        fetchImpl = this.#resolveFetch();
        if (controller.signal.aborted) {
          throw this.#beforeDispatchFailure(caller, timedOut, budgetMs, catalogDiagnostics(), 'the catalog request');
        }
      } catch (error) {
        if (error instanceof TransportError) throw this.#remember(error);
        throw this.#remember(
          new TransportError(
            'configuration',
            'CLIENT_CONFIGURATION',
            error instanceof Error ? error.message : 'client configuration failed',
            catalogDiagnostics(),
          ),
        );
      }

      const headers = new Headers();
      headers.set('accept', 'application/json');
      for (const [name, value] of Object.entries(authHeaders)) headers.set(name, value);

      try {
        response = await fetchImpl(`${this.#baseUrl}/daemon/request`, {
          method: 'GET',
          headers,
          signal: controller.signal,
          credentials: this.#credentials(),
          redirect: 'error',
          cache: 'no-store',
        });
        text = await response.text();
      } catch (error) {
        const kind = isAbortError(error) ? abortReason(caller, timedOut) : 'network';
        throw this.#remember(
          new TransportError(
            kind,
            kind === 'timeout' ? 'REQUEST_TIMEOUT' : kind === 'aborted' ? 'REQUEST_ABORTED' : 'NETWORK_ERROR',
            kind === 'network' && error instanceof Error ? error.message : `catalog ${kind}`,
            catalogDiagnostics({ dispatch: 'unknown', attempts: 1 }),
          ),
        );
      }
    } finally {
      clearTimeout(timer);
      if (caller) caller.removeEventListener('abort', onCallerAbort);
    }

    const actualDigest = response.headers.get('x-hyprial-catalog-digest');
    const base: RequestDiagnostics = {
      method: 'catalog',
      requestId: response.headers.get('x-hyprial-request-id'),
      status: response.status,
      dispatch: 'dispatched',
      catalogDigest: actualDigest,
      attempts: 1,
      startedAt,
      durationMs: Math.round(performance.now() - startedPerf),
    };
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw this.#remember(new EnvelopeError('ENVELOPE_NOT_JSON', 'catalog body is not JSON', base));
    }
    if (!isRecord(parsed) || parsed.schemaVersion !== ENVELOPE_SCHEMA_VERSION) {
      throw this.#remember(new EnvelopeError('ENVELOPE_INVALID', 'catalog payload is not version 1', base));
    }
    if (response.status === 401 || response.status === 403) {
      throw this.#remember(
        new HttpError(
          typeof parsed.code === 'string' ? parsed.code : 'HTTP_FORBIDDEN',
          typeof parsed.message === 'string' ? parsed.message : 'catalog refused',
          response.status,
          base,
        ),
      );
    }
    if (parsed.ok !== true) {
      throw this.#remember(
        new DomainError(
          typeof parsed.code === 'string' ? parsed.code : 'CATALOG_UNAVAILABLE',
          typeof parsed.message === 'string' ? parsed.message : 'catalog unavailable',
          parsed.data,
          { ...base, dispatch: 'not-dispatched' },
        ),
      );
    }
    const catalogDigest = typeof parsed.catalogDigest === 'string' ? parsed.catalogDigest : actualDigest;
    const expectedDigest = options.catalogDigest ?? this.#config.expectedCatalogDigest;
    if (expectedDigest && catalogDigest !== expectedDigest) {
      throw this.#remember(new CatalogDriftError(expectedDigest, catalogDigest, { ...base, catalogDigest }));
    }
    const catalogVersion = parsed.catalogVersion;
    const declarationDigest = parsed.declarationDigest;
    const generatedFrom = parsed.generatedFrom;
    const backend = parsed.backend;
    const principal = parsed.principal;
    const rawMethods = parsed.methods;
    if (
      typeof catalogVersion !== 'string' ||
      typeof declarationDigest !== 'string' ||
      typeof generatedFrom !== 'string' ||
      !Array.isArray(rawMethods) ||
      !isRecord(backend) ||
      !isRecord(principal)
    ) {
      throw this.#remember(new EnvelopeError('ENVELOPE_INVALID', 'catalog payload is incomplete', base));
    }
    const methods = rawMethods.map((entry: unknown): CatalogMethod => {
      if (!isRecord(entry) || typeof entry.name !== 'string' || typeof entry.path !== 'string') {
        throw this.#remember(new EnvelopeError('ENVELOPE_INVALID', 'catalog method is incomplete', base));
      }
      const name = entry.name;
      const path = entry.path;
      return {
        name,
        path,
        state: typeof entry.state === 'string' ? entry.state : 'unknown',
        callable: entry.callable === true,
      };
    });
    const snapshot: CatalogSnapshot = {
      schemaVersion: ENVELOPE_SCHEMA_VERSION,
      httpContractVersion: typeof parsed.httpContractVersion === 'number' ? parsed.httpContractVersion : 1,
      catalogVersion,
      catalogDigest: catalogDigest ?? '',
      declarationDigest,
      generatedFrom,
      backend,
      principal,
      methods,
    };
    this.#lastDiagnostics = { ...base, catalogDigest: snapshot.catalogDigest };
    return snapshot;
  }

  #untilAborted<T>(promise: Promise<T>, signal: AbortSignal, onAbort: () => Error): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const cleanup = () => signal.removeEventListener('abort', handler);
      const handler = () => {
        cleanup();
        reject(onAbort());
      };
      if (signal.aborted) {
        handler();
        return;
      }
      signal.addEventListener('abort', handler, { once: true });
      promise.then(
        (value) => {
          cleanup();
          resolve(value);
        },
        (error) => {
          cleanup();
          reject(error);
        },
      );
    });
  }

  #beforeDispatchFailure(
    caller: AbortSignal | undefined,
    timedOut: boolean,
    budgetMs: number,
    diagnostics: RequestDiagnostics,
    phase = 'dispatch',
  ): TransportError {
    if (timedOut) {
      return new TransportError(
        'timeout',
        'REQUEST_TIMEOUT',
        `request exceeded its ${budgetMs}ms budget before ${phase}`,
        diagnostics,
      );
    }
    return new TransportError(
      'aborted',
      'REQUEST_ABORTED',
      caller?.aborted ? `caller aborted before ${phase}` : `request aborted before ${phase}`,
      diagnostics,
    );
  }

  #budget(override: number | undefined): number {
    const value = override ?? this.#config.requestBudgetMs ?? DEFAULT_REQUEST_BUDGET_MS;
    if (!Number.isFinite(value) || value <= 0) {
      throw new RangeError('request budget must be a positive number of milliseconds');
    }
    return value;
  }

  async #credential(source: string | (() => string | Promise<string>), label: string): Promise<string> {
    const value = typeof source === 'function' ? await source() : source;
    if (typeof value !== 'string' || value.length === 0) {
      throw new Error(`the configured ${label} provider returned no value`);
    }
    return value;
  }

  async #resolveAuthHeaders(includeCsrf: boolean): Promise<Readonly<Record<string, string>>> {
    if (this.#auth.mode === 'bearer') {
      const token = await this.#credential(this.#auth.token, 'bearer token');
      return { authorization: `Bearer ${token}` };
    }
    const [view, generation, csrf] = await Promise.all([
      this.#credential(this.#auth.view, 'view proof'),
      this.#credential(this.#auth.generation, 'generation'),
      includeCsrf ? this.#credential(this.#auth.csrf, 'CSRF') : Promise.resolve(''),
    ]);
    return {
      'x-hyprial-view': view,
      'x-hyprial-generation': generation,
      ...(includeCsrf ? { 'x-hyprial-csrf': csrf } : {}),
    };
  }

  #credentials(): RequestCredentials {
    return this.#auth.mode === 'same-origin-session' ? 'include' : 'omit';
  }

  #resolveFetch(): typeof fetch {
    const implementation = this.#config.fetch ?? globalThis.fetch;
    if (typeof implementation !== 'function') throw new Error('no fetch implementation is available');
    return implementation.bind(globalThis);
  }

  #remember<T extends Error>(error: T): T {
    const candidate = error as unknown as { diagnostics?: RequestDiagnostics };
    if (candidate.diagnostics) this.#lastDiagnostics = candidate.diagnostics;
    return error;
  }
}

export type {
  DispatchState,
  HttpTransportAuth,
  HttpTransportConfig,
  InvocationResult,
  RequestDiagnostics,
  RequestOptions,
} from './types';
