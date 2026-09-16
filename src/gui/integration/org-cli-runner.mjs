import { spawn } from 'node:child_process';
import { realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

function fail(message) { throw Object.assign(new Error(message), { code: 'INVALID_ARGUMENT' }); }

// Matches h2b.home.configured_h2b_home(): H2B_HOME (when present), otherwise
// Path.home() / '.h2b'; org.store.OrgContextStore uses home / 'org-context.md'.
// HARNESS_STATE_DIR is deliberately NOT used for the accepted document.
export async function orgAcceptedPath(env = process.env) {
  const userHome = env.HOME || homedir();
  let home = Object.hasOwn(env, 'H2B_HOME') ? env.H2B_HOME : join(userHome, '.h2b');
  if (typeof home !== 'string') fail('Host H2B_HOME must be a string');
  if (home === '~') home = userHome;
  else if (home.startsWith('~/')) home = join(userHome, home.slice(2));
  else if (home.startsWith('~')) fail('Host H2B_HOME must not use another user home shorthand');
  // Resolve symlinks like Python Path.resolve; never create a missing home.
  return join(await realpath(resolve(home)), 'org-context.md');
}

export function validateOrgCliArgv(argv) {
  if (!Array.isArray(argv) || argv.some(value => typeof value !== 'string' || value.includes('\0'))) fail('organization argv must contain strings');
  if (argv[0] !== 'org') fail('only organization CLI operations are allowed');
  if (argv[1] === 'fetch') {
    if (argv.length !== 7 || argv[2] !== '--from' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(argv[3]) || argv[4] !== '--timeout' || argv[6] !== '--json') fail('invalid organization fetch argv');
    const seconds = Number(argv[5]);
    if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 20) fail('organization fetch timeout exceeds the Host limit');
  } else if (argv[1] === 'import') {
    if (!isAbsolute(argv[2] || '') || !((argv.length === 4 && argv[3] === '--json') || (argv.length === 5 && argv[3] === '--force' && argv[4] === '--json'))) fail('invalid organization import argv');
  } else fail('organization status must use the read-only Host query, not this write runner');
}

// Instantiated by the Host, not a browser-supplied executable/env/path. Capture
// its environment once so a preview and its write share the same execution home.
export function createOrgCliRunner({ env = process.env, timeoutMs = 25000, maxOutputBytes = 524288 } = {}) {
  const capturedEnv = { ...env };
  return async function runOrgCli(argv) {
    validateOrgCliArgv(argv);
    return await new Promise(resolveResult => {
      const child = spawn('h2b', argv, { env: capturedEnv, shell: false, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
      let bytes = 0, stdout = '', stderr = '', timedOut = false, overflow = false, settled = false;
      function kill() {
        if (!child.pid) return;
        try { if (process.platform === 'win32') child.kill('SIGKILL'); else process.kill(-child.pid, 'SIGKILL'); }
        catch (error) { if (error.code !== 'ESRCH') child.kill('SIGKILL'); }
      }
      function finish(status) {
        if (settled) return;
        settled = true; clearTimeout(timer);
        resolveResult({ status, stdout, stderr, timedOut, overflow });
      }
      const timer = setTimeout(() => { timedOut = true; kill(); }, timeoutMs);
      function collect(stream, chunk) {
        bytes += chunk.length;
        if (bytes > maxOutputBytes) { overflow = true; kill(); return; }
        if (stream === 'stdout') stdout += chunk.toString(); else stderr += chunk.toString();
      }
      child.stdout.on('data', chunk => collect('stdout', chunk));
      child.stderr.on('data', chunk => collect('stderr', chunk));
      child.on('error', error => {
        // No command/env dump. Preserve an actionable structured launch error.
        stdout = JSON.stringify({ ok: false, code: error.code || 'COMMAND_FAILED', error: 'H2B organization CLI could not be launched' });
        finish(null);
      });
      child.on('close', status => finish(status));
    });
  };
}
