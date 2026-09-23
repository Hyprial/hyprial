#!/usr/bin/env node
/** Install the sole workspace root provider before registering its profile patch. */
import { readFileSync, realpathSync, existsSync, lstatSync, mkdirSync, writeFileSync, renameSync, rmSync, openSync, closeSync } from 'node:fs';
import { homedir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { PLUGIN_PACKAGE, LEGACY_PLUGIN_PACKAGE, PLUGIN_ROW_ID, LEGACY_PLUGIN_ROW_ID } from './hyprial-plugin-package.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const layoutPackage = join(root, 'packages/gui-layout');
export function verifyGuiLayoutPackage(directory = layoutPackage) {
  const manifest = JSON.parse(readFileSync(join(directory, 'package.json'), 'utf8'));
  if (manifest.name !== '@hyprial/dsh-gui-layout' || manifest.exports?.['.'] !== './host.js' || manifest.exports?.['./client'] !== './client.js' || manifest.dsh?.client?.platform !== 'web') {
    throw new Error('Invalid GUI layout package metadata');
  }
  if (manifest.dsh.bundle) throw new Error('GUI layout must not insert another root row; H2B Talk replaces ui-layout');
  for (const file of ['host.js', 'client.js', 'build.mjs']) if (!existsSync(join(directory, file))) throw new Error(`GUI layout package is missing ${file}`);
  const checked = spawnSync(process.execPath, [join(directory, 'build.mjs'), '--check'], { encoding: 'utf8' });
  if (checked.error || checked.status !== 0) throw new Error('GUI layout client differs from source; rebuild before installation');
  return manifest;
}
export function guiLayoutInstallArguments(directory = layoutPackage, profile = 'web') {
  return ['plugin', '--profile', profile, 'add', '--ignore-scripts', resolve(directory)];
}
export function installGuiLayoutPackage({ directory = layoutPackage, profile = 'web', run = spawnSync } = {}) {
  verifyGuiLayoutPackage(directory);
  const result = run('dsh', guiLayoutInstallArguments(directory, profile), { cwd: root, stdio: 'inherit' });
  if (result.error || result.status !== 0) throw new Error('Could not install GUI layout dependency');
}
export function verifyGuiLayoutProfile(source) {
  // Package verification runs before npm ci during a clean install. Only the
  // later profile-composition phase needs the installed YAML dependency.
  const { parse } = createRequire(import.meta.url)('yaml');
  let rows;
  try { rows = parse(source, { logLevel: 'silent', customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: value => value }] }); }
  catch { throw new Error('Cannot parse DSH composed profile'); }
  if (!Array.isArray(rows)) throw new Error('Invalid DSH composed profile');
  const providers = [];
  const plugins = [];
  function visit(entries) {
    const ids = new Set();
    for (const row of entries) {
      // Loader rejects duplicate IDs even when one entry is disabled. IDs are
      // scoped to their entry group, matching Cordis EntryGroup.update.
      if (row && typeof row.id === 'string') {
        if (ids.has(row.id)) throw new Error('Duplicate DSH loader entry id: ' + row.id);
        ids.add(row.id);
      }
      if (!row || row.disabled === true) continue;
      if (['@hyprial/dsh-gui-layout', '@deepseek-ai/dsh-client-ui-layout'].includes(row.name)) providers.push(row);
      if (row.name === PLUGIN_PACKAGE) plugins.push(row);
      if (row.group && Array.isArray(row.config)) visit(row.config);
    }
  }
  visit(rows);
  if (providers.length !== 1 || providers[0].name !== '@hyprial/dsh-gui-layout') {
    throw new Error('DSH profile must have exactly one active GUI layout provider; check user layout overrides');
  }
  if (plugins.length !== 1 || plugins[0].id !== PLUGIN_ROW_ID) throw new Error('DSH profile must have exactly one active ' + PLUGIN_ROW_ID + ' entry');
  return true;
}

