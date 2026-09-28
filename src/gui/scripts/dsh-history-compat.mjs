// Apply only to an isolated candidate runtime before it passes release checks.
import { readFileSync, writeFileSync, renameSync } from 'node:fs';
import { installedPackage } from './dsh-runtime.mjs';
import { createHash } from 'node:crypto';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
const manifest = JSON.parse(readFileSync(new URL('../packages/dsh-history-compat/patch.json', import.meta.url), 'utf8'));
const sha256 = value => createHash('sha256').update(value).digest('hex');
export function applyDshHistoryCompatibility(runtime, patch = manifest) {
  const plan = patch.files.map(entry => {
    const root = installedPackage(runtime, entry.package);
    const version = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).version;
    const filename = join(root, entry.file), original = readFileSync(filename, 'utf8');
    // Content-addressed, not version-addressed: the patch's validity is a
    // property of the bytes it rewrites. Upstream patch releases of the audited
    // line (0.1.5-rc.2 -> 0.1.5-rc.3) ship these files byte-identical, so a
    // version bump alone must not fail the gate; any real source change still
    // fails closed and demands re-audit.  The installed copy is found by
    // installedPackage(), because npm may hoist or nest it.
    const digest = sha256(original);
    if (digest === entry.afterSha256) return { filename, original, next: original };
    if (digest !== entry.beforeSha256) throw new Error(`Unaudited history compatibility input: ${entry.package}/${entry.file} at ${version} (audited at ${entry.version}); sha256 ${digest}`);
    let next = original;
    for (const { before, after } of entry.replacements) {
      if (next.split(before).length !== 2) throw new Error(`History compatibility anchor mismatch: ${filename}`);
      next = next.replace(before, after);
    }
    if (sha256(next) !== entry.afterSha256) throw new Error(`History compatibility output mismatch: ${filename}`);
    return { filename, original, next };
  });
  // Validate the complete candidate before changing any file. An interrupted
  // candidate remains unpublished; the installer discards failed candidates.
  for (const { filename, original, next } of plan) {
    if (next === original) continue;
    const temporary = filename + '.history-compat-next';
    writeFileSync(temporary, next, { flag: 'wx' });
    renameSync(temporary, filename);
  }
  return { patchVersion: patch.patchVersion, files: plan.length, changed: plan.filter(p => p.next !== p.original).length };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 3) throw new Error('Usage: dsh-history-compat.mjs candidate-runtime-directory');
  console.log(JSON.stringify(applyDshHistoryCompatibility(process.argv[2])));
}
