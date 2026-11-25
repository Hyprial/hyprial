import {
  beginOperation,
  createOperationLedger,
  getOperation,
  markOperationUnknown,
  OperationTransitionError,
  projectOperationError,
  reconcileUnknownOperation,
  recordReadFailure,
  recordReadSuccess,
  registerOperation,
  resolveOperation,
} from '../src/state/operation-lifecycle';

const id = 'request-opaque-1';

function setupInFlight(requestId = id) {
  let ledger = registerOperation(createOperationLedger<string>(), requestId);
  ledger = beginOperation(ledger, requestId);
  return ledger;
}

describe('operation outcome states', () => {
  it('distinguishes all five outcomes and does not treat unknown as success', () => {
    let ledger = registerOperation(createOperationLedger<string>(), id);
    expect(getOperation(ledger, id)?.status).toBe('not-sent');

    ledger = beginOperation(ledger, id);
    expect(getOperation(ledger, id)?.status).toBe('in-flight');

    ledger = markOperationUnknown(ledger, id);
    expect(getOperation(ledger, id)?.status).toBe('unknown');
    expect(getOperation(ledger, id)?.status).not.toBe('success');

    ledger = reconcileUnknownOperation(ledger, id, { status: 'success' });
    expect(getOperation(ledger, id)?.status).toBe('success');

    ledger = registerOperation(ledger, 'request-opaque-2');
    ledger = beginOperation(ledger, 'request-opaque-2');
    ledger = resolveOperation(ledger, 'request-opaque-2', {
      status: 'definitive-failure',
      error: 'request rejected',
    });
    expect(getOperation(ledger, 'request-opaque-2')).toMatchObject({
      status: 'definitive-failure',
      failure: 'request rejected',
    });
  });

  it('allows a definitive outcome without first passing through unknown', () => {
    const success = resolveOperation(setupInFlight(), id, {
      status: 'success',
    });
    expect(getOperation(success, id)?.status).toBe('success');

    const failure = resolveOperation(setupInFlight(), id, {
      status: 'definitive-failure',
      error: 'rejected',
    });
    expect(getOperation(failure, id)?.status).toBe('definitive-failure');
  });
});

describe('unknown reconciliation', () => {
  it('reconciles only by the original request id and creates no second request', () => {
    let ledger = markOperationUnknown(setupInFlight(), id);
    const before = ledger;

    expect(() => beginOperation(before, id)).toThrow(OperationTransitionError);
    expect(() =>
      reconcileUnknownOperation(before, 'request-opaque-other', {
        status: 'success',
      }),
    ).toThrow(OperationTransitionError);
    expect(before.operations.size).toBe(1);
    expect(getOperation(before, id)?.status).toBe('unknown');

    ledger = reconcileUnknownOperation(before, id, { status: 'success' });
    expect(ledger.operations.size).toBe(1);
    expect(getOperation(ledger, id)?.status).toBe('success');
  });

  it('keeps repeated unknown reconciliation idempotent', () => {
    const unknown = markOperationUnknown(setupInFlight(), id);
    const once = reconcileUnknownOperation(unknown, id, { status: 'unknown' });
    const twice = reconcileUnknownOperation(once, id, { status: 'unknown' });

    expect(once).toBe(unknown);
    expect(twice).toBe(once);
    expect(twice.operations.size).toBe(1);
  });

  it('treats repeated terminal resolution as idempotent', () => {
    const success = resolveOperation(setupInFlight(), id, {
      status: 'success',
    });
    const repeatedSuccess = resolveOperation(success, id, {
      status: 'success',
    });
    expect(repeatedSuccess).toBe(success);
    expect(repeatedSuccess.operations.size).toBe(1);

    const failure = resolveOperation(setupInFlight(), id, {
      status: 'definitive-failure',
      error: 'rejected',
    });
    const repeatedFailure = resolveOperation(failure, id, {
      status: 'definitive-failure',
      error: 'rejected',
    });
    expect(repeatedFailure).toBe(failure);
    expect(repeatedFailure.operations.size).toBe(1);
  });

  it('rejects a conflicting terminal outcome instead of silently correcting it', () => {
    const success = resolveOperation(setupInFlight(), id, {
      status: 'success',
    });

    expect(() =>
      resolveOperation(success, id, {
        status: 'definitive-failure',
        error: 'late failure',
      }),
    ).toThrow(OperationTransitionError);
    expect(() =>
      resolveOperation(success, id, { status: 'unknown' }),
    ).toThrow(OperationTransitionError);
    expect(getOperation(success, id)?.status).toBe('success');
  });

  it('rejects reconciliation for a request that is not unknown', () => {
    const inFlight = setupInFlight();
    expect(() =>
      reconcileUnknownOperation(inFlight, id, { status: 'success' }),
    ).toThrow(OperationTransitionError);
    expect(getOperation(inFlight, id)?.status).toBe('in-flight');
  });
});

