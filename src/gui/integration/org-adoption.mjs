import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { open, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';

export const ORG_ADOPTION_OPERATIONS = Object.freeze([
  'org-management-status', 'org-fetch', 'org-import-preview', 'org-import'
]);
const LIMIT = 256 * 1024;
const TTL = 5 * 60 * 1000;
const sha = value => createHash('sha256').update(value).digest('hex');
const record = value => !!value && typeof value === 'object' && !Array.isArray(value);
function fail(code, message, details) {
  throw Object.assign(new Error(message), { code, ...(details ? { details } : {}) });
}

export function fullOrgStatus(value) {
  return record(value) && value.ok === true && value.partial !== true
    && ['accepted', 'absent'].includes(value.slot)
    && Array.isArray(value.pending) && value.pending.every(record)
    && Number.isInteger(value.pendingCount) && value.pendingCount >= 0
    && value.pendingCount === value.pending.length
    && (value.slot === 'absent' || record(value.accepted));
}

// acceptedPath is supplied once by the trusted Host for its fixed H2B_HOME.
// Never accept a path or identity from a browser request.
export async function acceptedOrgDigest(acceptedPath) {
  if (!isAbsolute(acceptedPath)) fail('INVALID_ARGUMENT', 'accepted organization path must be absolute');
  let file;
  try {
    file = await open(acceptedPath, constants.O_RDONLY | constants.O_NOFOLLOW);
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > LIMIT) fail('ORG_INVALID_BASE', 'accepted organization must be a regular bounded file');
    const bytes = await file.readFile();
    if (bytes.length > LIMIT) fail('ORG_INVALID_BASE', 'accepted organization exceeds safety limit');
    return sha(bytes);
  } catch (error) {
    if (error.code === 'ENOENT') return 'absent';
    throw error;
  } finally { await file?.close(); }
}

function readResult(result) {
  if (result?.timedOut) fail('COMMAND_TIMEOUT', 'organization CLI timed out; inspect status before retrying');
  if (result?.overflow) fail('OUTPUT_TOO_LARGE', 'organization CLI output exceeded safety limit');
  let value;
  try { value = JSON.parse(result.stdout); } catch { fail('INVALID_RESPONSE', 'organization CLI returned invalid JSON'); }
  if (!record(value)) fail('INVALID_RESPONSE', 'organization CLI returned an invalid document');
  return value;
}
function resultError(result, value) {
  const nested = record(value.error) ? value.error : {};
  fail(value.code || nested.code || 'H2B_COMMAND_FAILED', String(nested.message || value.error || result.stderr || 'organization CLI failed').slice(0, 500), value.data || nested.details);
}
function inputText(input) {
  if (typeof input.text !== 'string' || !input.text.trim() || input.text.includes('\0') || Buffer.byteLength(input.text) > LIMIT) {
    fail('INVALID_ARGUMENT', 'organization text is required, without NUL, at most 256 KiB');
  }
  return input.text;
}
function fields(input, allowed) {
  if (!record(input) || Object.keys(input).some(key => !allowed.includes(key))) fail('INVALID_ARGUMENT', 'unsupported organization request fields');
}

/**
 * A Host-resident controller: one-shot previews are memory-only and disappear
 * on Host restart. runCli is the existing bounded fixed-argv executor; it must
 * retain the Host's identity/home and may not fall back to broader write roots.
 * readOnlyStatus must use the Host's existing read-only sandboxed query. It must
 * NOT run org status using the write runner. Current CLI chmod/mkdir will yield
 * a partial fallback there, so mutation stays closed until H2B fixes the query.
 */