/** Move legacy plugin rows to the current identifiers without dropping overrides. */
export function migrateLegacyGuiProfile({ profileDirectory = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'profiles', 'web') } = {}) {
  const manifest = JSON.parse(readFileSync(join(profileDirectory, 'package.json'), 'utf8'));
  if (!manifest.dsh?.profile?.bundles?.includes(PLUGIN_PACKAGE)) throw new Error('Register the Hyprial plugin bundle before migrating its legacy insertion');
  const patchPath = join(profileDirectory, 'cordis.patch.yml');
  if (!existsSync(patchPath)) return { changed: false };
  const lockPath = join(profileDirectory, '.h2b-gui-profile-migration.lock');
  let lock;
  try { lock = openSync(lockPath, 'wx', 0o600); }
  catch (error) { throw new Error('Cannot lock GUI profile migration; another migration may be running', { cause: error }); }
  let temp;
  try {
    const stat = lstatSync(patchPath);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('GUI profile patch must be a regular file; migrate linked profiles explicitly');
    const original = readFileSync(patchPath, 'utf8');
    const { parseDocument, isSeq, isMap, isAlias, visit } = createRequire(import.meta.url)('yaml');
    const document = parseDocument(original, { customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: value => value }] });
    if (document.errors.length || !isSeq(document.contents)) throw new Error('Cannot safely migrate GUI profile patch: expected a valid YAML patch list');
    const output = [];
    let changed = false;
    const pluginRow = row => isMap(row) && [LEGACY_PLUGIN_ROW_ID, PLUGIN_ROW_ID].includes(row.get('id'));
    // Rows still naming the retired plugin package, or still on the retired
    // row id, are migrated; everything else stays byte-for-byte untouched.
    const legacyPluginRow = row => pluginRow(row) &&
      (row.get('id') === LEGACY_PLUGIN_ROW_ID || row.get('name') === LEGACY_PLUGIN_PACKAGE);
    // Only rows the migration rewrites are validated; unrelated user rows are
    // never inspected beyond the id/name match above.
    const assertRewritable = row => {
      const name = row.get('name');
      if (name !== undefined && name !== null && ![LEGACY_PLUGIN_PACKAGE, PLUGIN_PACKAGE].includes(name)) {
        throw new Error('Legacy plugin row id belongs to another plugin; resolve the profile conflict explicitly');
      }
      const keys = row.items.map(pair => pair.key.value);
      if (keys.some(key => !['id', 'name', 'config', 'disabled'].includes(key))) {
        throw new Error('Legacy plugin row has unsupported loader fields; migrate it explicitly without discarding configuration');
      }
      let complex = false;
      visit(row, (_, node) => { if (isAlias(node) || node?.anchor) complex = true; });
      if (complex) throw new Error('Legacy plugin row contains YAML aliases or anchors; migrate it explicitly');
    };
    const rewritePluginRow = row => {
      if (row.get('id') === LEGACY_PLUGIN_ROW_ID) row.set('id', PLUGIN_ROW_ID);
      if (row.has('name')) row.set('name', PLUGIN_PACKAGE);
    };
    for (const operation of document.contents.items) {
      if (!isMap(operation) || !operation.has('insert')) {
        if (legacyPluginRow(operation)) {
          assertRewritable(operation);
          rewritePluginRow(operation);
          changed = true;
        }
        output.push(operation);
        continue;
      }
      const inserted = operation.get('insert', true);
      if (!isSeq(inserted)) throw new Error('Cannot safely migrate GUI profile patch: insert must be a list');
      const matches = inserted.items.filter(row => pluginRow(row));
      if (!matches.length) { output.push(operation); continue; }
      if (operation.items.length !== 1) throw new Error('Legacy plugin insertion has positioning or conditional fields; migrate it explicitly');
      const overrides = [];
      for (const row of matches) {
        assertRewritable(row);
        if (row.has('config') || row.has('disabled')) {
          rewritePluginRow(row);
          overrides.push(row);
        }
      }
      inserted.items = inserted.items.filter(row => !matches.includes(row));
      if (inserted.items.length) output.push(operation);
      // id/name now target the bundle-provided row; config, disabled, tags and
      // comments remain YAML nodes rather than round-tripping through objects.
      output.push(...overrides);
      changed = true;
    }
    if (!changed) return { changed: false };
    // Multiple override rows may legitimately target the same id (patch entries
    // apply in sequence); only the composed tree's own duplicate-id fence
    // rejects real duplicates at verification time.
    document.contents.items = output;
    const updated = document.toString();
    const token = randomUUID();
    const backupDirectory = join(profileDirectory, '.h2b-gui-profile-backup-' + token);
    mkdirSync(backupDirectory, { mode: 0o700 });
    const backupPath = join(backupDirectory, 'cordis.patch.yml');
    writeFileSync(backupPath, original, { mode: 0o600, flag: 'wx' });
    temp = join(profileDirectory, '.cordis.patch.gui-' + token + '.tmp');
    writeFileSync(temp, updated, { mode: stat.mode & 0o777, flag: 'wx' });
    if (readFileSync(patchPath, 'utf8') !== original) throw new Error('GUI profile changed during migration; original backup retained, retry after the editor finishes');
    renameSync(temp, patchPath); temp = undefined;
    return { changed: true, backupPath };
  } finally {
    if (temp) rmSync(temp, { force: true });
    closeSync(lock); rmSync(lockPath, { force: true });
  }
}
if (process.argv[1] && existsSync(process.argv[1]) && realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1])) {
  const command = process.argv[2] || 'verify';
  if (command === 'verify') verifyGuiLayoutPackage();
  else if (command === 'install') installGuiLayoutPackage();
  else if (command === 'verify-profile') verifyGuiLayoutProfile(readFileSync(0, 'utf8'));
  else if (command === 'migrate-profile') {
    const result = migrateLegacyGuiProfile();
    console.log(result.changed ? 'Migrated legacy GUI profile insertion; backup: ' + result.backupPath : 'GUI profile insertion needs no migration');
  }
  else throw new Error('Usage: gui-layout-package.mjs verify|install|verify-profile|migrate-profile');
}
