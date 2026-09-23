#!/usr/bin/env node
/** Local, versioned distribution of the GUI plugin bundle itself.

The shipped GUI tree is the plugin's source of truth; this script turns that
verified tree into one immutable tarball per content hash, installs it into
the DSH web profile through a `file:` dependency (never a mutable `link:`),
and records what a machine actually runs in a profile-side manifest. The
vendored Codex fork pioneered the same shape in codex-package.mjs.
*/
import { inspectGuiSource } from './gui-source.mjs';
import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const PLUGIN_PACKAGE = '@hyprial/dsh-hyprial-plugin';
export const LEGACY_PLUGIN_PACKAGE = '@hyprial/dsh-h2b-talk';
export const PLUGIN_ROW_ID = 'hyprial-plugin';
export const LEGACY_PLUGIN_ROW_ID = 'h2b-talk';
const PROFILE_FILES = ['package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'hyprial-plugin-install.json'];

function run(command, args, { cwd = root, capture = false } = {}) {
  const result = spawnSync(command, args, { cwd, encoding: 'utf8', stdio: capture ? 'pipe' : 'inherit' });
  if (result.error || result.status !== 0) throw new Error(`${command} failed (${result.status ?? 'unavailable'})${capture ? `: ${result.stderr}` : ''}`);
  return result.stdout;
}

/** Verify the plugin source and its loader wiring without touching the profile. */
export function verifyPluginSource(directory = root) {
  const source = inspectGuiSource(directory, { requireClean: true });
  const pkg = JSON.parse(readFileSync(join(directory, 'package.json'), 'utf8'));
  if (pkg.name !== PLUGIN_PACKAGE || typeof pkg.version !== 'string' || !/^\d+\.\d+\.\d+/.test(pkg.version)) {
    throw new Error('Invalid GUI plugin package metadata');
  }
  if (!Array.isArray(pkg.files) || !pkg.files.includes('dsh-web.patch.yml') || !pkg.files.includes('static')) {
    throw new Error('GUI plugin package must ship its patch layer and static host');
  }
  const patch = readFileSync(join(directory, 'dsh-web.patch.yml'), 'utf8');
  if (!new RegExp(`id: ${PLUGIN_ROW_ID}[\\s\\S]*name: '?${PLUGIN_PACKAGE.replace('/', '\\/')}'?`).test(patch)) {
    throw new Error('GUI plugin patch must register exactly one hyprial-plugin row under the package name');
  }
  const client = readFileSync(join(directory, 'static/client.js'), 'utf8');
  if (!client.includes(`id: ${JSON.stringify(PLUGIN_PACKAGE)}`)) throw new Error('Static client module id differs from the package name; rebuild before installation');
  return { pkg, source };
}

/** Pack the verified source into one versioned tarball plus its manifest. */
export function buildPluginPackage({ directory = root } = {}) {
  const { pkg, source } = verifyPluginSource(directory);
  const staging = mkdtempSync(join(tmpdir(), 'hyprial-plugin-pack-'));
  let bytes;
  let packed;
  try {
    packed = JSON.parse(run('npm', ['pack', '--ignore-scripts', '--json', '--pack-destination', staging], { cwd: directory, capture: true }))[0];
    const entries = run('tar', ['-tzf', join(staging, packed.filename)], { cwd: directory, capture: true }).split('\n');
    for (const file of ['package/dsh-web.patch.yml', 'package/static/host.js', 'package/static/client.js', 'package/integration/session-ledger.js', 'package/package.json']) {
      if (!entries.includes(file)) throw new Error(`GUI plugin package is missing ${file}`);
    }
    bytes = readFileSync(join(staging, packed.filename));
  } finally { rmSync(staging, { recursive: true, force: true }); }
  const manifest = {
    schema: 'hyprial.plugin-package/v1', name: pkg.name, version: pkg.version,
    file: basename(packed.filename), sha256: createHash('sha256').update(bytes).digest('hex'),
    sourceKind: source.kind, sourceCommit: source.commit,
  };
  return { manifest, bytes };
}

/** Install one verified tarball into the profile as a `file:` dependency. */
export async function installPluginPackage({
  directory = root, profile = 'web', link = false,
  dshHome = resolve(process.env.DSH_HOME || join(homedir(), '.dsh')),
  runCommand = (command, args) => run(command, args, {}),
} = {}) {
  const profileDirectory = join(dshHome, 'profiles', profile);
  if (link) {
    // Development flow: the profile points at the checkout itself.
    runCommand('dsh', ['plugin', '--profile', profile, 'add', '--ignore-scripts', resolve(directory)]);
    return { link: resolve(directory) };
  }
  const { manifest, bytes } = buildPluginPackage({ directory });
  const cache = join(dshHome, 'hyprial-packages', manifest.sha256);
  mkdirSync(cache, { recursive: true, mode: 0o700 });
  const cached = join(cache, manifest.file);
  writeFileSync(cached, bytes, { mode: 0o600 });
  writeFileSync(join(cache, 'release.json'), `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 });
  const backup = join(dshHome, 'hyprial-package-backups', new Date().toISOString().replaceAll(':', '-'));
  mkdirSync(backup, { recursive: true, mode: 0o700 });
  for (const file of PROFILE_FILES) {
    if (existsSync(join(profileDirectory, file))) copyFileSync(join(profileDirectory, file), join(backup, file));
  }
  // Let DSH initialize its template before creating a workspace configuration.
  if (!existsSync(join(profileDirectory, 'package.json'))) runCommand('dsh', ['plugin', '--profile', profile, 'list', '--json']);
  const before = existsSync(join(profileDirectory, 'package.json'))
    ? JSON.parse(readFileSync(join(profileDirectory, 'package.json'), 'utf8')) : {};
  let removedLegacy = false;
  if (before.dependencies && before.dependencies[LEGACY_PLUGIN_PACKAGE]) {
    // `dsh plugin` reconciles the bundles layer against the installed state,
    // so removing the legacy link also drops its bundle row.
    runCommand('dsh', ['plugin', '--profile', profile, 'remove', LEGACY_PLUGIN_PACKAGE]);
    removedLegacy = true;
  }
  console.log(`Installing ${manifest.name} ${manifest.version} (${manifest.sha256}); previous profile manifests: ${backup}`);
  try {
    runCommand('dsh', ['plugin', '--profile', profile, 'add', '--ignore-scripts', `file:${cached}`]);
  } catch (error) {
    console.error(`Installation failed. Profile backup: ${backup}.`);
    throw error;
  }
  const installed = { ...manifest, artifact: cached, backup, removedLegacy };
  mkdirSync(profileDirectory, { recursive: true });
  writeFileSync(join(profileDirectory, 'hyprial-plugin-install.json'), `${JSON.stringify(installed, null, 2)}\n`, { mode: 0o600 });
  return installed;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const command = process.argv[2] || 'verify';
    if (command === 'verify') {
      const { pkg, source } = verifyPluginSource();
      console.log(`Verified ${pkg.name} ${pkg.version} from ${source.kind} source ${source.commit}`);
    } else if (command === 'install') {
      const link = process.argv.includes('--link');
      await installPluginPackage({ link });
    } else throw new Error('Usage: node scripts/hyprial-plugin-package.mjs verify|install [--link]');
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
