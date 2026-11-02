import type { DispatchState, RequestDiagnostics } from './types';

/** Base class for every typed client failure. Diagnostics never claim rollback. */
export abstract class ClientError extends Error {
  abstract readonly kind: string;
  readonly code: string;
  readonly diagnostics: RequestDiagnostics;

  protected constructor(code: string, message: string, diagnostics: RequestDiagnostics) {
    super(message);
    this.name = new.target.name;
    this.code = code;
    this.diagnostics = diagnostics;
    Object.setPrototypeOf(this, new.target.prototype);
  }

  get dispatch(): DispatchState {
    return this.diagnostics.dispatch;
  }
}

export type TransportFailure = 'network' | 'timeout' | 'aborted' | 'budget-exhausted' | 'configuration';

export class TransportError extends ClientError {
  readonly kind: TransportFailure;

  constructor(kind: TransportFailure, code: string, message: string, diagnostics: RequestDiagnostics) {
    super(code, message, diagnostics);
    this.kind = kind;
  }
}

export class HttpError extends ClientError {
  readonly kind = 'http';
  readonly status: number;

  constructor(code: string, message: string, status: number, diagnostics: RequestDiagnostics) {
    super(code, message, diagnostics);
    this.status = status;
  }
}

export class DomainError extends ClientError {
  readonly kind = 'domain';
  readonly data: unknown;

  constructor(code: string, message: string, data: unknown, diagnostics: RequestDiagnostics) {
    super(code, message, diagnostics);
    this.data = data;
  }
}

export class EnvelopeError extends ClientError {
  readonly kind = 'invalid-envelope';

  constructor(code: string, message: string, diagnostics: RequestDiagnostics) {
    super(code, message, diagnostics);
  }
}

export class CatalogDriftError extends ClientError {
  readonly kind = 'catalog-drift';
  readonly expected: string;
  readonly actual: string | null;

  constructor(expected: string, actual: string | null, diagnostics: RequestDiagnostics) {
    super(
      'CATALOG_DRIFT',
      `served catalog ${actual ?? '(missing digest)'} does not match the pinned ${expected}`,
      diagnostics,
    );
    this.expected = expected;
    this.actual = actual;
  }
}
