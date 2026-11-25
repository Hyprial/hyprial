/** Public transport types shared by the generated client and feature adapters. */

export type DispatchState = 'not-dispatched' | 'dispatched' | 'unknown';
export type RetryPolicy = 'never' | 'safe';

export interface RequestOptions {
  /** Caller cancellation. Aborting after the request is sent leaves dispatch unknown. */
  readonly signal?: AbortSignal;
  /** Per-invocation budget in milliseconds. Defaults to the transport budget. */
  readonly budgetMs?: number;
  /**
   * Metadata from the generated client. `safe` is never set for writes. The
   * v1 transport still performs exactly one attempt; callers decide explicitly
   * whether an idempotent read may be repeated.
   */
  readonly retry?: RetryPolicy;
  /** Pinned catalog digest; drift raises CatalogDriftError and is never retried. */
  readonly catalogDigest?: string;
  /** Extra request headers. Transport-owned auth and content headers always win. */
  readonly headers?: Readonly<Record<string, string>>;
}

export interface RequestDiagnostics {
  readonly method: string;
  readonly requestId: string | null;
  readonly status: number | null;
  /** Did the request reach daemon dispatch? Never inferred as "definitely not sent". */
  readonly dispatch: DispatchState;
  readonly catalogDigest: string | null;
  readonly attempts: number;
  readonly startedAt: number;
  readonly durationMs: number;
}

export interface InvocationResult<TResult> {
  readonly result: TResult;
  readonly diagnostics: RequestDiagnostics;
}

/** Safe catalog projection used to disable actions before transport dispatch. */
export interface MethodAvailability {
  readonly available: boolean;
  readonly code?: string;
  readonly reason?: string;
}

/** Resolve one call against the pinned catalog and the composition policy. */
export type MethodAccess = (method: string, params?: unknown) => MethodAvailability;

export type TokenSource = string | (() => string | Promise<string>);

/** Direct HTTP callers keep the bearer mode used by hosts and non-browser code. */
export interface BearerHttpAuth {
  readonly mode: 'bearer';
  readonly token: TokenSource;
}

/**
 * Browser callers use cookies for the session and a per-view proof from
 * sessionStorage. The upstream bearer never enters this mode.
 */
export interface SameOriginSessionAuth {
  readonly mode: 'same-origin-session';
  readonly view: TokenSource;
  readonly generation: TokenSource;
  readonly csrf: TokenSource;
  /** Unknown methods fail safe as writes and therefore require CSRF. */
  readonly mutates: (method: string) => boolean;
}

export type HttpTransportAuth = BearerHttpAuth | SameOriginSessionAuth;

export interface HttpTransportConfig {
  /** Origin plus optional path prefix, for example `http://127.0.0.1:47831`. */
  readonly baseUrl: string;
  /** Legacy bearer shorthand. Exactly one of `token` and `auth` is required. */
  readonly token?: TokenSource;
  /** Explicit auth mode; same-origin session auth never carries a bearer. */
  readonly auth?: HttpTransportAuth;
  /** Injectable fetch for tests and non-browser hosts. */
  readonly fetch?: typeof fetch;
  readonly requestBudgetMs?: number;
  /** Pin the served contract; any different server digest is reported as drift. */
  readonly expectedCatalogDigest?: string;
}

export interface CatalogMethod {
  readonly name: string;
  readonly path: string;
  readonly state: string;
  readonly callable: boolean;
  readonly reason?: string | null;
  readonly mutates?: boolean;
  readonly retry?: string;
  readonly paramsSchema?: unknown;
  readonly resultSchema?: unknown;
  readonly errors?: ReadonlyArray<unknown>;
}

export interface CatalogSnapshot {
  readonly schemaVersion: number;
  readonly httpContractVersion: number;
  readonly catalogVersion: string;
  readonly catalogDigest: string;
  readonly declarationDigest: string;
  readonly generatedFrom: string;
  readonly backend: Readonly<Record<string, unknown>>;
  readonly principal: Readonly<Record<string, unknown>>;
  readonly methods: ReadonlyArray<CatalogMethod>;
}
