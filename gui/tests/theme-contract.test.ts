/*
 * Theme reuse contract (hard gate).
 *
 * Decision record (Allen, 2026-11-11):
 * - The theme must be replaceable as a whole, and later development must reuse it.
 *   This test is the hard gate for that; it runs inside `npm --prefix gui test`, which
 *   CI already requires, so no protected workflow file needs to change.
 * - Scope: src/**\/*.css and src/**\/*.tsx, excluding src/dev and src/client/generated.
 * - Rules, one `it` each:
 *   1. Colour, font-family and box-shadow literals exist only under src/styles/themes/.
 *   2. The theme file's light and dark blocks declare the same variable names and carry
 *      the full shadcn set.
 *   3. Only tokens.css and shadcn.css may reference theme variables; everything else uses --ui-*.
 *   4. --ui-* is declared only in tokens.css; theme names only in the theme directory.
 *   5. Every var(--name) reference has a declaration.
 *   6. Every --color-* in shadcn.css maps to a theme variable and every shadcn colour is mapped.
 *   7. The theme imports the bundled Noto Sans SC stylesheet and both font stacks name it (Allen 2026-11-11).
 * - Exceptions are exact entries in the lists below, each with its reason; keep them minimal.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const GUI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(GUI_ROOT, 'src');
const THEME_DIR = 'src/styles/themes/';
const TOKENS = 'src/styles/tokens.css';
const SHADCN = 'src/styles/shadcn.css';
const THEME_FILE = 'src/styles/themes/hyprial-v5.css';

const SHADCN_NAMES = [
  'background', 'foreground', 'card', 'card-foreground', 'popover', 'popover-foreground',
  'primary', 'primary-foreground', 'secondary', 'secondary-foreground', 'muted',
  'muted-foreground', 'accent', 'accent-foreground', 'destructive', 'border', 'input', 'ring',
  'radius', 'sidebar', 'sidebar-foreground', 'sidebar-primary', 'sidebar-primary-foreground',
  'sidebar-accent', 'sidebar-accent-foreground', 'sidebar-border', 'sidebar-ring',
  'chart-1', 'chart-2', 'chart-3', 'chart-4', 'chart-5',
];

const SCHEME_INDEPENDENT = ['radius', 'font-sans', 'font-cn', 'font-mono'];

const THEME_NAME_DECLARATION_EXCEPTIONS: { file: string; name: string; reason: string }[] = [
  {
    file: SHADCN,
    name: 'font-sans',
    reason: 'Tailwind theme namespace inside @theme inline; emitted with the tw prefix and re-pointed at --ui-font-ui',
  },
  {
    file: SHADCN,
    name: 'font-mono',
    reason: 'Tailwind theme namespace inside @theme inline; emitted with the tw prefix and re-pointed at --ui-font-mono',
  },
];

const IGNORED_REFERENCE = /^--(tw-|radix-)/;

type Source = { file: string; text: string; kind: 'css' | 'tsx' };

function walk(directory: string, out: string[] = []): string[] {
  for (const name of readdirSync(directory).sort()) {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

function stripCssComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, ' '));
}

function stripTsComments(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:"'`\\])\/\/[^\n]*/g, (match, lead: string) => lead + ' '.repeat(match.length - lead.length));
}

function collect(): Source[] {
  const sources: Source[] = [];
  for (const path of walk(SRC)) {
    const file = relative(GUI_ROOT, path).split(sep).join('/');
    if (file.startsWith('src/dev/') || file.startsWith('src/client/generated/')) continue;
    if (file.endsWith('.css')) {
      sources.push({ file, kind: 'css', text: stripCssComments(readFileSync(path, 'utf8')) });
    } else if (file.endsWith('.tsx')) {
      sources.push({ file, kind: 'tsx', text: stripTsComments(readFileSync(path, 'utf8')) });
    }
  }
  return sources;
}

const sources = collect();
const outsideTheme = sources.filter((source) => !source.file.startsWith(THEME_DIR));
const themeSource = sources.find((source) => source.file === THEME_FILE);
const tokensSource = sources.find((source) => source.file === TOKENS);
const shadcnSource = sources.find((source) => source.file === SHADCN);

