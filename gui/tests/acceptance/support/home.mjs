/**
 * Decision record (S1/S3/S4 browser acceptance, 2026-11-21..2026-11-22)
 * - Temporary roots and screenshots use `os.tmpdir()` so caller `TMPDIR` is
 *   respected without a loader hook, keeping AF_UNIX socket paths below 100
 *   bytes (`withRoot`).
 * - `cleanEnvironment` strips inherited `HYPRIAL_*` and `HARNESS_*` variables,
 *   isolates home/state/XDG/cache dirs under one temporary root, forces
 *   `HYPRIAL_NETWORK_ISOLATED=1` and loopback `NO_PROXY`, and sets
 *   `HYPRIAL_ACTOR_BACKEND` from `HYPRIAL_ACCEPTANCE_ACTOR_BACKEND` (defaulting
 *   to `reference-abi`).
 * - Real-daemon homes are prepared via `tests.support.gui_home prepare` without
 *   `HYPRIAL_NODE_ID` override.
 * - `HYPRIAL_ACCEPTANCE_DRIVERS` filters Contract Mock vs Daemon tests/legs and
 *   emits `NOT_RUN <name> <reason>` via `recordNotRun`.
 * - Live-source worktrees are created as detached temporary Git worktrees of
 *   HEAD with `node_modules` symlinked to this checkout's `gui/node_modules`
 *   so edits never touch the checkout under test; cleanup covers `git worktree add`
 *   failures and `SIGTERM`/`SIGINT` signals idempotently before re-raising.
 */

import assert from 'node:assert/strict';
import { execFile, execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { rmSync } from 'node:fs';
import {
  access,
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rm,
  symlink,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

export const run = promisify(execFile);
export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');
export const GUI_ROOT = join(REPO_ROOT, 'gui');
export const OWNER = 'browser-m1-owner';
export const PYTHON = join(REPO_ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
export const HYPRIAL = join(REPO_ROOT, '.venv', process.platform === 'win32' ? 'Scripts/hyprial.exe' : 'bin/hyprial');
export const CONNECTED_READY = 'main.main#main-content';

export function pairedUserId(owner) {
  return `ou_${createHash('sha256').update(`user:${owner}`).digest('hex').slice(0, 32)}`;
}

const ACCEPTED_DRIVERS = new Set(['contract-mock', 'daemon']);
const REQUESTED_DRIVERS = (() => {
  const raw = process.env.HYPRIAL_ACCEPTANCE_DRIVERS;
  if (!raw || !raw.trim()) return null;
  const selected = raw.split(',').map((item) => item.trim()).filter(Boolean);
  for (const driver of selected) {
    assert.ok(ACCEPTED_DRIVERS.has(driver), `unsupported HYPRIAL_ACCEPTANCE_DRIVERS entry: ${driver}`);
  }
  return new Set(selected);
})();

export function driverEnabled(driver) {
  return REQUESTED_DRIVERS === null || REQUESTED_DRIVERS.has(driver);
}

export function recordNotRun(name, reason) {
  process.stdout.write(`NOT_RUN ${name} ${reason}\n`);
}

export function delay(milliseconds) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

export async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

export async function waitFor(predicate, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(100);
  }
  throw new Error(`timed out waiting for ${label}`);
}

export async function fileDigest(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex');
}

export function cleanEnvironment(root) {
  const env = Object.fromEntries(Object.entries(process.env)
    .filter(([name]) => !name.startsWith('HYPRIAL_') && !name.startsWith('HARNESS_')));
  return {
    ...env,
    HOME: join(root, 'user'),
    HYPRIAL_HOME: join(root, 'home'),
    HARNESS_STATE_DIR: join(root, 'state'),
    XDG_CONFIG_HOME: join(root, 'xdg-config'),
    XDG_STATE_HOME: join(root, 'xdg-state'),
    XDG_CACHE_HOME: join(root, 'xdg-cache'),
    XDG_DATA_HOME: join(root, 'xdg-data'),
    XDG_RUNTIME_DIR: join(root, 'runtime'),
    TMPDIR: join(root, 'tmp'),
    TMP: join(root, 'tmp'),
    TEMP: join(root, 'tmp'),
    npm_config_cache: join(root, 'npm-cache'),
    HYPRIAL_ACTOR_BACKEND: process.env.HYPRIAL_ACCEPTANCE_ACTOR_BACKEND || 'reference-abi',
    HYPRIAL_USER_ID: pairedUserId(OWNER),
    HYPRIAL_NETWORK_ISOLATED: '1',
    HYPRIAL_ZENOH_LISTEN: '',
    HYPRIAL_ZENOH_CONNECT: '',
    HYPRIAL_PEER_DISCOVERY: '0',
    NO_PROXY: '127.0.0.1,localhost,::1',
    no_proxy: '127.0.0.1,localhost,::1',
    UV_CACHE_DIR: join(root, 'uv-cache'),
    UV_PYTHON_DOWNLOADS: 'never',
  };
}

export async function prepareRoot(root) {
  for (const name of [
    'home', 'state', 'user', 'runtime', 'tmp', 'xdg-config', 'xdg-state',
    'xdg-cache', 'xdg-data', 'npm-cache',
  ]) {
    await mkdir(join(root, name), { recursive: true, mode: 0o700 });
  }
}

export async function prepareDaemonHome(root) {
  await run(
    PYTHON,
    [
      '-m', 'tests.support.gui_home', 'prepare',
      '--home', join(root, 'home'),
      '--state-dir', join(root, 'state'),
      '--owner', OWNER,
      '--json',
    ],
    { cwd: REPO_ROOT, env: cleanEnvironment(root) },
  );
}

export async function withRoot(prefix, operation) {
  const shortBase = await realpath(tmpdir());
  const useShort = Buffer.byteLength(join(shortBase, 'hg-XXXXXX/state/daemon-upstream.sock')) < 100
    ? shortBase
    : '/tmp';
  const root = await realpath(await mkdtemp(join(useShort, 'hg-')));
  try {
    return await operation(root);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

export async function withSourceWorktree(operation) {
  const parent = await realpath(await mkdtemp(join(tmpdir(), 'hyprial-gui-live-')));
  const tree = join(parent, 'tree');
  let cleaned = false;
  const syncCleanup = () => {
    if (cleaned) return;
    cleaned = true;
    try {
      execFileSync('git', ['-C', REPO_ROOT, 'worktree', 'remove', '--force', tree], { stdio: 'ignore' });
    } catch {
    }
    try {
      rmSync(parent, { recursive: true, force: true });
    } catch {
    }
  };
  const onSignal = (signal) => {
    process.off('SIGTERM', onSigterm);
    process.off('SIGINT', onSigint);
    syncCleanup();
    process.kill(process.pid, signal);
  };
  const onSigterm = () => onSignal('SIGTERM');
  const onSigint = () => onSignal('SIGINT');
  process.on('SIGTERM', onSigterm);
  process.on('SIGINT', onSigint);
  try {
    await run('git', ['-C', REPO_ROOT, 'worktree', 'add', '--detach', tree, 'HEAD']);
    await symlink(join(GUI_ROOT, 'node_modules'), join(tree, 'gui', 'node_modules'), 'dir');
    return await operation(join(tree, 'gui'));
  } finally {
    process.off('SIGTERM', onSigterm);
    process.off('SIGINT', onSigint);
    if (!cleaned) {
      cleaned = true;
      await run('git', ['-C', REPO_ROOT, 'worktree', 'remove', '--force', tree]).catch(() => undefined);
      await rm(parent, { recursive: true, force: true });
    }
  }
}
