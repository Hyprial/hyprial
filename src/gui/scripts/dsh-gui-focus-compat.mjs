// Apply only to an isolated candidate runtime; never edit a running release.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { applyDshHistoryCompatibility } from './dsh-history-compat.mjs';

const manifest = JSON.parse(readFileSync(new URL('../packages/dsh-gui-focus-compat/patch.json', import.meta.url), 'utf8'));
export function applyDshGuiFocusCompatibility(runtime) {
  // Reuse the audited, atomic, whole-candidate validation discipline.
  return applyDshHistoryCompatibility(runtime, manifest);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 3) throw new Error('Usage: dsh-gui-focus-compat.mjs candidate-runtime-directory');
  console.log(JSON.stringify(applyDshGuiFocusCompatibility(process.argv[2])));
}