function lineOf(text: string, index: number): number {
  return text.slice(0, index).split('\n').length;
}

function snippet(text: string, index: number, length = 90): string {
  return text.slice(index, index + length).split('\n')[0].trim();
}

type Declaration = { prop: string; value: string; index: number };

function declarations(text: string): Declaration[] {
  const out: Declaration[] = [];
  for (const match of text.matchAll(/(?:^|[;{}\s])(-{0,2}[a-zA-Z][\w-]*)\s*:\s*([^;{}]+)/g)) {
    const index = (match.index ?? 0) + match[0].indexOf(match[1]);
    out.push({ prop: match[1].toLowerCase(), value: match[2].trim(), index });
  }
  return out;
}

const COLOR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch|oklab|lab|lch|hwb)\(/;
const NAMED_COLOR = /^(?:white|black|red|green|blue|gray|grey|orange|yellow|purple|pink|silver|navy|teal)$/i;
const COLOR_PROPS = /^(?:color|background|background-color|border-color|outline-color|fill|stroke|caret-color|accent-color|text-decoration-color|column-rule-color)$/;
const SHADOW_OK = [
  /^none$/,
  /^inherit$/,
  /^var\(--ui-(?:shadow-[a-z-]+|edge)\)$/,
];
const RING_SHADOW = /^(?:inset )?0 0 0 [^,]+$/;

function stripVars(value: string): string {
  let out = value;
  let previous = '';
  while (out !== previous) {
    previous = out;
    out = out.replace(/var\([^()]*\)/g, 'VAR');
  }
  return out;
}

function shadowAllowed(value: string): boolean {
  if (SHADOW_OK.some((pattern) => pattern.test(value))) return true;
  const layers = stripVars(value).split(/,(?![^(]*\))/).map((layer) => layer.trim());
  const rawLayers = value.split(/,(?![^(]*\))/).map((layer) => layer.trim());
  return rawLayers.every((layer, position) => {
    if (SHADOW_OK.some((pattern) => pattern.test(layer))) return true;
    if (!RING_SHADOW.test(layers[position].replace(/var\([^)]*\)/g, 'VAR'))) return false;
    return !COLOR_LITERAL.test(layer);
  });
}

