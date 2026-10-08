import { createHash } from 'node:crypto';
import { readdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const project = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repo = resolve(project, '../..');
const evidence = join(repo, 'docs/evidence/gui-deep-modules-d1-2026-10-02');
const digest = value => createHash('sha256').update(value).digest('hex');
async function inventory(root, exclude = new Set()) {
  const records = [];
  async function visit(path) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      if (exclude.has(entry.name) || entry.name.endsWith('.log')) continue;
      const child = join(path, entry.name);
      if (entry.isSymbolicLink()) throw new Error(`Source symlink unsupported: ${child}`);
      if (entry.isDirectory()) await visit(child);
      else {
        const bytes = await readFile(child);
        records.push({ path: relative(repo, child), bytes: bytes.length, sha256: digest(bytes) });
      }
    }
  }
  await visit(root);
  return records.sort((a, b) => a.path.localeCompare(b.path, 'en'));
}
const runtime = await inventory(project, new Set(['node_modules', '.cache', 'coverage']));
const source = await inventory(join(evidence, 'source-snapshot'));
const lock = JSON.parse(await readFile(join(project, 'package-lock.json'), 'utf8'));
const dependencies = Object.entries(lock.packages).filter(([path]) => path).map(([path, info]) => ({ path, version: info.version, license: info.license, integrity: info.integrity }));
const result = {
  baselineCommit: '4382f94689adcc1c4ca1cacc3961ad2af734f544',
  d0SourceCommit: 'c3fa0178c5fb6ce3d043e384e163c2089627a12a',
  d0DeliveryDocuments: 'untracked, not members of the source commit',
  upstreamSourceCommitMapping: 'UNKNOWN; exact npm artifacts/local source hashes are the provenance anchors',
  runtimeTreeSha256: digest(runtime.map(row => `${row.path}\0${row.sha256}\0${row.bytes}\n`).join('')),
  sourceSnapshotTreeSha256: digest(source.map(row => `${row.path}\0${row.sha256}\0${row.bytes}\n`).join('')),
  runtimeFiles: runtime,
  originalSourceFiles: source,
  dependencies,
};
await writeFile(join(evidence, 'source-and-runtime-sha256.json'), JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({ runtimeFiles: runtime.length, originalSourceFiles: source.length, runtimeTreeSha256: result.runtimeTreeSha256, dependencies: dependencies.length }));
