import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { AuthError } from '../auth/runner.mjs';

const worker = fileURLToPath(new URL('./worker.py', import.meta.url));
const ACTIONS = new Set(['inspect', 'save-key', 'create', 'configure', 'grant', 'start']);

/** Fixed trusted worker; its named operations use shell.daemon_request(_once). */
export function createSetupDaemon({ pythonExecutable, hyprialHome, env = process.env, spawnImpl = spawn, timeoutMs = 90_000 }) {
  if (!isAbsolute(pythonExecutable ?? '') || !isAbsolute(hyprialHome ?? '')) throw new AuthError('SETUP_CONFIGURATION_INVALID');
  let active = null;
  let closed = false;
  const environment = { ...env, HYPRIAL_HOME: hyprialHome, PYTHONUNBUFFERED: '1' };
  for (const name of ['HARNESS_STATE_DIR', 'HARNESS_SOCKET', 'HARNESS_SOCKET_PATH', 'HYPRIAL_OWNER', 'PYTHONPATH', 'PYTHONHOME']) delete environment[name];
  return {
    async run(action, payload = {}) {
      if (closed) throw new AuthError('SETUP_CLOSED');
      if (active) throw new AuthError('SETUP_BUSY');
      if (!ACTIONS.has(action) || !payload || typeof payload !== 'object' || Array.isArray(payload)) throw new AuthError('INVALID_ACTION');
      const child = spawnImpl(pythonExecutable, ['-I', worker, action], { env: environment, shell: false, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, detached: process.platform !== 'win32' });
      let complete;
      const done = new Promise(resolve => { complete = resolve; });
      const slot = { child, done, reason: null, stop: null };
      active = slot;
      return new Promise((resolve, reject) => {
        let output = '';
        let size = 0;
        let finished = false;
        let killer;
        const kill = signal => {
          try {
            if (spawnImpl === spawn && process.platform !== 'win32' && child.pid) process.kill(-child.pid, signal);
            else child.kill(signal);
          } catch { /* already exited */ }
        };
        slot.stop = reason => {
          if (slot.reason || finished) return;
          slot.reason = reason; kill('SIGTERM');
          killer = setTimeout(() => kill('SIGKILL'), 1000); killer.unref?.();
        };
        const timeout = setTimeout(() => slot.stop('SETUP_TIMEOUT'), timeoutMs); timeout.unref?.();
        const finish = exit => {
          if (finished) return;
          finished = true; clearTimeout(timeout); clearTimeout(killer);
          if (active === slot) active = null;
          complete();
          if (slot.reason) { reject(new AuthError(slot.reason)); return; }
          try {
            const result = JSON.parse(output);
            if (exit !== 0 || result?.ok !== true) {
              const code = result?.code;
              const error = new AuthError(typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(code) ? code : 'SETUP_COMMAND_FAILED');
              error.definitive = action === 'grant' && code === 'PROVIDER_CREDENTIAL_EXISTS' && result?.definitive === true;
              reject(error);
            } else resolve(result);
          } catch { reject(new AuthError('SETUP_COMMAND_FAILED')); }
        };
        child.stdout.setEncoding('utf8');
        child.stdout.on('data', chunk => {
          size += Buffer.byteLength(chunk);
          if (size > 1_048_576) { slot.stop('SETUP_OUTPUT_LIMIT'); return; }
          output += chunk;
        });
        child.stderr.on('data', () => {});
        child.stdin.on('error', () => {});
        child.once('error', () => { slot.reason = 'SETUP_PROCESS_FAILED'; finish(1); });
        child.once('close', finish);
        child.stdin.end(JSON.stringify(payload));
      });
    },
    async cancel() { const slot = active; if (slot) { slot.stop('SETUP_CANCELLED'); await slot.done; } },
    async close() { closed = true; await this.cancel(); },
  };
}