describe('read failure projection', () => {
  it('retains previously loaded data and marks stale/error without faking success', () => {
    let ledger = registerOperation(createOperationLedger<string>(), id);
    ledger = recordReadSuccess(ledger, id, 'loaded-before-failure');
    ledger = recordReadFailure(ledger, id, 'read failed');

    const operation = getOperation(ledger, id);
    expect(operation).toMatchObject({
      status: 'not-sent',
      data: 'loaded-before-failure',
      stale: true,
      readError: 'read failed',
    });
    expect(operation?.status).not.toBe('success');
    expect(projectOperationError(operation!)).toMatchObject({
      status: 'not-sent',
      stale: true,
      dataAvailable: true,
      error: 'read failed',
      needsReconciliation: false,
    });
  });

  it('does not turn a failed first read into an empty success', () => {
    let ledger = registerOperation(createOperationLedger<string>(), id);
    ledger = recordReadFailure(ledger, id, 'read failed');

    const operation = getOperation(ledger, id);
    expect(operation?.data).toBeUndefined();
    expect(operation).toMatchObject({ stale: true, readError: 'read failed' });
    expect(operation?.status).not.toBe('success');
    expect(projectOperationError(operation!)).toMatchObject({
      stale: true,
      dataAvailable: false,
      error: 'read failed',
    });
  });

  it('rejects an empty read success', () => {
    let ledger = createOperationLedger<string | undefined>();
    ledger = registerOperation(ledger, id);

    expect(() => recordReadSuccess(ledger, id, undefined)).toThrow(
      /loaded data/,
    );
    expect(getOperation(ledger, id)).toMatchObject({
      data: undefined,
      stale: false,
      readError: null,
    });
  });

  it('clears stale/error only when a later read succeeds with data', () => {
    let ledger = registerOperation(createOperationLedger<string>(), id);
    ledger = recordReadSuccess(ledger, id, 'old');
    ledger = recordReadFailure(ledger, id, 'read failed');
    ledger = recordReadSuccess(ledger, id, 'fresh');

    expect(getOperation(ledger, id)).toMatchObject({
      data: 'fresh',
      stale: false,
      readError: null,
    });
  });
});

describe('illegal transitions and opaque ids', () => {
  it('rejects illegal transitions without changing the prior ledger', () => {
    let ledger = registerOperation(createOperationLedger<string>(), id);

    expect(() => beginOperation(ledger, 'request-missing')).toThrow(
      OperationTransitionError,
    );
    expect(() => markOperationUnknown(ledger, id)).toThrow(
      OperationTransitionError,
    );
    expect(() =>
      resolveOperation(ledger, id, { status: 'success' }),
    ).toThrow(OperationTransitionError);
    expect(ledger.operations.size).toBe(1);
    expect(getOperation(ledger, id)?.status).toBe('not-sent');

    const inFlight = beginOperation(ledger, id);
    expect(() => beginOperation(inFlight, id)).toThrow(
      OperationTransitionError,
    );
    expect(getOperation(inFlight, id)?.status).toBe('in-flight');

    const terminal = resolveOperation(inFlight, id, { status: 'success' });
    expect(() => markOperationUnknown(terminal, id)).toThrow(
      OperationTransitionError,
    );
    expect(() => beginOperation(terminal, id)).toThrow(
      OperationTransitionError,
    );
    expect(getOperation(terminal, id)?.status).toBe('success');
  });

  it('does not create a second operation when the same request is registered again', () => {
    const first = registerOperation(createOperationLedger<string>(), id);
    const duplicate = registerOperation(first, id);

    expect(duplicate).toBe(first);
    expect(duplicate.operations.size).toBe(1);
    expect(Array.from(duplicate.operations.keys())).toEqual([id]);
  });

  it('uses only the caller-supplied opaque id and never fabricates one', () => {
    const empty = createOperationLedger<string>();
    expect(empty.operations.size).toBe(0);

    const ledger = registerOperation(empty, id);
    expect(Array.from(ledger.operations.keys())).toEqual([id]);
    expect(getOperation(ledger, id)?.requestId).toBe(id);
    expect(getOperation(ledger, 'local-agent-squire-2')).toBeUndefined();
    expect(getOperation(ledger, 'dev000000001.local')).toBeUndefined();
    expect(
      getOperation(ledger, 'https://example.invalid/request'),
    ).toBeUndefined();
  });

  it('rejects empty or padded ids instead of normalizing them', () => {
    const ledger = createOperationLedger<string>();

    expect(() => registerOperation(ledger, '')).toThrow(
      OperationTransitionError,
    );
    expect(() => registerOperation(ledger, '   ')).toThrow(
      OperationTransitionError,
    );
    expect(() => registerOperation(ledger, ' request-opaque-1')).toThrow(
      OperationTransitionError,
    );
    expect(ledger.operations.size).toBe(0);
  });

  it('keeps independent operations isolated by exact id', () => {
    let ledger = createOperationLedger<string>();
    ledger = registerOperation(ledger, 'request-a');
    ledger = beginOperation(ledger, 'request-a');
    ledger = markOperationUnknown(ledger, 'request-a');
    ledger = registerOperation(ledger, 'request-b');

    expect(getOperation(ledger, 'request-a')?.status).toBe('unknown');
    expect(getOperation(ledger, 'request-b')?.status).toBe('not-sent');

    ledger = reconcileUnknownOperation(ledger, 'request-a', {
      status: 'success',
    });
    expect(getOperation(ledger, 'request-a')?.status).toBe('success');
    expect(getOperation(ledger, 'request-b')?.status).toBe('not-sent');
    expect(ledger.operations.size).toBe(2);
  });
});
