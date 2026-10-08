import { mkdir, lstat, readFile, writeFile, rename, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import lockfile from 'proper-lockfile';

export function createFsPersistence(root) {
  const storePath = join(root, 'store.json');
  let compromised;
  return {
    async read() {
      let stat;
      try { stat = await lstat(storePath); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
      if (!stat.isFile() || stat.size > 8 * 1024 * 1024) throw Object.assign(new Error('Invalid GUI store'), { code: 'GUI_STORE_ERROR' });
      return JSON.parse(await readFile(storePath, 'utf8'));
    },
    async write(bytes) {
      const temp = `${storePath}.${randomUUID()}.tmp`;
      try { await writeFile(temp, bytes, { mode: 0o600, flag: 'wx' }); if (compromised) throw compromised; await rename(temp, storePath); }
      finally { await rm(temp, { force: true }); }
    },
    async lock(run) {
      compromised = undefined;
      await mkdir(root, { recursive: true, mode: 0o700 });
      const stat = await lstat(root);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw Object.assign(new Error('GUI root must be a real directory'), { code: 'GUI_INVALID_ARGUMENT' });
      const releaseLock = await lockfile.lock(root, { lockfilePath: join(root, '.write-lock'), stale: 30000, update: 5000, retries: { retries: 30, minTimeout: 20, maxTimeout: 300 }, onCompromised(error) { compromised = error; } });
      try { return await run(); } finally { await releaseLock(); }
    },
  };
}
