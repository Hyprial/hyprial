import { readFile, readdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

async function sourceFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const location = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await sourceFiles(location)));
    else if (entry.name.endsWith('.ts')) files.push(location);
  }
  return files;
}

describe('client module boundary', () => {
  it('imports only local modules: no React, pages, state, features or components', async () => {
    const files = await sourceFiles(resolve('src/client'));

    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const source = await readFile(file, 'utf8');
      for (const match of source.matchAll(/\bfrom\s+['"]([^'"]+)['"]/g)) {
        const specifier = match[1];
        expect(specifier.startsWith('.'), `${file} imports ${specifier}`).toBe(true);
        expect(specifier, `${file} imports ${specifier}`).not.toMatch(
          /react|@\/|components|features|pages|state/,
        );
      }
    }
  });
});
