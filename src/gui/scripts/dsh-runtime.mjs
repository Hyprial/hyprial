import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

export const registry = 'https://registry.npmjs.org';
const versionPattern = /^\d+\.\d+\.\d+(?:-[\w.]+)?$/;
// Share the caller's cache/proxy/isolation for metadata and package installation.
// Resolve latest only against npm; a transport failure may use a mirror for the
// already selected exact candidate, never to select an older DSH version.
export function createNpmRunner(env = process.env, spawn = spawnSync, log = console.error) {
  return args => {
    const metadata = args[0] === 'view';
    const mirror = 'https://registry.npmmirror.com';
    const sources = metadata ? [registry] : env.DSH_NPM_MIRROR_FIRST === '1' ? [mirror, registry] : [registry, mirror];
    // A complete cold dependency graph needs much longer than one HTTP request.
    // The budget is shared across fallback attempts, not reset on a new source.
    const deadline = Date.now() + (metadata ? 60000 : args.includes('--package-lock-only') ? 900000 : 600000);
    for (const [index, source] of sources.entries()) {
      const command = [...args.filter(arg => !arg.startsWith('--registry=')),
        '--registry=' + source, '--fetch-retries=1', '--fetch-timeout=30000',
        '--fetch-retry-mintimeout=1000', '--fetch-retry-maxtimeout=5000', '--loglevel=http',
        ...(metadata ? [] : ['--prefer-offline'])];
      const started = Date.now();
      const timeout = Math.max(1, deadline - started);
      log(`DSH_NPM command=${args[0]} registry=${source} timeout_seconds=${Math.ceil(timeout / 1000)}`);
      const p = spawn('npm', command, { env, encoding: 'utf8',
        timeout, killSignal: 'SIGKILL', maxBuffer: 16 * 1024 * 1024 });
      log(`DSH_NPM command=${args[0]} seconds=${Math.round((Date.now() - started) / 1000)} status=${p.error?.code || p.status}`);
      if (!p.error && p.status === 0) return p.stdout.trim();
      const detail = [p.error?.message, p.stderr, p.stdout].filter(Boolean).join('\n');
      const safe = detail.replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g, '$1[redacted]@')
        .replace(/([?&](?:token|auth|key)=)[^\s"&]+/gi, '$1[redacted]');
      const error = new Error(`npm ${args[0]} failed (${source}): ${safe.slice(-12000)}`);
      const transient = !p.error && (/\b(?:ETIMEDOUT|ESOCKETTIMEDOUT|ECONNRESET|ECONNREFUSED|EAI_AGAIN|ENETUNREACH|ENOTFOUND|E50[234])\b/.test(detail) || (source === mirror && /\b(?:E404|ETARGET)\b/.test(detail)));
      if (!transient || Date.now() >= deadline || index === sources.length - 1) throw error;
      log(error.message + '\nRetrying the same candidate through the alternate registry.');
    }
  };
}
const npm = createNpmRunner();
export function resolveLatest(run = npm) {
  const version = JSON.parse(run(['view', '@deepseek-ai/dsh', 'dist-tags.latest', '--json', '--registry=' + registry]));
  assert.equal(typeof version, 'string');
  assert.match(version, versionPattern, 'npm latest must resolve to an exact version');
  return version;
}

// Never mutate the source template or reuse a previous run's lock.
export function prepareCandidate(template, destination, version, run = npm) {
  assert.match(version, versionPattern);
  assert.notEqual(resolve(template), resolve(destination));
  assert.ok(!existsSync(join(destination, 'package-lock.json')), 'Candidate already has a dependency lock');
  const manifest = JSON.parse(readFileSync(join(template, 'package.json'), 'utf8'));
  assert.equal(manifest.dependencies['@deepseek-ai/dsh'], 'latest', 'DSH source policy must remain latest');
  manifest.dependencies['@deepseek-ai/dsh'] = version;
  mkdirSync(destination, { recursive: true });
  writeFileSync(join(destination, 'package.json'), JSON.stringify(manifest, null, 2) + '\n');
  run(['install', '--prefix', destination, '--package-lock-only', '--ignore-scripts', '--no-audit', '--no-fund', '--registry=' + registry]);
  return inspectCandidate(destination);
}

export function inspectCandidate(destination) {
  const manifest = JSON.parse(readFileSync(join(destination, 'package.json'), 'utf8'));
  const version = manifest.dependencies['@deepseek-ai/dsh'];
  assert.match(version, versionPattern, 'Candidate must record an exact resolved version');
  const lock = JSON.parse(readFileSync(join(destination, 'package-lock.json'), 'utf8'));
  assert.equal(lock.packages[''].dependencies['@deepseek-ai/dsh'], version, 'Candidate root lock mismatch');
  assert.equal(lock.packages['node_modules/@deepseek-ai/dsh'].version, version, 'Candidate DSH lock mismatch');
  return version;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [action, ...args] = process.argv.slice(2);
    if (action === 'resolve' && args.length === 0) console.log(resolveLatest());
    else if (action === 'prepare' && args.length === 3) prepareCandidate(...args);
    else throw new Error('Usage: dsh-runtime.mjs resolve | prepare <template> <candidate> <resolved-version>');
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
