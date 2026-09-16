import { createHmac, randomBytes, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';

const NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const APP_ID = /^[A-Za-z0-9_-]{1,128}$/;
const NATIVE_ID = /^[A-Za-z0-9_-]{1,256}$/;
const AUTH_STATUSES = new Set(['requested', 'already_requested', 'nothing_to_request', 'super_sensitive_only', 'request_limit_exceeded', 'failed']);
const SAFE_ERROR_CODES = new Set(['ADAPTER_EXISTS', 'ADAPTER_NOT_FOUND', 'INVALID_ARGUMENT', 'PERMISSION_DENIED', 'SECRET_REQUIRED', 'SECRET_UNAVAILABLE']);

function fail(code, message, details) { throw Object.assign(new Error(message), { code, ...(details ? { details } : {}) }); }
function strictObject(input, keys) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(key => !keys.includes(key))) fail('INVALID_ARGUMENT', 'Unsupported enrollment fields');
}
function nameOf(value) { if (typeof value !== 'string' || !NAME.test(value)) fail('INVALID_ARGUMENT', 'Adapter name must be a lowercase token (maximum 64 characters)'); return value; }
function normalize(input) {
  strictObject(input, ['operation', 'name', 'appId', 'secret', 'routes', 'defaultRoute', 'previewToken', 'confirmed']);
  const name = nameOf(input.name);
  if (typeof input.appId !== 'string' || !APP_ID.test(input.appId)) fail('INVALID_ARGUMENT', 'App ID format is invalid');
  if (typeof input.secret !== 'string' || !input.secret.length || input.secret.length > 4096 || /[\s\x00-\x1f\x7f]/.test(input.secret)) fail('INVALID_ARGUMENT', 'App secret format is invalid');
  if (input.routes !== undefined && (!Array.isArray(input.routes) || input.routes.length > 20)) fail('INVALID_ARGUMENT', 'Routes must be an array with at most 20 entries');
  const routes = (input.routes || []).map(route => {
    strictObject(route, ['name', 'nativeId']);
    nameOf(route.name);
    if (typeof route.nativeId !== 'string' || !NATIVE_ID.test(route.nativeId)) fail('INVALID_ARGUMENT', 'Route chat ID format is invalid');
    return { name: route.name, nativeId: route.nativeId };
  });
  if (!routes.length) fail('ROUTE_REQUIRED', 'Installed H2B adapter add requires at least one outbound route; obtain a chat ID before registering');
  if (new Set(routes.map(route => route.name)).size !== routes.length) fail('INVALID_ARGUMENT', 'Route names must be unique');
  const defaultRoute = input.defaultRoute || '';
  if (defaultRoute && !routes.some(route => route.name === defaultRoute)) fail('INVALID_ARGUMENT', 'Default route must name one of the supplied routes');
  return { name, appId: input.appId, secret: input.secret, routes, defaultRoute };
}

// Called directly by the Host, never through its audited shell tool. Neither argv
// nor environment receives the secret; the child gets a private stdin pipe only.
export function runAdapterCli(argv, { stdin = '', env = process.env, timeoutMs = 20000 } = {}) {
  return new Promise(resolve => {
    const child = spawn('h2b', argv, { env, shell: false, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', size = 0, timedOut = false, overflow = false, done = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeoutMs);
    function finish(result) { if (done) return; done = true; clearTimeout(timer); resolve({ stdout, timedOut, overflow, ...result }); }
    child.stdout.on('data', chunk => { size += chunk.length; if (size > 262144) { overflow = true; child.kill('SIGKILL'); } else stdout += chunk.toString(); });
    // Discard stderr, including framework exceptions or third-party credential echoes.
    child.stderr.on('data', chunk => { size += chunk.length; if (size > 262144) { overflow = true; child.kill('SIGKILL'); } });
    child.stdin.on('error', () => {});
    child.on('error', () => finish({ status: null }));
    child.on('close', status => finish({ status }));
    child.stdin.end(stdin);
  });
}

function parseResult(result) {
  if (result.timedOut) fail('COMMAND_TIMEOUT', 'H2B command timed out');
  if (result.overflow) fail('OUTPUT_TOO_LARGE', 'H2B output exceeded the safety limit');
  let doc;
  try { doc = JSON.parse(result.stdout); } catch { fail('COMMAND_FAILED', 'H2B did not return a valid response'); }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) fail('COMMAND_FAILED', 'H2B returned an invalid document');
  return doc;
}
function cliFailure(doc) {
  const code = SAFE_ERROR_CODES.has(doc?.error?.code) ? doc.error.code : 'COMMAND_FAILED';
  fail(code, code === 'ADAPTER_EXISTS' ? 'Adapter already exists; replacement is not allowed' : 'H2B rejected the operation; inspect Adapter status before retrying');
}
export function safeAuthorizationUrl(value) {
  if (typeof value !== 'string' || value.length > 2048) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'open.feishu.cn' && !url.username && !url.password && !url.port && !url.search && !url.hash && /^\/app\/[A-Za-z0-9_-]+\/permission$/.test(url.pathname) ? url.href : null;
  } catch { return null; }
}

