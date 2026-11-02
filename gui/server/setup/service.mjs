import { randomUUID } from 'node:crypto';
import { AuthError } from '../auth/runner.mjs';
import { createSetupDaemon } from './daemon.mjs';
import { createSetupCheckpoint, createMemorySetupCheckpoint } from './checkpoint.mjs';

const PROVIDER = 'deepseek';
const MODEL = 'deepseek-flash';
const codeError = code => { throw new AuthError(code); };
function compatible(agent) {
  return agent.provider === PROVIDER && agent.model === MODEL && agent.preferredHarness === 'pi' && !agent.hosted;
}
function isRunning(agent) { return agent?.status === 'online' || agent?.online === true || agent?.status === 'running'; }
function keyInput(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 4096 && value === value.trim() && !/[\u0000-\u0020\u007f]/.test(value);
}

/** Ordered public daemon operations. Durable provenance contains IDs only, never keys. */
export function createSetupService(options) {
  const daemon = options.daemon ?? createSetupDaemon(options);
  const checkpoint = options.checkpoint ?? (options.hyprialHome ? createSetupCheckpoint(options.hyprialHome) : createMemorySetupCheckpoint());
  let operation = null;
  let closed = false;
  const inspect = async () => {
    const result = await daemon.run('inspect');
    if (!Array.isArray(result.agents)) codeError('SETUP_INVALID_RESPONSE');
    const found = result.agents.filter(agent => agent?.actor === 'squire');
    if (found.length > 1) codeError('SQUIRE_AMBIGUOUS');
    return found[0] ?? null;
  };
  return {
    async run(action, input, progress, _sessionId) {
      if (closed) codeError('SETUP_CLOSED');
      if (operation) codeError('SETUP_BUSY');
      if (!['setup-status', 'model-save', 'squire-prepare'].includes(action)) codeError('INVALID_ACTION');
      let finishOperation;
      const current = { cancelled: false, done: new Promise(resolve => { finishOperation = resolve; }) };
      let release;
      const stages = { created: false, granted: false, started: false };
      operation = current;
      const check = () => { if (current.cancelled) codeError('SETUP_CANCELLED'); };
      let saved;
      const persist = async () => { if (saved) await checkpoint.write(saved); };
      try {
        if (action === 'setup-status') {
          if (await checkpoint.busy?.()) codeError('SETUP_BUSY');
        } else release = await checkpoint.acquire();
        saved = await checkpoint.read();
        if (action === 'setup-status') {
          const existing = await inspect(); check();
          const owned = !!existing && saved?.squireUri === existing.uri && saved?.squireCreatedAt === existing.createdAtMs;
          return { ok: true, created: !!existing, started: isRunning(existing), compatible: existing ? compatible(existing) : true,
            keySaved: saved?.keySaved ?? null, granted: null, grantRecorded: owned ? saved.granted : null, grantVerified: false,
            resumeAvailable: !!saved?.keySaved && ((!existing && !saved.squireUri) || owned) && !saved.pendingAction,
            pendingAction: saved?.pendingAction ?? null, modelVerified: false };
        }
        if (action === 'model-save') {
          if (!keyInput(input?.key) || Object.keys(input).length !== 1) codeError('INVALID_MODEL_KEY');
          if (saved?.granted) codeError('MODEL_KEY_ALREADY_GRANTED');
          if (saved?.pendingAction) codeError('SETUP_REVIEW_REQUIRED');
          const entry = `gui-deepseek-${randomUUID()}`;
          saved = { ...saved, entry, granted: false, keySaved: false, pendingAction: 'save' };
          await persist();
          try { await daemon.run('save-key', { entry, key: input.key }); }
          finally { input.key = ''; }
          check();
          saved.keySaved = true; delete saved.pendingAction;
          await persist();
          return { ok: true, saved: true, modelVerified: false };
        }
        if (!saved?.keySaved) codeError('MODEL_KEY_REQUIRED');
        if (saved.pendingAction) codeError('SETUP_REVIEW_REQUIRED');
        let existing = await inspect(); check();
        if (!existing && saved.squireUri) codeError('SQUIRE_IDENTITY_CHANGED');
        if (existing && !compatible(existing)) codeError('SQUIRE_CONFIGURATION_CONFLICT');
        if (existing && (saved.squireUri !== existing.uri || saved.squireCreatedAt !== existing.createdAtMs)) codeError('SQUIRE_EXISTING_REVIEW_REQUIRED');
        stages.created = !!existing;
        stages.granted = saved.granted;
        if (existing && isRunning(existing) && !saved.granted) codeError('SQUIRE_RUNNING_REVIEW_REQUIRED');
        if (!existing) {
          saved.pendingAction = 'create'; await persist();
          const receipt = await daemon.run('create'); check();
          const created = receipt.agent;
          if (receipt.created !== true || !created || created.actor !== 'squire' || !compatible(created) || typeof created.uri !== 'string' || !Number.isFinite(created.createdAtMs)) codeError('SQUIRE_CREATION_UNVERIFIED');
          saved.squireUri = created.uri;
          saved.squireCreatedAt = created.createdAtMs;
          stages.created = true;
          delete saved.pendingAction; await persist();
        }
        const inspectOwned = async () => {
          const observed = await inspect(); check();
          if (!observed || !compatible(observed) || observed.uri !== saved.squireUri || observed.createdAtMs !== saved.squireCreatedAt) codeError('SQUIRE_IDENTITY_CHANGED');
          return observed;
        };
        existing = await inspectOwned();
        if (!saved.configured) {
          saved.pendingAction = 'configure'; await persist();
          await daemon.run('configure'); check();
          existing = await inspectOwned();
          saved.configured = true; delete saved.pendingAction; await persist();
        }
        stages.created = true;
        progress({ stage: 'created' }); check();
        existing = await inspectOwned();
        if (!saved.granted) {
          saved.pendingAction = 'grant'; await persist();
          await daemon.run('grant', { entry: saved.entry }); check();
          saved.granted = true; delete saved.pendingAction; await persist();
        }
        stages.granted = true;
        progress({ stage: 'granted' }); check();
        existing = await inspectOwned();
        if (!isRunning(existing)) {
          saved.pendingAction = 'start'; await persist();
          const start = await daemon.run('start'); check();
          if (start.started !== true) codeError('SQUIRE_START_UNVERIFIED');
        }
        delete saved.pendingAction; stages.started = true; await persist();
        progress({ stage: 'started' });
        return { ok: true, created: true, granted: true, grantRecorded: true, grantVerified: false, started: true, modelVerified: false };
      } catch (error) {
        if (error?.definitive && saved) {
          delete saved.pendingAction;
          try { await persist(); } catch { return { ok: false, code: 'SETUP_CHECKPOINT_UNSAFE', ...stages, modelVerified: false }; }
        }
        const code = typeof error?.code === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(error.code) ? error.code : 'SETUP_FAILED';
        return { ok: false, code, ...stages, modelVerified: false };
      } finally {
        try { await release?.(); }
        finally { if (operation === current) operation = null; finishOperation(); }
      }
    },
    async cancel() { const current = operation; if (current) current.cancelled = true; await daemon.cancel(); await current?.done; },
    async close() { closed = true; await this.cancel(); await daemon.close(); },
  };
}
