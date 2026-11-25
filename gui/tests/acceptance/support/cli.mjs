/**
 * Decision record (M1/S1/S3/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - Starts the actual source `hyprial gui` and `hyprial gui dev` commands in
 *   isolated environments via `spawn(..., { detached: true })` so process-group
 *   cleanup reclaims all owned children on stop or failure.
 * - Verifies single-process Python host invariants (#2985): under the `daemon`
 *   driver `backend_pid === null`, `backend_endpoint === ''`, and no
 *   `server.json`, `frontend-authority.json`, or `request-http.json` capability
 *   files exist.
 * - Verifies private 0600 `launch.url` handoff without leaking `#grant=` or the
 *   grant token in stdout startup JSON.
 * - `startActualGui` defaults `sessionTtlMs` to `600_000` (600 s) so long
 *   acceptance scenarios do not hit session expiry unless explicitly tested.
 */

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { lstat, readFile } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import {
  GUI_ROOT,
  HYPRIAL,
  REPO_ROOT,
  cleanEnvironment,
  delay,
  exists,
  fileDigest,
  waitFor,
} from './home.mjs';
import { freePort } from './ipc-proxy.mjs';

export function launch(command, args, options) {
  return spawn(command, args, {
    cwd: REPO_ROOT,
    detached: process.platform !== 'win32',
    ...options,
  });
}

export function captureProcess(child) {
  const capture = { stdout: '', stderr: '', listeners: new Set() };
  const append = (name) => (chunk) => {
    capture[name] = (capture[name] + String(chunk)).slice(-64_000);
    for (const listener of capture.listeners) listener();
  };
  child.stdout?.setEncoding('utf8');
  child.stderr?.setEncoding('utf8');
  child.stdout?.on('data', append('stdout'));
  child.stderr?.on('data', append('stderr'));
  return capture;
}

export function logsOf(capture) {
  return `stdout:\n${capture.stdout}\nstderr:\n${capture.stderr}`.slice(-12_000);
}

export async function stopChild(child) {
  if (child === undefined || child.exitCode !== null) return;
  if (process.platform !== 'win32' && child.pid !== undefined) {
    try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill('SIGTERM'); }
  } else {
    child.kill('SIGTERM');
  }
  await Promise.race([once(child, 'exit'), delay(10_000)]);
  if (child.exitCode !== null) return;
  if (process.platform !== 'win32' && child.pid !== undefined) {
    try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
  } else {
    child.kill('SIGKILL');
  }
  await Promise.race([once(child, 'exit'), delay(3_000)]);
}

export async function waitForPath(child, path, capture, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`process exited before ${path}\n${logsOf(capture)}`);
    if (await exists(path)) return;
    await delay(100);
  }
  throw new Error(`timed out waiting for ${path}\n${logsOf(capture)}`);
}

export function waitForStartup(child, capture, timeoutMs = 180_000) {
  return new Promise((resolveStartup, rejectStartup) => {
    let consumed = 0;
    const inspect = () => {
      while (true) {
        const newline = capture.stdout.indexOf('\n', consumed);
        if (newline < 0) return;
        const line = capture.stdout.slice(consumed, newline).trim();
        consumed = newline + 1;
        if (line === '') continue;
        let payload;
        try { payload = JSON.parse(line); } catch { continue; }
        if (payload?.ok === true && Object.hasOwn(payload, 'handoff_path')) {
          settle(resolveStartup, { payload, line });
          return;
        }
      }
    };
    const timer = setTimeout(() => settle(rejectStartup, new Error(`GUI did not publish startup JSON\n${logsOf(capture)}`)), timeoutMs);
    const onExit = (code, signal) => settle(
      rejectStartup,
      new Error(`GUI exited before startup JSON (${code ?? signal})\n${logsOf(capture)}`),
    );
    const onError = (error) => settle(rejectStartup, error);
    const settle = (action, value) => {
      clearTimeout(timer);
      capture.listeners.delete(inspect);
      child.off('exit', onExit);
      child.off('error', onError);
      action(value);
    };
    capture.listeners.add(inspect);
    child.once('exit', onExit);
    child.once('error', onError);
    inspect();
  });
}

export function assertPrivateHandoff(payload, startupLine, handoffPath, launchUrl) {
  assert.equal(basename(handoffPath), 'launch.url');
  assert.equal(startupLine.includes('#grant='), false);
  assert.equal(JSON.stringify(payload).includes('#grant='), false);
  const parsed = new URL(launchUrl);
  assert.equal(`${parsed.origin}${parsed.pathname}`, payload.url);
  assert.equal(parsed.search, '');
  const grants = new URLSearchParams(parsed.hash.slice(1)).getAll('grant');
  assert.equal(grants.length, 1);
  assert.ok(grants[0].length >= 32);
  assert.equal(startupLine.includes(grants[0]), false);
  assert.equal(JSON.stringify(payload).includes(grants[0]), false);
  return grants[0];
}