export function createAdapterEnrollment({ run = runAdapterCli, env = process.env, now = Date.now } = {}) {
  const previews = new Map(), active = new Set(), key = randomBytes(32);
  const digest = value => createHmac('sha256', key).update(JSON.stringify(value)).digest('hex');
  async function invoke(argv, stdin = '') { try { return await run(argv, { stdin, env }); } catch { fail('COMMAND_FAILED', 'H2B command could not be launched'); } }
  async function assertAbsent(name) {
    const result = await invoke(['adapter', 'list', '--json']);
    const doc = parseResult(result);
    if (result.status !== 0 || doc.ok === false || !Array.isArray(doc.adapters)) cliFailure(doc);
    if (doc.adapters.some(adapter => adapter?.name === name)) fail('ADAPTER_EXISTS', 'Adapter already exists; replacement is not allowed');
  }
  async function handle(input) {
    const operation = input?.operation;
    if (operation === 'adapter-authorize') {
      strictObject(input, ['operation', 'name', 'confirmed']);
      const name = nameOf(input.name);
      if (input.confirmed !== true) fail('CONFIRMATION_REQUIRED', 'Confirm requesting tenant authorization for this Adapter');
      const result = await invoke(['adapter', 'authorize', name, '--json']);
      const doc = parseResult(result);
      if (!AUTH_STATUSES.has(doc.status)) cliFailure(doc);
      const authorizationUrl = safeAuthorizationUrl(doc.authorizationUrl);
      return { ok: true, operation, document: { name, status: doc.status, requested: doc.ok === true, code: Number.isSafeInteger(doc.code) ? doc.code : -1, manualApprovalRequired: true, authorizationUrl, urlRejected: !!doc.authorizationUrl && !authorizationUrl, scopeDeclarationSupported: false } };
    }
    if (!['adapter-enroll-preview', 'adapter-enroll'].includes(operation)) fail('INVALID_ARGUMENT', 'Unsupported enrollment operation');
    const config = normalize(input);
    for (const [token, preview] of previews) if (preview.expiresAt <= now()) previews.delete(token);
    if (operation === 'adapter-enroll-preview') {
      await assertAbsent(config.name);
      while (previews.size >= 32) previews.delete(previews.keys().next().value);
      const previewToken = randomUUID();
      previews.set(previewToken, { digest: digest(config), expiresAt: now() + 300000 });
      const { secret, ...document } = config;
      return { ok: true, operation, document: { ...document, credentialsValidated: false, writesConfiguration: false }, previewToken, previewExpiresInSeconds: 300 };
    }
    if (input.confirmed !== true) fail('CONFIRMATION_REQUIRED', 'Confirm registering this Adapter configuration');
    const preview = previews.get(input.previewToken);
    if (!preview || preview.digest !== digest(config)) fail('PREVIEW_REQUIRED', 'Review the exact current configuration before registering');
    previews.delete(input.previewToken);
    if (active.has(config.name)) fail('OPERATION_IN_PROGRESS', 'Registration for this Adapter is already in progress');
    active.add(config.name);
    try {
      await assertAbsent(config.name);
      const argv = ['adapter', 'add', config.name, '--app-id', config.appId];
      for (const route of config.routes) argv.push('--route', route.name + '=' + route.nativeId);
      if (config.defaultRoute) argv.push('--default-route', config.defaultRoute);
      argv.push('--json');
      const result = await invoke(argv, config.secret);
      let doc;
      try { doc = parseResult(result); } catch (error) {
        fail(error.code || 'COMMAND_FAILED', 'Registration outcome is unknown; inspect the Adapter list before retrying', { outcomeUnknown: true, name: config.name });
      }
      if (result.status !== 0 || doc.ok !== true) cliFailure(doc);
      const daemonReloaded = !!doc.daemonReload && doc.daemonReload.ok === true;
      return { ok: true, operation, document: { name: config.name, appId: config.appId, configured: true, daemonReloaded, partial: !daemonReloaded, nextStep: daemonReloaded ? 'Check Adapter status, then request tenant authorization or start explicitly' : 'Configuration is saved; inspect status and load Adapter configuration when the daemon is available. Do not register again.' } };
    } finally { active.delete(config.name); }
  }
  return { handle };
}
