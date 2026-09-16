#!/usr/bin/env node
// Release archives carry provenance; a checkout continues to use Git's index.
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

export function inspectGuiSource(root, { env = process.env, requireClean = false } = {}) {
  const metadata = join(root, 'gui-release.json');
  if (existsSync(metadata)) {
    const release = JSON.parse(readFileSync(metadata, 'utf8'));
    if (release.schema !== 'hyprial.gui-release/v1' || !/^[a-f0-9]{40}$/.test(release.commit) ||
        typeof release.version !== 'string' || !release.version.trim() || !Array.isArray(release.files) || !release.files.length) {
      throw new Error('Invalid GUI release provenance');
    }
    for (const [key, expected] of [['HYPRIAL_SOURCE_COMMIT', release.commit], ['HYPRIAL_SOURCE_VERSION', release.version]]) {
      if (env[key] !== undefined && env[key] !== expected) throw new Error(`${key} differs from GUI release provenance`);
    }
    const paths = new Set();
    for (const entry of release.files) {
      const path = entry?.path;
      if (typeof path !== 'string' || !path || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..') ||
          path === 'gui-release.json' || paths.has(path) || !/^[a-f0-9]{64}$/.test(entry.sha256) || typeof entry.executable !== 'boolean') {
        throw new Error('Invalid GUI release file entry');
      }
      paths.add(path);
      // Do not follow symlinks in either files or their parent directories.
      let cursor = root;
      for (const part of path.split('/')) {
        cursor = join(cursor, part);
        if (lstatSync(cursor).isSymbolicLink()) throw new Error(`GUI release symlink: ${path}`);
      }
      const stat = lstatSync(cursor);
      if (!stat.isFile() || Boolean(stat.mode & 0o111) !== entry.executable ||
          createHash('sha256').update(readFileSync(cursor)).digest('hex') !== entry.sha256) {
        throw new Error(`GUI release file changed: ${path}`);
      }
    }
    for (const required of ['package.json', 'scripts/install-local.sh', 'scripts/gui-source.mjs']) {
      if (!paths.has(required)) throw new Error(`GUI release provenance missing ${required}`);
    }
    return { kind: 'release', commit: release.commit, version: release.version, workingTreeClean: true, paths: [...paths] };
  }
  const git = args => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  let commit, status;
  try {
    // Require this directory to be a tracked GUI tree, not merely inside an unrelated repository.
    git(['ls-files', '--error-unmatch', 'package.json', 'scripts/install-local.sh']);
    commit = git(['rev-parse', 'HEAD']);
    status = git(['status', '--porcelain', '--untracked-files=no', '--', '.']);
  } catch { throw new Error('GUI source needs a Git checkout or verified gui-release.json'); }
  if (requireClean && status) throw new Error('Tracked GUI repository files are dirty; preserve changes and use a clean checkout');
  return { kind: 'git', commit, workingTreeClean: status === '' };
}

export function isShippedGuiFile(root, target) {
  if (existsSync(join(root, 'gui-release.json'))) return inspectGuiSource(root).paths.includes(relative(root, target).split('\\').join('/'));
  try {
    execFileSync('git', ['ls-files', '--error-unmatch', target], { cwd: root, stdio: 'ignore' });
    return true;
  } catch { return false; }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = inspectGuiSource(resolve(dirname(fileURLToPath(import.meta.url)), '..'), { requireClean: true });
    console.log(`Verified GUI ${result.kind} source ${result.commit}`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
