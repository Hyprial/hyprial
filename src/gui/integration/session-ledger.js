import { open, readFile, rename, unlink } from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const maps = ['sessions', 'participants', 'humanChats', 'remoteBindings', 'remoteNames', 'humanChatAliases', 'humanChatArchive'];
export function validateLedger(document) {
  if (document?.version !== 1 || !document.sessions) throw new Error('unexpected ledger schema');
  for (const key of maps) {
    if (document[key] !== undefined && (!document[key] || typeof document[key] !== 'object' || Array.isArray(document[key]))) {
      throw new Error(`unexpected ${key} schema`);
    }
  }
  for (const [adapter, binding] of Object.entries(document.remoteBindings || {})) {
    if (!binding || typeof binding.sessionId !== 'string' || typeof binding.actor !== 'string') {
      throw new Error(`invalid remote binding for ${adapter}`);
    }
    const name = document.remoteNames?.[binding.sessionId]
      || 'dsh-session-' + createHash('sha256').update(binding.sessionId).digest('hex').slice(0, 8);
    if (binding.actor.split(':').length !== 4 || binding.actor.split(':')[3] !== name) {
      throw new Error(`remote name/binding mismatch for ${adapter}; explicit recovery required`);
    }
  }
  return { ...document, ...Object.fromEntries(maps.map(key => [key, document[key] || {}])) };
}

export async function readSessionLedger(file) {
  try { return validateLedger(JSON.parse(await readFile(file, 'utf8'))); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    // A missing primary after prior use is not a fresh installation. Do not
    // silently assign a new identity or resurrect an old binding from backup.
    try { await readFile(`${file}.backup`); }
    catch (backupError) {
      if (backupError.code === 'ENOENT') return validateLedger({ version: 1, sessions: {} });
      throw backupError;
    }
    throw new Error(`session ledger missing; recovery required from ${file}.backup`);
  }
}

async function atomicWrite(file, text) {
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try {
    const handle = await open(temporary, 'wx', 0o600);
    try { await handle.writeFile(text, 'utf8'); await handle.sync(); }
    finally { await handle.close(); }
    await rename(temporary, file);
    const directory = await open(path.dirname(file), 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  } finally { await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}

// Callers hold the bridge ledger lock. Only committed primary state is backed
// up: a failed primary write never advances the backup. A failure after commit
// can leave the backup one revision behind; it must not be replayed implicitly.
export async function writeSessionLedger(file, document) {
  const text = JSON.stringify(validateLedger(document)) + '\n';
  await atomicWrite(file, text);
  await atomicWrite(`${file}.backup`, text);
}

export function mergeLegacyLedger(current, legacy) {
  const result = validateLedger(structuredClone(current));
  const source = validateLedger(legacy);
  for (const key of maps) {
    for (const [id, value] of Object.entries(source[key])) {
      if (Object.hasOwn(result[key], id) && JSON.stringify(result[key][id]) !== JSON.stringify(value)) {
        throw new Error(`legacy ledger conflicts with durable ledger: ${key}/${id}; manual recovery required`);
      }
      result[key][id] = value;
    }
  }
  return result;
}
