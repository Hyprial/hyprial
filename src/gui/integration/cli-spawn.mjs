// GUI CLI capture under an existing Windows restricted token. Node/libuv's
// named pipes are denied there. Private temporary files preserve the inherited
// token and avoid changing sandbox policy or granting extra filesystem access.
import { spawn as nativeSpawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

export function spawnCli(command, args, options = {}) {
  if (process.platform !== 'win32' || process.env.HYPRIAL_DESKTOP_COMPONENTS !== '1') return nativeSpawn(command, args, options);
  return spawnCapturedCli(command, args, options);
}

// Exported for native regression probes; callers should use spawnCli.
export function spawnCapturedCli(command, args, options = {}) {
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.stdin = new PassThrough();
  let actual, cancelled = false;
  child.kill = signal => { cancelled = true; return actual ? actual.kill(signal) : true; };
  queueMicrotask(() => {
    let directory, handles = [], timer;
    const cleanup = () => {
      clearInterval(timer);
      for (const fd of handles) { try { fs.closeSync(fd); } catch {} }
      handles = [];
      if (directory) { try { fs.rmSync(directory, { recursive: true, force: true }); } catch {} }
    };
    try {
      if (cancelled) { child.stdout.end(); child.stderr.end(); child.stdin.destroy(); child.emit('close', null, 'SIGTERM'); return; }
      directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hyprial-cli-'));
      const files = ['stdin', 'stdout', 'stderr'].map(name => path.join(directory, name));
      // These GUI helpers never send CLI stdin; mutations use bounded argv/files.
      const input = child.stdin.read();
      fs.writeFileSync(files[0], input || '', { mode: 0o600 });
      handles = [fs.openSync(files[0], 'r'), fs.openSync(files[1], 'wx+', 0o600), fs.openSync(files[2], 'wx+', 0o600)];
      actual = nativeSpawn(command, args, { ...options, stdio: handles, shell: false });
      child.pid = actual.pid;
      actual.once('error', error => { child.emit('error', error); });
      const limit = 2 * 1024 * 1024;
      timer = setInterval(() => {
        try { if (handles.slice(1).some(fd => fs.fstatSync(fd).size > limit)) actual.kill(); }
        catch { actual.kill(); }
      }, 50);
      actual.once('close', (code, signal) => {
        try {
          for (const [index, stream] of [[1, child.stdout], [2, child.stderr]]) {
            const length = Math.min(fs.fstatSync(handles[index]).size, limit + 1);
            const bytes = Buffer.alloc(length);
            fs.readSync(handles[index], bytes, 0, length, 0);
            stream.end(bytes);
          }
        } catch (error) { child.emit('error', error); }
        finally { child.stdout.end(); child.stderr.end(); child.stdin.destroy(); cleanup(); child.emit('close', code, signal); }
      });
    } catch (error) { child.stdout.end(); child.stderr.end(); child.stdin.destroy(); cleanup(); child.emit('error', error); child.emit('close', null, null); }
  });
  return child;
}
