/** Component-level settings read states; not live browser or backend evidence. */
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TransportError } from '../../src/client';
import { DiagnosticSection } from '../../src/features/settings';
import type { DiagnosticApi, DiagnosticSnapshot } from '../../src/features/settings';

const DIAGNOSTIC: DiagnosticSnapshot = {
  observedAtMs: 1_762_075_800_000,
  actorCount: 2,
  strandedCount: 1,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function unknownFailure(method: string): TransportError {
  return new TransportError('network', 'NETWORK_ERROR', 'response lost', {
    method,
    requestId: 'settings-read',
    status: null,
    dispatch: 'unknown',
    catalogDigest: 'settings-test',
    attempts: 1,
    startedAt: 1,
    durationMs: 1,
  });
}

describe('diagnostic read states', () => {
  it('renders diagnostic counts and rejects a replaced api snapshot', async () => {
    const first = deferred<DiagnosticSnapshot>();
    let firstSignal: AbortSignal | undefined;
    const firstApi: DiagnosticApi = {
      read: vi.fn((signal) => {
        firstSignal = signal;
        return first.promise;
      }),
    };
    const secondApi: DiagnosticApi = { read: vi.fn(async () => DIAGNOSTIC) };
    const view = render(<DiagnosticSection api={firstApi} />);
    expect(screen.getByRole('status')).toHaveTextContent('正在读取运行诊断');
    view.rerender(<DiagnosticSection api={secondApi} />);
    expect((await screen.findByText('Agent 观测条目')).parentElement).toHaveTextContent('Agent 观测条目2');
    expect(screen.getByText('滞留收件人条目').parentElement).toHaveTextContent('滞留收件人条目1');
    expect(firstSignal?.aborted).toBe(true);
    await act(async () => {
      first.resolve({ observedAtMs: 1, actorCount: 9, strandedCount: 9 });
    });
    expect(screen.getByText('Agent 观测条目').parentElement).not.toHaveTextContent('Agent 观测条目9');
  });

  it('separates failed and unknown diagnostic reads from an empty snapshot', async () => {
    const failed = deferred<DiagnosticSnapshot>();
    const failedApi: DiagnosticApi = { read: vi.fn(() => failed.promise) };
    const view = render(<DiagnosticSection api={failedApi} />);
    await act(async () => {
      failed.reject(new Error('diagnostic failed'));
    });
    expect(await screen.findByText('运行诊断查询失败')).toBeInTheDocument();
    expect(screen.queryByText(/当前快照没有 Agent/)).not.toBeInTheDocument();

    const unknownApi: DiagnosticApi = { read: vi.fn(async () => { throw unknownFailure('top.snapshot'); }) };
    view.rerender(<DiagnosticSection api={unknownApi} />);
    expect(await screen.findByText('运行诊断结果未知')).toBeInTheDocument();
  });
});
