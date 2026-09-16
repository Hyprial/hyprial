#!/usr/bin/env node
/** Local, versioned distribution of the vendored Codex compatibility fork. */
import { isShippedGuiFile } from './gui-source.mjs';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, existsSync, copyFileSync, readdirSync, mkdtempSync, rmSync } from 'node:fs';
import { dirname, join, resolve, basename } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { homedir, tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = join(root, 'vendor/dsh-codex');
const distribution = join(root, 'packages/dsh-codex');
function run(command, args, cwd = root, capture = false) {
  const result = spawnSync(command, args, { cwd, encoding: 'utf8', stdio: capture ? 'pipe' : 'inherit' });
  if (result.error || result.status !== 0) throw new Error(`${command} failed (${result.status ?? 'unavailable'})${capture ? `: ${result.stderr}` : ''}`);
  return result.stdout;
}
function sourceDigest() {
  const paths = ['package.json', 'pnpm-lock.yaml', 'tsdown.config.ts', 'tsconfig.json', 'tsconfig.client.json', 'UPSTREAM.json'];
  function collect(directory) {
    for (const entry of readdirSync(join(source, directory), { withFileTypes: true })) {
      const relative = `${directory}/${entry.name}`;
      if (entry.isDirectory()) collect(relative);
      else paths.push(relative);
    }
  }
  collect('src');
  const digest = createHash('sha256');
  for (const file of paths.sort()) digest.update(file).update('\0').update(readFileSync(join(source, file))).update('\0');
  return digest.digest('hex');
}
export function verifyPackage(directory = distribution) {
  const manifest = JSON.parse(readFileSync(join(directory, 'release.json'), 'utf8'));
  if (manifest.schema !== 'hyprial.codex-package/v1' || manifest.name !== 'dsh-codex' ||
      !/^\d+\.\d+\.\d+-hyprial\.\d+$/.test(manifest.version) ||
      manifest.file !== `dsh-codex-${manifest.version}.tgz` || !/^[a-f0-9]{64}$/.test(manifest.sha256)) {
    throw new Error('Invalid Codex release manifest');
  }
  if (manifest.sourceSha256 !== sourceDigest()) throw new Error('Codex source changed; rebuild and version the compatibility package');
  const artifact = join(directory, manifest.file);
  const digest = createHash('sha256').update(readFileSync(artifact)).digest('hex');
  if (digest !== manifest.sha256) throw new Error('Codex package checksum mismatch');
  const packaged = JSON.parse(run('tar', ['-xOf', artifact, 'package/package.json'], root, true));
  if (packaged.name !== manifest.name || packaged.version !== manifest.version ||
      packaged.dependencies?.['@earendil-works/pi-ai'] !== manifest.piAiVersion) {
    throw new Error('Codex package metadata does not match release manifest');
  }
  const entries = run('tar', ['-tzf', artifact], root, true).split('\n');
  for (const file of ['lib/index.js', 'lib/client.js', 'lib/network-worker.js', 'LICENSE', 'UPSTREAM.json', 'HYPRIAL.md']) {
    if (!entries.includes(`package/${file}`)) throw new Error(`Codex package is missing ${file}`);
  }
  return { manifest, artifact };
}
function build() {
  run('pnpm', ['install', '--frozen-lockfile', '--ignore-scripts'], source);
  run('pnpm', ['run', 'build'], source);
  run('pnpm', ['test'], source);
  mkdirSync(distribution, { recursive: true });
  const staging = mkdtempSync(join(tmpdir(), 'hyprial-codex-pack-'));
  let packed;
  try {
    packed = JSON.parse(run('npm', ['pack', '--ignore-scripts', '--json', '--pack-destination', staging], source, true))[0];
    const target = join(distribution, packed.filename);
    const bytes = readFileSync(join(staging, packed.filename));
    const tracked = isShippedGuiFile(root, target);
    if (tracked && existsSync(target) && !readFileSync(target).equals(bytes)) {
      throw new Error('A committed Codex package is immutable; increment the hyprial.N version');
    }
    writeFileSync(target, bytes);
  } finally { rmSync(staging, { recursive: true, force: true }); }
  const pkg = JSON.parse(readFileSync(join(source, 'package.json'), 'utf8'));
  const upstream = JSON.parse(readFileSync(join(source, 'UPSTREAM.json'), 'utf8'));
  const manifest = {
    schema: 'hyprial.codex-package/v1', name: pkg.name, version: pkg.version,
    file: basename(packed.filename),
    sha256: createHash('sha256').update(readFileSync(join(distribution, packed.filename))).digest('hex'),
    piAiVersion: pkg.dependencies['@earendil-works/pi-ai'], sourceSha256: sourceDigest(),
    buildDshVersion: '0.1.1-rc.2', compatibilityGate: 'npm-latest', upstream,
  };
  writeFileSync(join(distribution, 'release.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  verifyPackage();
  console.log(`Built ${manifest.file} (${manifest.sha256})`);
}
/** Acknowledge one audited native-polyfill deprecation, retaining other policies. */
export async function configureProfileDeprecations(profile) {
  if (typeof globalThis.DOMException !== 'function') throw new Error('Codex requires native DOMException');
  // Install runs after npm ci; verify intentionally has no third-party imports.
  const { parseDocument, isMap } = await import('yaml');
  const file = join(profile, 'pnpm-workspace.yaml');
  const document = parseDocument(existsSync(file) ? readFileSync(file, 'utf8') : 'packages: [.]\n');
  if (document.errors.length || !isMap(document.contents)) throw new Error('Invalid pnpm workspace configuration');
  const policy = document.get('allowedDeprecatedVersions', true);
  if (policy !== undefined && !isMap(policy)) throw new Error('allowedDeprecatedVersions must be a mapping');
  if (document.hasIn(['allowedDeprecatedVersions', 'node-domexception'])) return false;
  document.setIn(['allowedDeprecatedVersions', 'node-domexception'], '1.0.0');
  mkdirSync(profile, { recursive: true, mode: 0o700 });
  writeFileSync(file, document.toString(), { mode: 0o600 });
  return true;
}
async function install() {
  const { manifest, artifact } = verifyPackage();
  // Keep the verified tarball outside the checkout: a later GUI update must not
  // invalidate pnpm's file dependency or prevent reinstalling the previous build.
  const dshHome = resolve(process.env.DSH_HOME || join(homedir(), '.dsh'));
  const cache = join(dshHome, 'hyprial-packages', manifest.sha256);
  mkdirSync(cache, { recursive: true, mode: 0o700 });
  const cached = join(cache, manifest.file);
  copyFileSync(artifact, cached);
  copyFileSync(join(distribution, 'release.json'), join(cache, 'release.json'));
  const profile = join(dshHome, 'profiles/web');
  // Let DSH initialize its template before creating a workspace configuration.
  if (!existsSync(join(profile, 'package.json'))) run('dsh', ['plugin', '--profile', 'web', 'list', '--json'], root, true);
  const backup = join(dshHome, 'hyprial-package-backups', new Date().toISOString().replaceAll(':', '-'));
  mkdirSync(backup, { recursive: true, mode: 0o700 });
  for (const file of ['package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'hyprial-codex-install.json']) {
    if (existsSync(join(profile, file))) copyFileSync(join(profile, file), join(backup, file));
  }
  console.log(`Installing dsh-codex ${manifest.version}; previous profile manifests: ${backup}`);
  try {
    await configureProfileDeprecations(profile);
    run('dsh', ['plugin', '--profile', 'web', 'add', '--ignore-scripts', `file:${cached}`]);
  } catch (error) {
    console.error(`Installation failed. Profile backup: ${backup}. See docs/codex-compatibility.md for recovery.`);
    throw error;
  }
  mkdirSync(profile, { recursive: true });
  writeFileSync(join(profile, 'hyprial-codex-install.json'), `${JSON.stringify({ ...manifest, artifact: cached, backup }, null, 2)}\n`, { mode: 0o600 });
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const command = process.argv[2];
    if (command === 'build') build();
    else if (command === 'verify') console.log(`Verified ${verifyPackage().manifest.file}`);
    else if (command === 'install') await install();
    else throw new Error('Usage: node scripts/codex-package.mjs build|verify|install');
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
