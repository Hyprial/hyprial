import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

function readProjectFile(relativePath: string): string {
  const candidates: string[] = [];

  if (import.meta.url.startsWith('file:')) {
    candidates.push(
      resolve(dirname(fileURLToPath(import.meta.url)), '..', relativePath),
    );
  }

  candidates.push(
    resolve(process.cwd(), relativePath),
    resolve(process.cwd(), 'gui', relativePath),
  );

  for (const candidate of candidates) {
    try {
      return readFileSync(candidate, 'utf8');
    } catch {
      // Try the next supported invocation directory.
    }
  }

  throw new Error(`Unable to read ${relativePath} from ${process.cwd()}`);
}

const tokens = readProjectFile('src/styles/tokens.css');
const theme = readProjectFile('src/styles/themes/hyprial-v5.css');
const primitivesCss = readProjectFile('src/styles/primitives.css');

const primitiveSourceFiles = [
  'Button.tsx',
  'Card.tsx',
  'DiagnosticBox.tsx',
  'Dialog.tsx',
  'Field.tsx',
  'Notice.tsx',
  'PageHeader.tsx',
  'Progress.tsx',
  'States.tsx',
  'Stepper.tsx',
  'types.ts',
] as const;

const primitiveSources = primitiveSourceFiles.map((file) => ({
  file,
  source: readProjectFile(`src/components/primitives/${file}`),
}));

function declaredUiTokens(css: string): Set<string> {
  return new Set(
    [...css.matchAll(/(--ui-[a-z0-9-]+)\s*:/g)].map((match) => match[1]),
  );
}

function referencedUiTokens(css: string): Set<string> {
  return new Set(
    [...css.matchAll(/var\((--ui-[a-z0-9-]+)/g)].map((match) => match[1]),
  );
}

describe('S1 token contract', () => {
  it('reads the real source files, not empty transformed output', () => {
    expect(tokens.length).toBeGreaterThan(1_000);
    expect(primitivesCss.length).toBeGreaterThan(1_000);
    for (const { file, source } of primitiveSources) {
      expect(source.length, `${file} must be non-empty`).toBeGreaterThan(100);
    }
  });

  it('retires the legacy variable layer and ships the shadcn theme file', () => {
    for (const legacy of [
      'ink',
      'ink-soft',
      'line',
      'line-soft',
      'paper',
      'green',
      'green-wash',
      'green-line',
      'selected',
      'selected-line',
      'focus',
      'font-ui',
      'font-brand',
      'shadow-window',
      'shadow-menu',
    ]) {
      expect(tokens, `--${legacy} must be retired`).not.toMatch(
        new RegExp(`(^|[^a-z-])--${legacy}\\s*:`),
      );
    }
    expect(tokens).not.toMatch(/--btn-/);
    expect(tokens).toContain('@import "./themes/hyprial-v5.css";');
    expect(theme).toContain('[data-theme="dark"]');

    for (const token of [
      '--ui-ink-1',
      '--ui-surface',
      '--ui-ground',
      '--ui-border',
      '--ui-accent',
      '--ui-focus',
      '--ui-positive',
      '--ui-danger',
      '--ui-warning',
      '--ui-space-1',
      '--ui-text-md',
      '--ui-radius-md',
      '--ui-shadow-card',
      '--ui-motion-base',
    ]) {
      expect(tokens).toMatch(new RegExp(`${token}\\s*:`));
    }
  });

  it('references only tokens declared in tokens.css', () => {
    const declared = declaredUiTokens(tokens);
    const referenced = referencedUiTokens(primitivesCss);

    for (const token of referenced) {
      expect(declared.has(token), `${token} must be declared in tokens.css`).toBe(
        true,
      );
    }
  });

  it('has no raw colours, external resources or old desktop min-width in primitives', () => {
    expect(/#[0-9a-fA-F]{3,8}\b/.test(primitivesCss)).toBe(false);
    expect(primitivesCss.includes('980px')).toBe(false);
    expect(primitivesCss.includes('@import')).toBe(false);
    expect(/url\(/.test(primitivesCss)).toBe(false);
    expect(/https?:\/\//.test(primitivesCss)).toBe(false);

    for (const { file, source } of primitiveSources) {
      expect(
        /#[0-9a-fA-F]{3,8}\b/.test(source),
        `${file} must not contain raw hex colours`,
      ).toBe(false);
      expect(
        /https?:\/\//.test(source),
        `${file} must not fetch external resources`,
      ).toBe(false);
      expect(
        /@import|url\(/.test(source),
        `${file} must not import external styles`,
      ).toBe(false);
    }
  });

  it('keeps reduced-motion fallbacks in the primitive layer', () => {
    expect(primitivesCss).toContain('@media (prefers-reduced-motion: reduce)');
    expect(primitivesCss).toContain('transition: none');
    expect(primitivesCss).toContain('animation: none');
  });
});