function balancedObject(text: string, start: number): string {
  let depth = 0;
  for (let index = start; index < text.length; index += 1) {
    if (text[index] === '{') depth += 1;
    if (text[index] === '}') {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return text.slice(start);
}

function themeBlocks(): { selector: string; names: string[] }[] {
  const text = themeSource?.text ?? '';
  const blocks: { selector: string; names: string[] }[] = [];
  for (const match of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = match[1].trim().replace(/\s+/g, ' ');
    if (selector.startsWith('@')) continue;
    const names = [...match[2].matchAll(/(?:^|[;\s])(--[a-z0-9-]+)\s*:/g)].map((entry) => entry[1].slice(2));
    blocks.push({ selector, names });
  }
  return blocks;
}

function themeNames(): Set<string> {
  return new Set(themeBlocks().flatMap((block) => block.names));
}

function format(violations: string[]): string[] {
  return violations;
}

describe('theme contract', () => {
  it('reads the real theme, tokens and shadcn files', () => {
    expect(themeSource?.text.length ?? 0).toBeGreaterThan(1_000);
    expect(tokensSource?.text.length ?? 0).toBeGreaterThan(1_000);
    expect(shadcnSource?.text.length ?? 0).toBeGreaterThan(500);
    expect(outsideTheme.length).toBeGreaterThan(20);
  });

  it('keeps colour, font-family and shadow literals inside the theme directory', () => {
    const violations: string[] = [];
    for (const source of outsideTheme) {
      if (source.kind === 'css') {
        for (const { prop, value, index } of declarations(source.text)) {
          const where = `${source.file}:${lineOf(source.text, index)} ${prop}: ${value.slice(0, 80)}`;
          if (COLOR_LITERAL.test(value)) violations.push(`${where}  [colour literal]`);
          else if (COLOR_PROPS.test(prop) && NAMED_COLOR.test(value)) violations.push(`${where}  [named colour]`);
          if (prop === 'font-family' && !/^(?:var\(--ui-font-[a-z-]+\)|inherit)$/.test(value)) {
            violations.push(`${where}  [font-family must be var(--ui-font-*) or inherit]`);
          }
          if (prop === 'font' && /["']/.test(value)) violations.push(`${where}  [font shorthand names a family]`);
          if (prop === 'box-shadow' && !shadowAllowed(value)) {
            violations.push(`${where}  [box-shadow must be none, var(--ui-shadow-*), var(--ui-edge) or a var() ring]`);
          }
        }
      } else {
        for (const match of source.text.matchAll(/style=\{\{/g)) {
          const start = (match.index ?? 0) + 'style={'.length;
          const body = balancedObject(source.text, start);
          if (COLOR_LITERAL.test(body)) {
            violations.push(`${source.file}:${lineOf(source.text, start)} ${snippet(source.text, start)}  [colour literal in style]`);
          }
        }
        for (const match of source.text.matchAll(/\b(?:fill|stroke|stopColor|floodColor)=["'](#[0-9a-fA-F]{3,8}|(?:rgb|hsl)a?\([^"']*)["']/g)) {
          violations.push(`${source.file}:${lineOf(source.text, match.index ?? 0)} ${match[0]}  [colour literal attribute]`);
        }
      }
    }
    expect(format(violations), '主题文件之外不得出现颜色、字体族或阴影字面量；改用 --ui-* token').toEqual([]);
  });

  it('declares the same variable names in the light and dark blocks, with the full shadcn set', () => {
    const blocks = themeBlocks();
    const light = blocks.find((block) => block.selector.startsWith(':root'));
    const dark = blocks.find((block) => block.selector.startsWith('.dark'));
    expect(light, 'light block (:root, [data-theme="light"])').toBeTruthy();
    expect(dark, 'dark block (.dark, [data-theme="dark"])').toBeTruthy();
    const lightNames = new Set(light!.names);
    const darkNames = new Set(dark!.names);
    const missingInDark = [...lightNames].filter((name) => !darkNames.has(name) && !SCHEME_INDEPENDENT.includes(name));
    const onlyInDark = [...darkNames].filter((name) => !lightNames.has(name));
    const staleIndependent = SCHEME_INDEPENDENT.filter((name) => !lightNames.has(name) || darkNames.has(name));
    expect({ missingInDark, onlyInDark, staleIndependent }).toEqual({
      missingInDark: [],
      onlyInDark: [],
      staleIndependent: [],
    });
    const missingShadcn = SHADCN_NAMES.filter((name) => !lightNames.has(name));
    const missingShadcnDark = SHADCN_NAMES.filter((name) => !SCHEME_INDEPENDENT.includes(name) && !darkNames.has(name));
    expect({ missingShadcn, missingShadcnDark }).toEqual({ missingShadcn: [], missingShadcnDark: [] });
    for (const name of ['destructive-foreground', 'success', 'warning', 'info', 'font-sans', 'font-mono']) {
      expect(lightNames.has(name), `--${name} in light`).toBe(true);
    }
    expect(light!.selector).toContain('[data-theme="light"]');
    expect(dark!.selector).toContain('[data-theme="dark"]');
  });

  it('lets only tokens.css and shadcn.css reference theme variables', () => {
    const names = themeNames();
    const violations: string[] = [];
    for (const source of outsideTheme) {
      if (source.file === TOKENS || source.file === SHADCN) continue;
      for (const match of source.text.matchAll(/var\(\s*--([a-z0-9-]+)/g)) {
        if (names.has(match[1])) {
          violations.push(`${source.file}:${lineOf(source.text, match.index ?? 0)} var(--${match[1]})  [use the matching --ui-* token]`);
        }
      }
    }
    expect(violations, '组件只消费 --ui-*，主题变量只由 tokens.css / shadcn.css 引用').toEqual([]);
  });

  it('declares --ui-* only in tokens.css and theme names only in the theme directory', () => {
    const names = themeNames();
    const violations: string[] = [];
    for (const source of outsideTheme) {
      if (source.kind !== 'css') continue;
      for (const { prop, index } of declarations(source.text)) {
        if (!prop.startsWith('--')) continue;
        const name = prop.slice(2);
        const where = `${source.file}:${lineOf(source.text, index)} ${prop}`;
        if (prop.startsWith('--ui-') && source.file !== TOKENS) {
          violations.push(`${where}  [--ui-* is declared only in tokens.css]`);
        }
        if (names.has(name)) {
          const allowed = THEME_NAME_DECLARATION_EXCEPTIONS.some(
            (entry) => entry.file === source.file && entry.name === name,
          );
          if (!allowed) violations.push(`${where}  [theme variable names are declared only in ${THEME_DIR}]`);
        }
      }
    }
    for (const source of outsideTheme) {
      if (source.kind !== 'tsx') continue;
      for (const match of source.text.matchAll(/['"](--[a-z0-9-]+)['"]\s*:/g)) {
        const name = match[1].slice(2);
        if (match[1].startsWith('--ui-') || names.has(name)) {
          violations.push(`${source.file}:${lineOf(source.text, match.index ?? 0)} ${match[1]}  [inline style may not set --ui-* or theme names]`);
        }
      }
    }
    expect(violations, '不得私自定义或覆盖 --ui-* 与主题变量').toEqual([]);
  });

  it('has no dangling var() references', () => {
    const declared = new Set<string>();
    for (const source of sources) {
      if (source.kind === 'css') {
        for (const { prop } of declarations(source.text)) if (prop.startsWith('--')) declared.add(prop);
      } else {
        for (const match of source.text.matchAll(/['"](--[a-z0-9-]+)['"]\s*:/g)) declared.add(match[1]);
      }
    }
    const violations: string[] = [];
    for (const source of sources) {
      for (const match of source.text.matchAll(/var\(\s*(--[a-z0-9-]+)/g)) {
        const name = match[1];
        if (IGNORED_REFERENCE.test(name) || declared.has(name)) continue;
        violations.push(`${source.file}:${lineOf(source.text, match.index ?? 0)} var(${name})  [no declaration]`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('maps every shadcn colour in @theme inline to a theme variable', () => {
    const text = shadcnSource?.text ?? '';
    const names = themeNames();
    const mapped = new Map<string, string>();
    const violations: string[] = [];
    for (const match of text.matchAll(/(--color-[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
      const value = match[2].trim();
      const target = /^var\(--([a-z0-9-]+)\)$/.exec(value);
      mapped.set(match[1], value);
      if (!target || !names.has(target[1])) {
        violations.push(`${SHADCN}:${lineOf(text, match.index ?? 0)} ${match[1]}: ${value}  [must be var(--<theme variable>)]`);
      }
    }
    const colourNames = SHADCN_NAMES.filter((name) => name !== 'radius');
    for (const name of colourNames) {
      if (!mapped.has(`--color-${name}`)) violations.push(`${SHADCN}: missing --color-${name}`);
    }
    expect(violations).toEqual([]);
  });

  it('bundles Noto Sans SC through the theme and keeps it in both font stacks', () => {
    const text = themeSource?.text ?? '';
    expect(text).toMatch(/@import\s+["']@fontsource-variable\/noto-sans-sc\/wght\.css["']/);
    const stack = (name: string) => new RegExp(`--${name}:\\s*([^;]+);`).exec(text)?.[1] ?? '';
    for (const name of ['font-sans', 'font-cn']) {
      expect(stack(name), `--${name}`).toContain('"Noto Sans SC Variable"');
    }
    expect(/@import\s+(?:url\()?["']?(?:https?:)?\/\//.test(text), 'no remote @import').toBe(false);
    expect(/url\(\s*["']?(?:https?:)?\/\//.test(text), 'no remote font url').toBe(false);
  });
});
