/**
 * A2 operation outcome model: not-sent / in-flight / success /
 * definitive-failure / unknown.
 *
 * This module is presentation state only. It does not perform network calls,
 * authentication, storage, retries, or idempotency-key generation, and it does
 * not define a business contract. The caller supplies the opaque request id;
 * this module never creates, derives, normalizes, or substitutes one.
 *
 * Unknown is not success. An unknown operation can only be reconciled by its
 * original request id, and this model refuses to move it back to in-flight:
 * retrying the write is the caller's contract-level decision, not a transition
 * this model may invent. Repeated terminal resolutions are idempotent; a
 * conflicting terminal outcome is rejected.
 *
 * Read state is kept separate from the send outcome. A failed read keeps the
 * previously loaded data, marks it stale, and records the error; it never
 * clears data or turns the operation into a success. A read success requires
 * actual loaded data, so an empty success is not representable.
 */

export type OperationStatus =
  | 'not-sent'
  | 'in-flight'
  | 'success'
  | 'definitive-failure'
  | 'unknown';

export interface OperationRecord<TData> {
  readonly requestId: string;
  readonly status: OperationStatus;
  /** Last successfully loaded presentation data; retained across read errors. */
  readonly data: TData | undefined;
  /** True when the retained data is older than a failed latest read. */
  readonly stale: boolean;
  readonly readError: string | null;
  /** Error for a definitive operation failure; never used for read errors. */
  readonly failure: string | null;
}

export interface OperationLedger<TData> {
  readonly operations: ReadonlyMap<string, OperationRecord<TData>>;
}

export type OperationResolution =
  | { readonly status: 'success' }
  | { readonly status: 'definitive-failure'; readonly error: string }
  | { readonly status: 'unknown' };

export interface OperationErrorProjection {
  readonly status: OperationStatus;
  readonly stale: boolean;
  readonly dataAvailable: boolean;
  readonly error: string | null;
  readonly needsReconciliation: boolean;
}

export class OperationTransitionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'OperationTransitionError';
  }
}

function requireRequestId(requestId: string): string {
  if (requestId.length === 0 || requestId.trim() !== requestId) {
    throw new OperationTransitionError(
      'requestId must be a caller-supplied, non-empty opaque string without surrounding whitespace',
    );
  }
  return requestId;
}

function requireNonEmpty(value: string, label: string): string {
  if (value.trim().length === 0) {
    throw new OperationTransitionError(`${label} must be non-empty`);
  }
  return value;
}

function requireOperation<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
): OperationRecord<TData> {
  const id = requireRequestId(requestId);
  const operation = ledger.operations.get(id);
  if (operation === undefined) {
    throw new OperationTransitionError(
      `unknown operation request id: ${id}`,
    );
  }
  return operation;
}

function replaceOperation<TData>(
  ledger: OperationLedger<TData>,
  operation: OperationRecord<TData>,
): OperationLedger<TData> {
  const operations = new Map(ledger.operations);
  operations.set(operation.requestId, operation);
  return { operations };
}

export function createOperationLedger<TData = unknown>(): OperationLedger<TData> {
  return { operations: new Map() };
}

/**
 * Registers a caller-supplied request id as not-sent. Registering the same id
 * again is an idempotent no-op, not a second request.
 */
export function registerOperation<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
): OperationLedger<TData> {
  const id = requireRequestId(requestId);
  if (ledger.operations.has(id)) {
    return ledger;
  }
  return replaceOperation(ledger, {
    requestId: id,
    status: 'not-sent',
    data: undefined,
    stale: false,
    readError: null,
    failure: null,
  });
}

/** Moves a registered operation from not-sent to in-flight. */
export function beginOperation<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
): OperationLedger<TData> {
  const operation = requireOperation(ledger, requestId);
  if (operation.status !== 'not-sent') {
    throw new OperationTransitionError(
      `cannot begin ${operation.requestId} from status ${operation.status}`,
    );
  }
  return replaceOperation(ledger, { ...operation, status: 'in-flight' });
}

