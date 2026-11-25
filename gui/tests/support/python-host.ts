/**
 * Decision record (one launch path, 2026-11-16)
- The Python session host is the only host that serves `/daemon/*`. Browser
  scripts and vitest suites that need a real session start it here, the way the
  launcher does: the product module run as a child process, the HYPRIAL_GUI_*
  inputs in its environment (the grant and the upstream bearer never in argv),
  `--port 0`, and a ready line that is read before anything connects.
- `devTools` passes the host's `--dev-tools` argument, the only way to turn the
  developer routes on; nothing in the environment does.
- Exactly one of `root` (a built GUI) and `liveUpstream` (a Vite dev server) is
  given, as the host requires. Without either a throwaway directory holding one
  stub index.html is served and removed with the host; suites that only speak
  the `/daemon/*` contract need no real build.
- Every HYPRIAL_* variable of the calling process is dropped before the host's
  own are set, so a developer's shell cannot change a test's principal, driver
  or grant. The interpreter is the repository venv (HYPRIAL_HOST_PYTHON
  overrides it, `uv run --no-sync python` is the fallback).
- close() sends SIGTERM and waits for the exit, then removes the directory it
  made. The directory is also removed on the process 'exit' event, so a worker
  that is stopped by vitest leaves nothing; a SIGKILLed worker cannot, and that
  is the only case that remains.
- The host answers on its own port, so callers read `origin` and `launchUrl()`;
  `url`, `port` and `backendConfigured` exist for browser scripts that were
  written against the Node server object this replaces.
- The test timeout for a case that starts this host is HOST_TEST_TIMEOUT_MS: a
  cold interpreter plus a real daemon dominates, the slowest measured case is
  about 16 s, and the 5 s default would fail a healthy start. Cases that wait for
  a real daemon set their own longer limits. It hides no hang.
 */

import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export interface ServingPrincipal {
  readonly owner: string;
  readonly node: string;
  readonly actor: string;
}

export interface AnonymousPrincipal {
  readonly kind: 'anonymous-bootstrap';
  readonly owner: null;
  readonly node: null;
  readonly actor: null;
}

export interface PythonHostOptions {
  readonly driver: 'daemon' | 'contract-mock';
  readonly endpoint: string;
  readonly upstreamToken: string;
  readonly principal: ServingPrincipal | AnonymousPrincipal;
  readonly launchGrant?: string;
  readonly root?: string;
  readonly liveUpstream?: string;
  readonly devTools?: boolean;
  readonly port?: number;
  readonly handoffPath?: string | null;
  readonly publicationPath?: string | null;
  readonly launchTtlMs?: number;
  readonly sessionTtlMs?: number;
  readonly maxSessions?: number;
}

export interface PythonHost {
  readonly origin: string;
  readonly url: string;
  readonly port: number;
  readonly listenAddress: string;
  readonly backendConfigured: true;
  readonly launchGrant: string;
  readonly launchUrl: (path?: string) => string;
  readonly advance: (milliseconds: number) => Promise<void>;
  readonly close: () => Promise<void>;
}

export const HOST_TEST_TIMEOUT_MS = 30_000;
const READY_PREFIX = 'HYPRIAL_GUI_READY ';
const START_TIMEOUT_MS = 30_000;
const STOP_TIMEOUT_MS = 10_000;
const REPOSITORY_ROOT = resolve(fileURLToPath(new URL('../../..', import.meta.url)));
const pendingDirectories = new Set<string>();

process.once('exit', () => {
  for (const directory of pendingDirectories) rmSync(directory, { recursive: true, force: true });
});

function pythonCommand(env: NodeJS.ProcessEnv): string[] {
  const configured = env.HYPRIAL_HOST_PYTHON?.trim();
  if (configured) return [configured];
  const venv = join(REPOSITORY_ROOT, '.venv', 'bin', 'python');
  return existsSync(venv) ? [venv] : ['uv', 'run', '--no-sync', 'python'];
}

