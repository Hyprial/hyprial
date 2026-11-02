import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { randomUUID } from 'node:crypto';

export function createOwnedBackend(env = process.env, spawnChild = spawn, timings = {}) {
  const python = env.HYPRIAL_GUI_PYTHON;
  if (!python || !isAbsolute(python) || !env.HYPRIAL_HOME || !env.HYPRIAL_GUI_PRINCIPAL) {
    throw new Error('Live GUI requires HYPRIAL_GUI_PYTHON (absolute), HYPRIAL_HOME and HYPRIAL_GUI_PRINCIPAL');
  }
  const childEnv = Object.fromEntries([
    'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'TEMP', 'TMP', 'LANG',
    'HYPRIAL_HOME', 'HARNESS_STATE_DIR', 'HYPRIAL_OWNER', 'HYPRIAL_GUI_PRINCIPAL',
  ].filter(key => env[key] !== undefined).map(key => [key, env[key]]));
  Object.assign(childEnv, {HYPRIAL_GUI_BACKEND_DRIVER: 'daemon', PYTHONUNBUFFERED: '1'});
  const child = spawnChild(python, ['-c', 'from hyprial.shell import run_gui_backend; run_gui_backend()'],
    {env: childEnv, stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true});
  const pending = new Map();
  let buffer = '';
  let bufferBytes = 0;
  let closed = false;
  let terminationStarted = false;
  let closePromise;
  let readyResolve, readyReject;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  // Prevent startup rejection becoming an unhandled rejection before HTTP calls.
  ready.catch(() => {});
  const readyTimer = setTimeout(() => {
    fail(new Error('GUI backend readiness timeout'));
    terminate();
  }, timings.readyMs ?? 15000);
  function terminate() {
    if (terminationStarted) return;
    terminationStarted = true;
    child.kill('SIGTERM');
    const timer = setTimeout(() => child.kill('SIGKILL'), timings.killMs ?? 3000);
    child.once('close', () => clearTimeout(timer));
  }
  function fail(error) {
    clearTimeout(readyTimer);
    readyReject(error);
    for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(error); }
    pending.clear();
    closed = true;
  }
  child.once('error', () => fail(new Error('GUI backend unavailable')));
  child.once('exit', () => fail(new Error('GUI backend exited')));
  child.stdin.on('error', () => { fail(new Error('GUI backend unavailable')); terminate(); });
  child.stdout.on('error', () => { fail(new Error('GUI backend unavailable')); terminate(); });
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    buffer += chunk;
    bufferBytes += Buffer.byteLength(chunk);
    // Recovery can contain 100 original requests, each admitted by the
    // 128 KiB HTTP limit. History uses small pages below daemon's 8 MiB cap.
    if (bufferBytes > 16777216) { fail(new Error('GUI backend response limit')); terminate(); return; }
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
      bufferBytes -= Buffer.byteLength(line) + 1;
      let frame;
      try { frame = JSON.parse(line); } catch { fail(new Error('Invalid GUI backend response')); terminate(); return; }
      if (!frame || typeof frame !== 'object' || Array.isArray(frame)) {
        fail(new Error('Invalid GUI backend response')); terminate(); return;
      }
      if (frame.id === 'ready' && frame.ok === true) { clearTimeout(readyTimer); readyResolve(); continue; }
      const entry = pending.get(frame.id);
      if (!entry) continue; // Timed-out writes remain queryable, never replayed.
      pending.delete(frame.id); clearTimeout(entry.timer);
      delete frame.id;
      if (typeof frame.ok !== 'boolean') entry.reject(new Error('Invalid GUI backend response'));
      else entry.resolve(frame);
    }
  });
  return {
    async call(frame) {
      await ready;
      if (closed || pending.size >= 16) throw new Error('GUI backend unavailable or busy');
      const id = randomUUID();
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(new Error('GUI backend outcome unknown')); }, timings.callMs ?? 30000);
        pending.set(id, {resolve, reject, timer});
        child.stdin.write(JSON.stringify({id, ...frame}) + '\n', error => {
          if (error) { const entry = pending.get(id); pending.delete(id); clearTimeout(timer); entry?.reject(error); }
        });
      });
    },
    async close() {
      if (closePromise) return closePromise;
      fail(new Error('GUI backend closed'));
      if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
      closePromise = new Promise(resolve => {
        child.once('close', resolve);
        child.stdin.end();
        terminate();
      });
      return closePromise;
    },
  };
}