export function createOrgAdoptionController({ runCli, readOnlyStatus, acceptedPath, now = Date.now, temporaryRoot = tmpdir() }) {
  if (typeof runCli !== 'function' || typeof readOnlyStatus !== 'function' || !isAbsolute(acceptedPath)) fail('INVALID_ARGUMENT', 'trusted organization runner/read-only query/path required');
  const tokens = new Map();
  let busy = false;
  async function status() {
    let value;
    try { value = await readOnlyStatus(); }
    catch (error) { fail('ORG_READ_ONLY_REQUIRED', '只读组织查询失败，禁止获取/采纳；不得扩大写权限。', { causeCode: error.code || 'QUERY_FAILED' }); }
    if (!fullOrgStatus(value)) fail('ORG_READ_ONLY_REQUIRED', '完整只读组织状态不可用；partial 采纳槽位降级不能用于获取或采纳。请先修复 H2B org status 读路径，不扩大目录写权限。');
    return value;
  }
  async function withText(text, action) {
    const dir = await mkdtemp(join(temporaryRoot, 'dsh-org-import-'));
    try {
      const file = join(dir, 'incoming.md');
      await writeFile(file, text, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
      return await action(file);
    } finally { await rm(dir, { recursive: true, force: true }); }
  }
  return async function handleOrgAdoption(input) {
    const operation = input?.operation;
    if (!ORG_ADOPTION_OPERATIONS.includes(operation)) fail('UNSUPPORTED_OPERATION', 'unsupported organization operation');
    if (busy) fail('ORG_BUSY', 'another organization operation is in progress');
    busy = true;
    try {
      for (const [token, value] of tokens) if (value.expiresAt <= now()) tokens.delete(token);
      if (operation === 'org-management-status') {
        fields(input, ['operation']);
        try {
          const document = await status();
          return { ok: true, operation, document: { enabled: true, status: document } };
        } catch (error) {
          return { ok: true, operation, document: { enabled: false, code: error.code || 'ORG_READ_ONLY_REQUIRED', message: error.message } };
        }
      }
      if (operation === 'org-fetch') {
        fields(input, ['operation', 'target', 'timeout', 'confirmed']);
        if (input.confirmed !== true) fail('CONFIRMATION_REQUIRED', 'organization fetch writes pending candidates and requires confirmation');
        if (typeof input.target !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(input.target)) fail('INVALID_ARGUMENT', 'an exact reachable node target is required');
        if (typeof input.timeout !== 'number' || !Number.isFinite(input.timeout) || input.timeout <= 0 || input.timeout > 20) fail('INVALID_ARGUMENT', 'timeout must be greater than 0 and at most 20 seconds');
        await status();
        const result = await runCli(['org', 'fetch', '--from', input.target, '--timeout', String(input.timeout), '--json']);
        const document = readResult(result);
        if (result.status !== 0 || document.ok !== true) resultError(result, document);
        if (!Array.isArray(document.candidates)) fail('INVALID_RESPONSE', 'organization fetch did not return candidates');
        return { ok: true, operation, document };
      }
      fields(input, operation === 'org-import-preview' ? ['operation', 'text'] : ['operation', 'text', 'previewToken', 'confirmed']);
      const text = inputText(input);
      const digest = sha(text);
      if (operation === 'org-import-preview') {
        const current = await status();
        const baseDigest = await acceptedOrgDigest(acceptedPath);
        const result = await withText(text, file => runCli(['org', 'import', file, '--json']));
        const value = readResult(result);
        if (result.status === 0 || value.ok !== false || value.code !== 'CONFIRMATION_REQUIRED') {
          if (result.status !== 0) resultError(result, value);
          fail('INVALID_RESPONSE', 'organization preview must require confirmation and must not adopt');
        }
        if (!record(value.data) || !record(value.data.meta) || !record(value.data.source) || typeof value.data.diff !== 'string') fail('INVALID_RESPONSE', 'organization CLI confirmation preview is incomplete');
        if (baseDigest !== await acceptedOrgDigest(acceptedPath)) fail('ORG_BASE_CHANGED', 'accepted organization changed during preview; preview again');
        while (tokens.size >= 16) tokens.delete(tokens.keys().next().value);
        const previewToken = randomUUID();
        const expiresAt = now() + TTL;
        tokens.set(previewToken, { digest, baseDigest, expiresAt });
        return { ok: true, operation, document: { ...value.data, previewToken, expiresAt, baseDigest, inputDigest: digest, slot: current.slot } };
      }
      if (input.confirmed !== true) fail('CONFIRMATION_REQUIRED', 'organization adoption and potential network publication require explicit confirmation');
      const preview = tokens.get(input.previewToken);
      if (!preview) fail('ORG_PREVIEW_REQUIRED', 'preview is missing, expired, consumed, or belongs to another Host');
      // Consume before asynchronous work; errors/timeouts never allow replay.
      tokens.delete(input.previewToken);
      if (preview.digest !== digest) fail('ORG_PREVIEW_MISMATCH', 'organization text changed; preview again');
      await status();
      if (preview.expiresAt <= now()) fail('ORG_PREVIEW_REQUIRED', 'preview expired while checking the organization state');
      if (preview.baseDigest !== await acceptedOrgDigest(acceptedPath)) fail('ORG_BASE_CHANGED', 'accepted organization changed; preview again');
      const result = await withText(text, async file => {
        if (preview.expiresAt <= now()) fail('ORG_PREVIEW_REQUIRED', 'preview expired before adoption');
        if (preview.baseDigest !== await acceptedOrgDigest(acceptedPath)) fail('ORG_BASE_CHANGED', 'accepted organization changed; preview again');
        return await runCli(['org', 'import', file, '--force', '--json']);
      });
      const document = readResult(result);
      if (result.status !== 0 || document.ok !== true) resultError(result, document);
      if (document.adopted !== true) fail('INVALID_RESPONSE', 'organization CLI did not confirm adoption');
      return { ok: true, operation, document };
    } finally { busy = false; }
  };
}