export async function startActualGui({
  root, driver, mode = 'normal', ttlMs = 60_000, sessionTtlMs = 600_000, scenario = 'empty', devTools = false, liveSource = false, guiHome = GUI_ROOT,
}) {
  const live = liveSource || mode === 'dev';
  const tools = devTools || mode === 'dev';
  const port = await freePort();
  const env = cleanEnvironment(root);
  env.HYPRIAL_GUI_BACKEND_DRIVER = driver;
  env.HYPRIAL_GUI_CONTRACT_MOCK_SCENARIO = scenario;
  env.HYPRIAL_GUI_LAUNCH_TTL_MS = String(ttlMs);
  env.HYPRIAL_GUI_SESSION_TTL_MS = String(sessionTtlMs);
  const args = mode === 'dev' ? ['gui', 'dev'] : ['gui'];
  if (liveSource) args.push('--live-source');
  if (live) args.push('--gui-home', guiHome);
  if (devTools) args.push('--with-dev-tools');
  const child = launch(HYPRIAL, [...args, '--no-open', '--json', '--port', String(port)], {
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const capture = captureProcess(child);
  try {
    const { payload, line } = await waitForStartup(child, capture);
    assert.equal(payload.ok, true);
    assert.equal(payload.mode, live && tools ? 'dev' : 'normal');
    assert.equal(payload.backend_driver, driver);
    assert.equal(payload.hot_reload, live);
    assert.equal(payload.live_source, live);
    assert.equal(payload.dev_tools, tools);
    assert.equal(payload.api_docs, tools && driver === 'daemon');
    assert.equal(Object.hasOwn(payload, 'live_source_port'), false);
    if (payload.auto_worktree === null) {
      assert.equal(payload.gui_home, live ? guiHome : GUI_ROOT);
    } else {
      assert.equal(payload.auto_worktree.source, REPO_ROOT);
      assert.equal(payload.gui_home, join(payload.auto_worktree.path, 'gui'));
    }
    assert.equal(typeof payload.handoff_path, 'string');
    assert.equal(typeof payload.frontend_pid, 'number');
    if (driver === 'daemon') {
      assert.equal(payload.backend_pid, null, 'the daemon driver is served inside the host process');
      assert.equal(payload.backend_endpoint, '');
    } else {
      assert.equal(typeof payload.backend_pid, 'number');
      assert.notEqual(payload.frontend_pid, payload.backend_pid);
    }
    if (live) {
      assert.equal(typeof payload.assets_pid, 'number', 'live source owns the Vite dev server behind the Python host');
      const pids = [payload.frontend_pid, payload.backend_pid, payload.assets_pid].filter((pid) => pid !== null);
      assert.equal(new Set(pids).size, driver === 'daemon' ? 2 : 3);
    } else {
      assert.equal(payload.assets_pid, null);
    }
    const path = payload.handoff_path;
    const directory = dirname(path);
    const [fileInfo, directoryInfo] = await Promise.all([lstat(path), lstat(directory)]);
    assert.equal(fileInfo.isSymbolicLink(), false);
    assert.equal(fileInfo.isFile(), true);
    assert.equal(fileInfo.mode & 0o777, 0o600);
    assert.equal(directoryInfo.isDirectory(), true);
    assert.equal(directoryInfo.mode & 0o777, 0o700);
    const launchText = await readFile(path, 'utf8');
    assert.equal(launchText.endsWith('\n'), true);
    const launchUrl = launchText.trim();
    assert.equal(launchUrl.includes('\n'), false);
    const grant = assertPrivateHandoff(payload, line, path, launchUrl);
    const runtimeFiles = {
      requestConfig: join(directory, 'request-http.json'),
      server: join(directory, 'server.json'),
      hostLog: join(directory, 'host.log'),
      assetsLog: join(directory, 'assets.log'),
      mockLog: join(directory, 'mock.log'),
      requestHttpLog: join(directory, 'request-http.log'),
      launchPublication: join(directory, 'launch.ready'),
    };
    const secrets = [];
    if (driver === 'daemon') {
      assert.equal(await exists(runtimeFiles.server), false, 'no request-http capability file: the dispatch runs in the host');
      assert.equal(await exists(join(directory, 'frontend-authority.json')), false);
      assert.equal(await exists(runtimeFiles.requestConfig), false);
    }
    return {
      child,
      capture,
      payload,
      startupLine: line,
      handoffPath: path,
      launchUrl,
      grant,
      origin: new URL(payload.url).origin,
      runtimeDir: directory,
      runtimeFiles,
      secrets,
      buildDigest: !live
        ? await fileDigest(join(GUI_ROOT, 'dist', 'index.html'))
        : null,
    };
  } catch (error) {
    if (child.exitCode === null) await stopChild(child);
    throw error;
  }
}

export function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    throw error;
  }
}

export async function waitForPidGone(pid, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!pidAlive(pid)) return;
    await delay(100);
  }
  throw new Error(`GUI-owned child ${pid} survived shutdown`);
}

export async function terminateGuiAndCheck(gui, { daemon = null } = {}) {
  assert.equal(await exists(gui.handoffPath), false);
  const exited = once(gui.child, 'exit');
  gui.child.kill('SIGTERM');
  const [code, signal] = await Promise.race([
    exited,
    delay(20_000).then(() => { throw new Error(`GUI did not exit on SIGTERM\n${logsOf(gui.capture)}`); }),
  ]);
  assert.equal(signal, null);
  assert.equal(code, 143);
  await waitForPidGone(gui.payload.frontend_pid);
  if (gui.payload.backend_pid !== null) await waitForPidGone(gui.payload.backend_pid);
  if (gui.payload.assets_pid !== null) await waitForPidGone(gui.payload.assets_pid);
  for (const path of [...Object.values(gui.runtimeFiles), gui.handoffPath]) {
    await waitFor(async () => !(await exists(path)), `private cleanup of ${path}`, 10_000);
  }
  if (daemon !== null) {
    assert.equal(daemon.child.exitCode, null);
    assert.equal(await exists(daemon.upstreamPath), true);
  }
}