export function hostEnvironment(
  options: PythonHostOptions,
  launchGrant: string,
  parent: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(parent)) {
    if (!name.startsWith('HYPRIAL_')) env[name] = value;
  }
  env.HYPRIAL_GUI_BACKEND_DRIVER = options.driver;
  env.HYPRIAL_BACKEND_ENDPOINT = options.endpoint;
  env.HYPRIAL_GUI_UPSTREAM_TOKEN = options.upstreamToken;
  env.HYPRIAL_GUI_LAUNCH_GRANT = launchGrant;
  if (options.launchTtlMs !== undefined) env.HYPRIAL_GUI_LAUNCH_TTL_MS = String(options.launchTtlMs);
  if (options.sessionTtlMs !== undefined) env.HYPRIAL_GUI_SESSION_TTL_MS = String(options.sessionTtlMs);
  if (options.maxSessions !== undefined) env.HYPRIAL_GUI_MAX_SESSIONS = String(options.maxSessions);
  if ('actor' in options.principal && options.principal.actor !== null) {
    env.HYPRIAL_GUI_PRINCIPAL = options.principal.actor;
    env.HYPRIAL_NODE_ID = options.principal.node;
  }
  if (options.handoffPath) env.HYPRIAL_GUI_HANDOFF_PATH = options.handoffPath;
  if (options.publicationPath) env.HYPRIAL_GUI_PUBLICATION_PATH = options.publicationPath;
  env.PYTHONUNBUFFERED = '1';
  return env;
}

export function hostArguments(options: PythonHostOptions, root: string | undefined): string[] {
  if (options.root !== undefined && options.liveUpstream !== undefined) {
    throw new Error('startPythonHost takes a root or a liveUpstream, not both');
  }
  const served = options.liveUpstream !== undefined
    ? ['--live-upstream', options.liveUpstream]
    : ['--root', root ?? ''];
  const tools = options.devTools === true ? ['--dev-tools'] : [];
  return ['-m', 'hyprial.shell.impl.gui.host', 'serve', '--port', String(options.port ?? 0), ...served, ...tools];
}

export async function startPythonHost(options: PythonHostOptions): Promise<PythonHost> {
  const launchGrant = options.launchGrant ?? randomBytes(32).toString('hex');
  let directory: string | undefined;
  let root = options.root;
  if (root === undefined && options.liveUpstream === undefined) {
    directory = mkdtempSync(join(tmpdir(), 'gui-python-host-'));
    writeFileSync(join(directory, 'index.html'), '<!doctype html><title>Hyprial</title>');
    root = directory;
    pendingDirectories.add(directory);
  }
  const removeDirectory = (): void => {
    if (directory === undefined) return;
    pendingDirectories.delete(directory);
    rmSync(directory, { recursive: true, force: true });
  };
  let args: string[];
  try {
    args = hostArguments(options, root);
  } catch (error) {
    removeDirectory();
    throw error;
  }
  const [command, ...prefix] = pythonCommand(process.env);
  const child = spawn(command, [...prefix, ...args], {
    env: hostEnvironment(options, launchGrant),
    stdio: ['ignore', 'pipe', 'pipe'],
    cwd: REPOSITORY_ROOT,
  });
  const exited = new Promise<number | null>((done) => {
    child.once('close', (code) => { done(code); });
    child.once('error', () => { removeDirectory(); done(null); });
  });
  let stderr = '';
  let stdout = '';
  child.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
  const stop = async (): Promise<void> => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    const killer = setTimeout(() => { child.kill('SIGKILL'); }, STOP_TIMEOUT_MS);
    try {
      await exited;
    } finally {
      clearTimeout(killer);
      removeDirectory();
    }
  };
  const ready = await new Promise<{ readonly url: string; readonly host: string; readonly port: number }>(
    (resolveReady, rejectReady) => {
      const timer = setTimeout(() => { rejectReady(new Error('python host did not become ready')); }, START_TIMEOUT_MS);
      child.stdout.on('data', (chunk: Buffer) => {
        stdout += chunk.toString();
        const line = stdout.split('\n').find((candidate) => candidate.startsWith(READY_PREFIX));
        if (line === undefined) return;
        clearTimeout(timer);
        resolveReady(JSON.parse(line.slice(READY_PREFIX.length)) as { url: string; host: string; port: number });
      });
      void exited.then((code) => {
        clearTimeout(timer);
        rejectReady(new Error(`python host exited with ${code} before it was ready: ${stderr.trim()}`));
      });
    },
  ).catch(async (error: unknown) => {
    await stop();
    throw error;
  });
  return {
    origin: ready.url,
    url: ready.url,
    port: ready.port,
    listenAddress: ready.host,
    backendConfigured: true,
    launchGrant,
    launchUrl: (path = '/') => `${ready.url}${path}#grant=${launchGrant}`,
    advance: (milliseconds) => new Promise((resolveAdvance) => { setTimeout(resolveAdvance, milliseconds); }),
    close: stop,
  };
}
