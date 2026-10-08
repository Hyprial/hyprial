// Assemble the distributable static browser assets into dist/.
//
// There is no bundler in this product: the browser bundle is the checked-in
// ESM asset, and the receipt records exact bytes so packaging can verify what
// it ships. `npm run build` is idempotent and offline.
import { createHash } from 'node:crypto';
import { mkdir, readFile, rm, writeFile, copyFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const product = resolve(fileURLToPath(new URL('.', import.meta.url)));
const root = resolve(product, '..');
const outIndex = process.argv.indexOf('--out');
const out = outIndex === -1 ? join(root, 'dist') : resolve(process.argv[outIndex + 1] ?? '');

const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
// Prove the composition root imports cleanly before assets are stamped.
await import('./index.mjs');

await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
const files = {};
for (const name of ['client.js', 'style.css']) {
  const source = join(product, 'assets', name);
  const bytes = await readFile(source);
  await copyFile(source, join(out, name));
  files[name] = { bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
}
const receipt = {
  schema: 'hyprial.gui-product-build/v1',
  product: manifest.name,
  version: manifest.version,
  files,
};
await writeFile(join(out, 'manifest.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ status: 'built', out, files: Object.keys(files) }));
