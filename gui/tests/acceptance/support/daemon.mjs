/**
 * Decision record (S3/S4 browser acceptance, 2026-11-21..2026-11-22)
 * - Starts an isolated `hyprial daemon run` (or `tests.support.tailnet_daemon`
 *   when `membershipDouble` is requested) under `HYPRIAL_NETWORK_ISOLATED=1`.
 * - Waits until `ping` reports `serving`/`reconciled` with `restorePending !== true`
 *   and an `epoch` matching `daemon.catalog`, then calls
 *   `onboarding.firstRun.skip` over IPC when `skipFirstRun` is enabled and
 *   `onboarding.status` does not already report `firstRun.skipped === true`,
 *   so restarts on an existing root succeed without `expectedRevision: 0` conflict.
 * - When `throughProxy` is enabled, moves `daemon.sock` to
 *   `daemon-upstream.sock` and mounts `startIpcDropProxy` on `daemon.sock`.
 * - `startRealDaemon` cleans up any started proxy and child process via
 *   `stopChild` if startup fails; `ipcCall` settles idempotently and destroys
 *   the client socket on connect failure, response, parse error, timeout, or
 *   premature close.
 */

import { rename } from 'node:fs/promises';
import { connect as connectNet } from 'node:net';
import { join } from 'node:path';
import {
  HYPRIAL,
  PYTHON,
  cleanEnvironment,
  prepareDaemonHome,
  prepareRoot,
  waitFor,
} from './home.mjs';
import {
  captureProcess,
  launch,
  logsOf,
  stopChild,
  waitForPath,
} from './cli.mjs';
import { startIpcDropProxy } from './ipc-proxy.mjs';

export async function ipcCall(socketPath, method, params) {
  const client = connectNet(socketPath);
  client.setEncoding('utf8');
  const id = `browser-acceptance-${method.replaceAll('.', '-')}`;
  let buffer = '';
  await new Promise((resolveConnect, rejectConnect) => {
    const onConnect = () => {
      client.off('error', onError);
      client.off('close', onClose);
      resolveConnect();
    };
    const onError = (error) => {
      client.off('connect', onConnect);
      client.off('close', onClose);
      client.destroy();
      rejectConnect(error);
    };
    const onClose = () => {
      client.off('connect', onConnect);
      client.off('error', onError);
      client.destroy();
      rejectConnect(new Error(`IPC ${method} closed before connect`));
    };
    client.once('connect', onConnect);
    client.once('error', onError);
    client.once('close', onClose);
  });
  client.write(`${JSON.stringify({ version: 1, id, method, params })}\n`);
  return new Promise((resolveResponse, rejectResponse) => {
    let settled = false;
    let timer;
    const finish = (action, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      client.destroy();
      action(value);
    };
    timer = setTimeout(() => finish(rejectResponse, new Error(`IPC ${method} timed out`)), 20_000);
    client.on('data', (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf('\n');
      if (newline < 0) return;
      try {
        const response = JSON.parse(buffer.slice(0, newline));
        if (response.error) finish(rejectResponse, new Error(JSON.stringify(response.error)));
        else finish(resolveResponse, response.result);
      } catch (error) {
        finish(rejectResponse, error);
      }
    });
    client.once('error', (error) => finish(rejectResponse, error));
    client.once('close', () => finish(rejectResponse, new Error(`IPC ${method} closed before response`)));
  });
}

export async function waitForDaemonServing(child, socketPath, capture, timeoutMs = 60_000) {
  await waitFor(async () => {
    if (child.exitCode !== null) throw new Error(`daemon exited before serving:\n${logsOf(capture)}`);
    try {
      const ping = await ipcCall(socketPath, 'ping', {});
      const catalog = await ipcCall(socketPath, 'daemon.catalog', {});
      return ['serving', 'reconciled'].includes(ping.phase)
        && ping.restorePending !== true
        && typeof ping.epoch === 'string'
        && ping.epoch === catalog.epoch;
    } catch {
      return false;
    }
  }, 'serving daemon with a consistent catalog', timeoutMs);
}

export async function startRealDaemon(root, {
  throughProxy = false,
  membershipDouble = false,
  prepareHome = true,
  skipFirstRun = true,
  dropMethod = 'agent.create',
  armAfterMethod = null,
  onDrop = null,
} = {}) {
  await prepareRoot(root);
  if (prepareHome) await prepareDaemonHome(root);
  const socketPath = join(root, 'state', 'daemon.sock');
  const env = cleanEnvironment(root);
  const argv = membershipDouble
    ? [PYTHON, '-m', 'tests.support.tailnet_daemon', 'daemon', 'run']
    : [HYPRIAL, 'daemon', 'run'];
  const child = launch(argv[0], argv.slice(1), {
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const capture = captureProcess(child);
  let proxy = null;
  try {
    await waitForPath(child, socketPath, capture);
    await waitForDaemonServing(child, socketPath, capture);
    if (skipFirstRun) {
      const status = await ipcCall(socketPath, 'onboarding.status', {});
      if (status?.firstRun?.skipped !== true) {
        const revision = typeof status?.firstRun?.revision === 'number' ? status.firstRun.revision : 0;
        await ipcCall(socketPath, 'onboarding.firstRun.skip', {
          expectedRevision: revision,
          operationKey: `acceptance-first-run-skip-${process.pid}-${Date.now()}`,
        });
      }
    }
    if (!throughProxy) return { child, capture, socketPath, upstreamPath: socketPath, proxy: null };
    const upstreamPath = join(root, 'state', 'daemon-upstream.sock');
    await rename(socketPath, upstreamPath);
    proxy = await startIpcDropProxy(socketPath, upstreamPath, dropMethod, { armAfterMethod, onDrop });
    return { child, capture, socketPath, upstreamPath, proxy };
  } catch (error) {
    if (proxy !== null) await proxy.close().catch(() => undefined);
    await stopChild(child);
    throw error;
  }
}
