/*
 * Decision record (unavailable copy to notifications, PR 1f/F0, 2026-11-14):
 * - Owner rule (ui-development-standard §4.7): a function that is
 *   unavailable is not explained on the page. No capability port means the
 *   control is not rendered; a refused call is reported through the
 *   notification mechanism when the user operates it.
 * - This gate scans every string literal, template fragment and JSX text of
 *   gui/src (comments, identifiers and module specifiers are not visited)
 *   for the unavailable vocabulary. Each hit must be registered in exactly
 *   one map of unavailable-copy.allow.ts, and each registered entry must
 *   still be found: the maps are EXACT. Exactness is all this file enforces;
 *   it cannot stop an entry being added together with new banned copy.
 *   Shrink-only is enforced in CI by scripts/ci/check_gui_unavailable_copy.mjs,
 *   which compares the ledger with the one at the base branch.
 * - English words (unavailable, unsupported) are matched as whole words and
 *   only in prose: a lowercase machine token such as 'unavailable' or
 *   'model-unavailable' is a status code, not copy, and is skipped.
 * - The vocabulary, exclusions, scan root and ledger module live in
 *   unavailable-copy.scan.ts; the base-comparison script fails when that
 *   file is narrowed (see its header). Scanned files are .ts/.tsx/.mts and
 *   .js/.mjs/.cjs under gui/src, plus the quoted strings of .css files;
 *   only the exact generated client files are skipped.
 * - The scan is lexical. Text assembled at run time (string concatenation,
 *   join, computed keys) is out of its reach and is not detected.
 * - A second scan covers JSX `disabled` without an initializer or set to
 *   true and `aria-disabled` set to "true" or true (always disabled
 *   controls) outside the primitives, and a third the placeholder
 *   components, both against per-file count maps. An <option> placeholder is
 *   not a control and is not scanned.
 * - New vocabulary hits are registered with a reason in the PR: STATE for
 *   runtime state or error text of a request that was made, NOTIFICATION for
 *   text that is only passed to notify or returned as an operation result,
 *   PENDING_* for copy that a later PR removes.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import {
  ENGLISH_BOUNDARY_AFTER,
  ENGLISH_BOUNDARY_BEFORE,
  ENGLISH_TERMS,
  LEDGER as allow,
  MACHINE_TOKEN as MACHINE_TOKEN_SOURCE,
  SCAN_ROOT,
  SKIPPED_DIRECTORIES,
  SKIPPED_FILES,
  SOURCE_EXTENSIONS,
  VOCABULARY_TERMS,
} from './unavailable-copy.scan';

const SRC_ROOT = resolve(process.cwd(), SCAN_ROOT);

const VOCABULARY = new RegExp(VOCABULARY_TERMS.join('|'), 'i');
const ENGLISH_WORD = new RegExp(
  `${ENGLISH_BOUNDARY_BEFORE}(?:${ENGLISH_TERMS.join('|')})${ENGLISH_BOUNDARY_AFTER}`,
  'i',
);
const MACHINE_TOKEN = new RegExp(MACHINE_TOKEN_SOURCE);
const CSS_STRING = /"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'/g;
const CSS_COMMENT = /\/\*[\s\S]*?\*\//g;

function hits(value: string): boolean {
  return VOCABULARY.test(value) || (ENGLISH_WORD.test(value) && !MACHINE_TOKEN.test(value));
}

const PLACEHOLDER_COMPONENTS = new Set([
  'UnsupportedState',
  'ReservedTableCard',
  'UnavailableBlock',
  'UnavailableSlot',
]);

const PERMANENT = ['STATE', 'NOTIFICATION', 'ELSEWHERE', 'LOGIC_NOT_COPY'] as const;
const COUNTS = ['PENDING_DISABLED', 'PENDING_COMPONENTS'] as const;
const PENDING = [
  'PENDING_SHELL',
  'PENDING_AGENTS',
  'PENDING_ORG',
  'PENDING_CHAT',
  'PENDING_CONNECTIONS',
  'PENDING_SETTINGS',
  'PENDING_REMAINDER',
] as const;

type Hits = Map<string, Set<string>>;

interface Scan {
  readonly copy: Hits;
  readonly disabled: Map<string, number>;
  readonly components: Map<string, number>;
}

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      sourceFiles(path, out);
    } else if (SOURCE_EXTENSIONS.some((extension) => entry.endsWith(extension))) {
      out.push(path);
    }
  }
  return out;
}

function isModuleSpecifier(node: ts.StringLiteral): boolean {
  const parent = node.parent;
  return (
    ts.isImportDeclaration(parent) ||
    ts.isExportDeclaration(parent) ||
    ts.isExternalModuleReference(parent) ||
    (ts.isLiteralTypeNode(parent) && ts.isImportTypeNode(parent.parent)) ||
    (ts.isCallExpression(parent) && parent.expression.kind === ts.SyntaxKind.ImportKeyword)
  );
}

function isTrue(initializer: ts.JsxAttributeValue): boolean {
  if (ts.isStringLiteral(initializer)) {
    return initializer.text === 'true';
  }
  return (
    ts.isJsxExpression(initializer) &&
    initializer.expression?.kind === ts.SyntaxKind.TrueKeyword
  );
}

function scriptKind(file: string): ts.ScriptKind {
  if (file.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (/\.(?:js|mjs|cjs)$/.test(file)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

function skipped(rel: string): boolean {
  return (
    (SKIPPED_FILES as readonly string[]).includes(rel) ||
    (SKIPPED_DIRECTORIES as readonly string[]).some((dir) => rel.startsWith(`${dir}/`))
  );
}

function scan(): Scan {
  const result: Scan = { copy: new Map(), disabled: new Map(), components: new Map() };
  for (const file of sourceFiles(SRC_ROOT)) {
    const rel = relative(SRC_ROOT, file).split('\\').join('/');
    if (skipped(rel)) {
      continue;
    }
    const text = readFileSync(file, 'utf8');
    const record = (raw: string): void => {
      const value = raw.trim();
      if (value && hits(value)) {
        const set = result.copy.get(rel) ?? new Set<string>();
        set.add(value);
        result.copy.set(rel, set);
      }
    };
    const bump = (map: Map<string, number>): void => {
      map.set(rel, (map.get(rel) ?? 0) + 1);
    };
    if (file.endsWith('.css')) {
      for (const match of text.replace(CSS_COMMENT, '').matchAll(CSS_STRING)) {
        record(match[1] ?? match[2] ?? '');
      }
      continue;
    }
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind(file));
    const visit = (node: ts.Node): void => {
      if (
        (ts.isStringLiteral(node) && !isModuleSpecifier(node)) ||
        ts.isNoSubstitutionTemplateLiteral(node) ||
        ts.isTemplateHead(node) ||
        ts.isTemplateMiddle(node) ||
        ts.isTemplateTail(node) ||
        ts.isJsxText(node)
      ) {
        record(node.text);
      }
      if (ts.isJsxAttribute(node) && !/^components\/(primitives|ui)\//.test(rel)) {
        const name = node.name.getText();
        const initializer = node.initializer;
        const alwaysTrue =
          name === 'disabled'
            ? !initializer || isTrue(initializer)
            : name === 'aria-disabled' && !!initializer && isTrue(initializer);
        if (alwaysTrue && node.parent.parent.tagName.getText() !== 'option') {
          bump(result.disabled);
        }
      }
      if (
        (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
        PLACEHOLDER_COMPONENTS.has(node.tagName.getText())
      ) {
        bump(result.components);
      }
      node.forEachChild(visit);
    };
    visit(source);
  }
  return result;
}

function listed(name: string): Record<string, readonly string[]> {
  return (allow as unknown as Record<string, Record<string, readonly string[]>>)[name] ?? {};
}

function countMap(name: string): Record<string, number> {
  return (allow as unknown as Record<string, Record<string, number>>)[name] ?? {};
}

function pendingEntries(): string[] {
  return PENDING.flatMap((name) =>
    Object.entries(listed(name)).flatMap(([path, texts]) => texts.map((text) => `${path}\t${text}`)),
  );
}

function entriesOf(names: readonly string[]): string[] {
  return names.flatMap((name) =>
    Object.entries(listed(name)).flatMap(([path, texts]) => texts.map((text) => `${path}\t${text}`)),
  );
}

function flatten(hits: Hits): string[] {
  return [...hits].flatMap(([path, texts]) => [...texts].map((text) => `${path}\t${text}`));
}

describe('static unavailable copy gate', () => {
  const found = scan();
  const foundEntries = new Set(flatten(found.copy));

  it('registers every vocabulary hit in exactly one map', () => {
    const registered = [...entriesOf(PERMANENT), ...pendingEntries()];
    const unregistered = [...foundEntries].filter((entry) => !registered.includes(entry)).sort();
    expect(unregistered).toEqual([]);
    const duplicated = registered.filter((entry, index) => registered.indexOf(entry) !== index);
    expect(duplicated.sort()).toEqual([]);
  });

  it('exports only the known maps', () => {
    expect(Object.keys(allow).sort()).toEqual([...PERMANENT, ...PENDING, ...COUNTS].sort());
  });

  it('lists no STATE, NOTIFICATION, LOGIC_NOT_COPY or PENDING entry that no longer exists', () => {
    const stale = [...entriesOf(['STATE', 'NOTIFICATION', 'LOGIC_NOT_COPY']), ...pendingEntries()]
      .filter((entry) => !foundEntries.has(entry))
      .sort();
    expect(stale).toEqual([]);
  });

  it('keeps the always-disabled control list exact', () => {
    expect(Object.fromEntries([...found.disabled].sort())).toEqual(countMap('PENDING_DISABLED'));
  });

  it('keeps the placeholder component list exact', () => {
    expect(Object.fromEntries([...found.components].sort())).toEqual(countMap('PENDING_COMPONENTS'));
  });
});
