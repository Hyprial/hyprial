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

export type TokenSource = string | (() => string | Promise<string>);

export interface HttpTransportConfig {
  /** Origin plus optional path prefix, for example `http://127.0.0.1:47831`. */
  readonly baseUrl: string;
  /** Bearer token source. The value is never placed in a URL or a component. */
  readonly token: TokenSource;
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
