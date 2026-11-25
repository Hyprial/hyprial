/**
 * Same-window lifecycle (2026-11-13): poll only the host control channel.
 * A generation change retires adapters and clears protected UI before a new
 * handshake. No business operation is replayed; expiry requires a new launch.
 * Recovery has one 30-second deadline, including rotation waits and handshakes.
 * A successful handshake clears it; failures never extend the same deadline.
 */
import {
  BrowserBootstrapError, connectBrowserBackend, requestSessionStatus,
  type BrowserBackendBootstrap, type BrowserSessionOptions,
} from './browser-session';
import type { BackendTransition } from './types';

export function watchBrowserSession(
  initial: BrowserBackendBootstrap,
  callbacks: {
    readonly changing: (reason: BackendTransition) => void;
    readonly connected: (next: BrowserBackendBootstrap) => void;
    readonly failed: (error: unknown) => void;
  },
  options: BrowserSessionOptions = {},
): () => void {
  let current = initial;
  let stopped = false;
  let reconnecting = false;
  let awaitingRotation = false;
  let timer: ReturnType<typeof setTimeout>;
  let recoveryTimer: ReturnType<typeof setTimeout> | undefined;
  const stop = () => {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(recoveryTimer);
    unsubscribe();
    current.connection.retire();
  };
  const fail = (error: unknown) => {
    if (stopped) return;
    stop();
    callbacks.failed(error);
  };
  const change = (reason: BackendTransition = 'reconnect') => {
    if (reconnecting) return;
    reconnecting = true;
    recoveryTimer = setTimeout(() => fail(new BrowserBootstrapError(
      'SESSION_RECOVERY_TIMEOUT', 'the local service did not recover within 30 seconds',
    )), 30_000);
    current.connection.retire();
    callbacks.changing(reason);
  };
  const transition = (reason: BackendTransition) => { if (!stopped) { awaitingRotation = true; change(reason); } };
  let unsubscribe = current.subscribeTransition(transition);
  const tick = async () => {
    try {
      if (!reconnecting || awaitingRotation) {
        const response = await requestSessionStatus(current.session, options);
        if (stopped) return;
        if (!response.ok) throw new BrowserBootstrapError('SESSION_UNAVAILABLE', 'the view session is unavailable', response.status);
        const body = await response.json() as { ok?: boolean; current?: boolean };
        if (stopped) return;
        if (body.ok !== true || typeof body.current !== 'boolean') throw new BrowserBootstrapError('SESSION_INVALID_RESPONSE', 'the host session response is invalid');
        if (!body.current) { awaitingRotation = false; change(); }
      }
      if (reconnecting && !awaitingRotation) {
        const next = await connectBrowserBackend(options);
        if (stopped) { next.connection.retire(); return; }
        clearTimeout(recoveryTimer);
        recoveryTimer = undefined;
        unsubscribe();
        current = next;
        unsubscribe = current.subscribeTransition(transition);
        reconnecting = false;
        callbacks.connected(next);
      }
    } catch (error) {
      if (stopped) return;
      change();
      if (error instanceof BrowserBootstrapError && (error.status === 401 || error.status === 403)) {
        fail(error);
      }
    } finally {
      if (!stopped) timer = setTimeout(() => { void tick(); }, 1000);
    }
  };
  timer = setTimeout(() => { void tick(); }, 1000);
  return stop;
}
