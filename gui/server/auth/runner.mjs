import { StringDecoder } from 'node:string_decoder';
import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { publicProgress, publicResult } from './public-data.mjs';

const worker = fileURLToPath(new URL('./worker.py', import.meta.url));
export class AuthError extends Error {
  constructor(code) { super(code); this.code = code; }
}

/** Owns exactly one child; cancellation is drained before releasing the slot. */
export function createAuthRunner({ pythonExecutable, hyprialHome, spawnImpl = spawn, timeoutMs = 660_000, env = process.env }) {
  if (!isAbsolute(pythonExecutable ?? '') || !isAbsolute(hyprialHome ?? '')) throw new AuthError('AUTH_CONFIGURATION_INVALID');
  let active = null;
  let closed = false;
  return {
    async run(action, onProgress = () => {}) {
      if (closed) throw new AuthError('AUTH_CLOSED');
      if (!['status', 'login', 'device'].includes(action)) throw new AuthError('INVALID_ACTION');
      if (active) throw new AuthError('AUTH_BUSY');
      let resolveDone;
      const done = new Promise(resolve => { resolveDone = resolve; });
      const slot = { child: null, reason: null, done, stop: null };
      active = slot;
      const childEnv = { ...env, HYPRIAL_HOME: hyprialHome, PYTHONUNBUFFERED: '1' };
      delete childEnv.HYPRIAL_OWNER;
      delete childEnv.PYTHONPATH;
      delete childEnv.PYTHONHOME;
      let child;
      try {
        child = spawnImpl(pythonExecutable, ['-I', worker, action], {
          env: childEnv, shell: false, stdio: ['ignore', 'pipe', 'pipe'],
          windowsHide: true, detached: process.platform !== 'win32',
        });
      } catch {
        active = null; resolveDone(); throw new AuthError('AUTH_PROCESS_FAILED');
      }
      slot.child = child;
      return new Promise((resolve, reject) => {
        let settled = false;
        let buffer = '';
        const decoder = new StringDecoder('utf8');
        let bytes = 0;
        let result;
        let killTimer;
        let deadline;
        const kill = signal => {
          try {
            if (process.platform !== 'win32' && spawnImpl === spawn && child.pid) process.kill(-child.pid, signal);
            else child.kill(signal);
          } catch { /* already exited */ }
        };
        const finish = () => {
          if (settled) return;
          settled = true;
          clearTimeout(deadline); clearTimeout(killTimer);
          if (active === slot) active = null;
          resolveDone();
          if (slot.reason) reject(new AuthError(slot.reason));
          else if (!result) reject(new AuthError('AUTH_PROCESS_FAILED'));
          else resolve(result);
        };
        slot.stop = reason => {
          if (settled || slot.reason) return;
          slot.reason = reason;
          kill('SIGTERM');
          killTimer = setTimeout(() => kill('SIGKILL'), 1000);
          killTimer.unref?.();
        };
        deadline = setTimeout(() => slot.stop('AUTH_TIMEOUT'), action === 'status' ? Math.min(timeoutMs, 30_000) : timeoutMs);
        deadline.unref?.();
        child.stdout.on('data', chunk => {
          bytes += chunk.length;
          if (bytes > 1_048_576) { slot.stop('AUTH_PROTOCOL_ERROR'); return; }
          buffer += decoder.write(chunk);
          if (buffer.length > 65_536) { slot.stop('AUTH_PROTOCOL_ERROR'); return; }
          let newline;
          while ((newline = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
            if (!line.trim()) continue;
            try {
              const frame = JSON.parse(line);
              if (frame.type === 'progress' && !result) {
                const progress = publicProgress(frame.data);
                if (progress) onProgress(progress);
              } else if (frame.type === 'result' && !result) result = publicResult(frame.data);
              else throw new Error('invalid frame');
            } catch { slot.stop('AUTH_PROTOCOL_ERROR'); }
          }
        });
        // Drain stderr without retaining or forwarding secrets or tracebacks.
        child.stderr.on('data', () => {});
        child.once('error', () => { slot.reason = 'AUTH_PROCESS_FAILED'; finish(); });
        child.once('close', code => {
          buffer += decoder.end();
          if (buffer.trim()) slot.reason ??= 'AUTH_PROTOCOL_ERROR';
          if (code !== 0 && result?.ok) slot.reason ??= 'AUTH_PROCESS_FAILED';
          finish();
        });
      });
    },
    async cancel() {
      const slot = active;
      if (!slot) return;
      slot.stop?.('AUTH_CANCELLED');
      await slot.done;
    },
    async close() { closed = true; await this.cancel(); },
  };
}
