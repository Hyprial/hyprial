import { open, readFile, rename, unlink } from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const maps = ['sessions', 'participants', 'receptionPolicies', 'humanChats', 'remoteBindings', 'remoteNames', 'humanChatAliases', 'humanChatArchive', 'sessionBindings'];
export function validateLedger(document) {
  if (document?.version !== 1 || !document.sessions) throw new Error('unexpected ledger schema');
  // Reception policy is stored per ledgerKey(identity). The only value this
  // repository writes is 'whitelist'; readers treat absence (or any other
  // persisted value) as the default 'open' network Agent reception.
  for (const key of maps) {
    if (document[key] !== undefined && (!document[key] || typeof document[key] !== 'object' || Array.isArray(document[key]))) {
      throw new Error(`unexpected ${key} schema`);
    }
  }
  for (const [adapter, binding] of Object.entries(document.remoteBindings || {})) {
    if (!binding || typeof binding.sessionId !== 'string' || typeof binding.actor !== 'string') {
      throw new Error(`invalid remote binding for ${adapter}`);
    }
    const selected = document.sessionBindings?.[binding.sessionId];
    const name = selected?.actor?.split(':')[3] || document.remoteNames?.[binding.sessionId]
      || 'dsh-session-' + createHash('sha256').update(binding.sessionId).digest('hex').slice(0, 8);
    if (binding.actor.split(':').length !== 4 || binding.actor.split(':')[3] !== name) {
      throw new Error(`remote name/binding mismatch for ${adapter}; explicit recovery required`);
    }
  }
  for (const [id, binding] of Object.entries(document.sessionBindings || {})) {
    const validIdentity = value => value && typeof value.actor === 'string' && value.actor.startsWith('agent:') && value.actor.split(':').length === 4 && value.actor.split(':').every(Boolean) && ['dsh-web:' + id, 'dsh-remote:' + id].includes(value.sessionRef);
    if (!validIdentity(binding) || typeof binding.enabled !== 'boolean') throw new Error('invalid session binding: ' + id);
    if (binding.legacyIdentities !== undefined && (!Array.isArray(binding.legacyIdentities) || binding.legacyIdentities.some(alias => !validIdentity(alias) || alias.actor.split(':').slice(0, 3).join(':') !== binding.actor.split(':').slice(0, 3).join(':')))) throw new Error('invalid legacy identities: ' + id);
    if (document.remoteNames?.[id] && document.remoteNames[id] !== binding.actor.split(':')[3]) throw new Error('session name/binding mismatch: ' + id);
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