/** Records that the in-flight outcome is not known yet. */
export function markOperationUnknown<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
): OperationLedger<TData> {
  const operation = requireOperation(ledger, requestId);
  if (operation.status !== 'in-flight') {
    throw new OperationTransitionError(
      `cannot mark ${operation.requestId} unknown from status ${operation.status}`,
    );
  }
  return replaceOperation(ledger, { ...operation, status: 'unknown' });
}

/**
 * Resolves an in-flight or unknown operation. Terminal resolutions are
 * idempotent when repeated with the same outcome, and rejected when they
 * conflict with the recorded terminal outcome.
 */
export function resolveOperation<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
  resolution: OperationResolution,
): OperationLedger<TData> {
  const operation = requireOperation(ledger, requestId);

  if (operation.status === 'not-sent') {
    throw new OperationTransitionError(
      `cannot resolve ${operation.requestId} before it is in flight`,
    );
  }

  if (
    operation.status === 'success' ||
    operation.status === 'definitive-failure'
  ) {
    if (operation.status === resolution.status) {
      return ledger;
    }
    throw new OperationTransitionError(
      `cannot change terminal status ${operation.status} to ${resolution.status}`,
    );
  }

  if (resolution.status === 'success') {
    return replaceOperation(ledger, {
      ...operation,
      status: 'success',
      failure: null,
    });
  }

  if (resolution.status === 'definitive-failure') {
    const error = requireNonEmpty(resolution.error, 'resolution error');
    return replaceOperation(ledger, {
      ...operation,
      status: 'definitive-failure',
      failure: error,
    });
  }

  if (operation.status === 'unknown') {
    return ledger;
  }
  return replaceOperation(ledger, { ...operation, status: 'unknown' });
}

/**
 * Reconciles an unknown operation against its original request id. This checks
 * the existing record first; it never creates a new operation and never
 * permits reconciliation from a state other than unknown.
 */
export function reconcileUnknownOperation<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
  resolution: OperationResolution,
): OperationLedger<TData> {
  const operation = requireOperation(ledger, requestId);
  if (operation.status !== 'unknown') {
    throw new OperationTransitionError(
      `cannot reconcile ${operation.requestId} from status ${operation.status}; only unknown can be reconciled`,
    );
  }
  return resolveOperation(ledger, requestId, resolution);
}

/** Stores a successful read without changing the operation outcome. */
export function recordReadSuccess<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
  data: TData,
): OperationLedger<TData> {
  const operation = requireOperation(ledger, requestId);
  if (data === undefined) {
    throw new OperationTransitionError(
      'read success requires loaded data; empty success is not representable',
    );
  }
  return replaceOperation(ledger, {
    ...operation,
    data,
    stale: false,
    readError: null,
  });
}

/**
 * Preserves previously loaded data while marking it stale and recording the
 * read error. The operation outcome and any definitive failure are unchanged.
 */
export function recordReadFailure<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
  error: string,
): OperationLedger<TData> {
  const operation = requireOperation(ledger, requestId);
  const message = requireNonEmpty(error, 'read error');
  return replaceOperation(ledger, {
    ...operation,
    stale: true,
    readError: message,
  });
}

export function getOperation<TData>(
  ledger: OperationLedger<TData>,
  requestId: string,
): OperationRecord<TData> | undefined {
  return ledger.operations.get(requestId);
}

/** Projects stale/read/operation errors without adding retry policy or copy. */
export function projectOperationError<TData>(
  operation: OperationRecord<TData>,
): OperationErrorProjection {
  let error: string | null = null;
  if (operation.status === 'definitive-failure') {
    error = operation.failure;
  } else if (operation.readError !== null) {
    error = operation.readError;
  }

  return {
    status: operation.status,
    stale: operation.stale,
    dataAvailable: operation.data !== undefined,
    error,
    needsReconciliation: operation.status === 'unknown',
  };
}
