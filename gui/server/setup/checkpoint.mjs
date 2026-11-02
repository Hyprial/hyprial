import { constants } from 'node:fs';
import { lstat, realpath, mkdir, open, rename, unlink, rmdir } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { AuthError } from '../auth/runner.mjs';

const NAME = 'pi-deepseek.json';
const GRANT = 'gui-first-run-pi-deepseek';
const ACTIONS = new Set([null, 'save', 'create', 'configure', 'grant', 'start']);
const KEYS = new Set(['schemaVersion', 'home', 'entryId', 'grantId', 'actorUri', 'actorCreatedAtMs', 'configured', 'granted', 'keySaved', 'pendingAction', 'updatedAtMs']);
function unsafe() { throw new AuthError('SETUP_CHECKPOINT_UNSAFE'); }
function valid(record, home) {
  if (!record || typeof record !== 'object' || Array.isArray(record) || Object.keys(record).some(key => !KEYS.has(key))) unsafe();
  if (record.schemaVersion !== 1 || record.home !== home || !/^gui-deepseek-[a-f0-9-]{36}$/.test(record.entryId ?? '') || record.grantId !== GRANT) unsafe();
  if (![record.configured, record.granted, record.keySaved].every(value => typeof value === 'boolean') || !ACTIONS.has(record.pendingAction) || !Number.isFinite(record.updatedAtMs)) unsafe();
  if (record.actorUri !== null && (typeof record.actorUri !== 'string' || !/^agent:[^\s:]+:[^\s:]+:squire$/.test(record.actorUri))) unsafe();
  if ((record.actorUri === null) !== (record.actorCreatedAtMs === null) || (record.actorCreatedAtMs !== null && (!Number.isSafeInteger(record.actorCreatedAtMs) || record.actorCreatedAtMs < 0))) unsafe();
  if ((record.configured && !record.actorUri) || (record.granted && (!record.configured || !record.keySaved))) unsafe();
  return record;
}
function trusted(stat, directory) {
  if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile())) unsafe();
  if (process.platform !== 'win32') {
    if (stat.uid !== process.getuid() || (stat.mode & 0o022) || (!directory && (stat.mode & 0o077))) unsafe();
  }
}

/** Durable provenance only. Never stores model credentials or provider responses. */
export function createSetupCheckpoint(hyprialHome) {
  if (!isAbsolute(hyprialHome ?? '')) unsafe();
  async function paths(create) {
    let canonical;
    try { trusted(await lstat(hyprialHome), true); canonical = await realpath(hyprialHome); }
    catch (error) { if (error.code === 'ENOENT' && !create) return null; throw error; }
    const directory = join(canonical, 'state', 'gui-first-run');
    for (const path of [join(canonical, 'state'), directory]) {
      try { trusted(await lstat(path), true); }
      catch (error) {
        if (error.code !== 'ENOENT') throw error;
        if (!create) return null;
        await mkdir(path, { mode: 0o700 });
        trusted(await lstat(path), true);
      }
    }
    return { home: canonical, directory, file: join(directory, NAME) };
  }
  return {
    async acquire() {
      const location = await paths(true);
      const lock = join(location.directory, 'operation.lock');
      try { await mkdir(lock, { mode: 0o700 }); }
      catch (error) { if (error.code === 'EEXIST') throw new AuthError('SETUP_BUSY'); throw error; }
      // Unknown stale locks require operator reconciliation, never automatic reap.
      const token = randomUUID();
      const owner = join(lock, 'owner.json');
      const handle = await open(owner, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0), 0o600);
      try { await handle.writeFile(JSON.stringify({ token, pid: process.pid, createdAtMs: Date.now() })); await handle.sync(); }
      finally { await handle.close(); }
      return async () => {
        trusted(await lstat(lock), true);
        const current = await open(owner, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
        try {
          trusted(await current.stat(), false);
          if (JSON.parse(await current.readFile('utf8')).token !== token) unsafe();
        } finally { await current.close(); }
        await unlink(owner); await rmdir(lock);
      };
    },
    async busy() {
      const location = await paths(false);
      if (!location) return false;
      try { await lstat(join(location.directory, 'operation.lock')); return true; }
      catch (error) { if (error.code === 'ENOENT') return false; throw error; }
    },
    async read() {
      const location = await paths(false);
      if (!location) return null;
      let handle;
      try {
        trusted(await lstat(location.file), false);
        handle = await open(location.file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
        const stat = await handle.stat(); trusted(stat, false);
        if (stat.size > 8192) unsafe();
        const record = valid(JSON.parse(await handle.readFile('utf8')), location.home);
        return {
          entry: record.entryId, squireUri: record.actorUri ?? undefined,
          squireCreatedAt: record.actorCreatedAtMs ?? undefined,
          configured: record.configured, granted: record.granted, keySaved: record.keySaved,
          ...(record.pendingAction ? { pendingAction: record.pendingAction } : {}),
        };
      } catch (error) {
        if (error.code === 'ENOENT') return null;
        if (error instanceof AuthError) throw error;
        throw new AuthError('SETUP_CHECKPOINT_UNSAFE');
      } finally { await handle?.close(); }
    },
    async write(saved) {
      const location = await paths(true);
      const record = valid({ schemaVersion: 1, home: location.home, entryId: saved.entry, grantId: GRANT,
        actorUri: saved.squireUri ?? null, actorCreatedAtMs: saved.squireCreatedAt ?? null,
        configured: saved.configured === true, granted: saved.granted === true, keySaved: saved.keySaved === true,
        pendingAction: saved.pendingAction ?? null, updatedAtMs: Date.now() }, location.home);
      try { trusted(await lstat(location.file), false); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      const temporary = join(location.directory, `.pi-${randomUUID()}.tmp`);
      let handle;
      try {
        handle = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0), 0o600);
        await handle.writeFile(`${JSON.stringify(record)}\n`, 'utf8'); await handle.sync();
        await handle.close(); handle = null;
        trusted(await lstat(location.directory), true);
        await rename(temporary, location.file);
        if (process.platform !== 'win32') {
          const directory = await open(location.directory, constants.O_RDONLY);
          try { await directory.sync(); } finally { await directory.close(); }
        }
      } finally { await handle?.close(); await unlink(temporary).catch(() => {}); }
    },
  };
}

/** Tests can inject this volatile store instead of touching any real home. */
export function createMemorySetupCheckpoint() {
  let value = null;
  let locked = false;
  return {
    async acquire() { if (locked) throw new AuthError('SETUP_BUSY'); locked = true; return async () => { locked = false; }; },
    async busy() { return locked; },
    async read() { return value ? structuredClone(value) : null; },
    async write(saved) { value = structuredClone(saved); },
  };
}
