/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): static import-graph
 * gate for the floating tool layer. The follow-up that ships the layer next
 * to the real backend needs a tool chunk without simulated data, so the
 * layer's transitive import graph is pinned to an exact allowlist (type-only
 * imports are followed too, which is stricter than the bundler). The fixture
 * composition root must reach the layer only through a dynamic import.
 * - Mount order is pinned as source order: the browser gates probe the layer
 *   as soon as the product root has children, so the layer is mounted first
 *   and creates its portal host in a layout effect (measured 2026-11-14: with
 *   a passive effect the layer landed 50-100 ms after the product root and
 *   the browser parity gate failed).
 */
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const SRC_ROOT = resolve(process.cwd(), 'src');

const TOOL_LAYER_GRAPH = [
  'dev/DevModuleIndex.tsx',
  'dev/dev-module-index.css',
  'dev/module-registry.ts',
  'dev/tools/impl/DevToolsLayer.tsx',
  'dev/tools/impl/api-docs.css',
  'dev/tools/impl/context.ts',
  'dev/tools/impl/mount.tsx',
  'dev/tools/index.ts',
  'state/gui-view-model.ts',
];

const FORBIDDEN_PATH = /(^|\/)(mock|fixtures?|fixture-tools|session|org)(\/|$)|scenario|parity-composition|DevPreviewApp|SessionToolbar/i;
const FORBIDDEN_TEXT = /hyprialScenario|hyprialFixture|hyprialFirstRun|fixture|mock/i;

interface ImportEdge {
  readonly specifier: string;
  readonly dynamic: boolean;
  readonly typeOnly: boolean;
}

function resolveSpecifier(fromFile: string, specifier: string): string | null {
  let base: string;
  if (specifier.startsWith('@/')) {
    base = resolve(SRC_ROOT, specifier.slice(2));
  } else if (specifier.startsWith('.')) {
    base = resolve(dirname(fromFile), specifier);
  } else {
    return null;
  }
  const candidates = [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')];
  const found = candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile());
  if (!found) {
    throw new Error(`unresolved import "${specifier}" from ${fromFile}`);
  }
  return found;
}

function importEdges(file: string): ImportEdge[] {
  if (!/\.tsx?$/.test(file)) {
    return [];
  }
  const text = readFileSync(file, 'utf8');
  const source = ts.createSourceFile(
    file,
    text,
    ts.ScriptTarget.Latest,
    true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const edges: ImportEdge[] = [];
  const visit = (node: ts.Node): void => {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      const typeOnly = ts.isImportDeclaration(node)
        ? node.importClause?.isTypeOnly === true
        : node.isTypeOnly;
      edges.push({ specifier: node.moduleSpecifier.text, dynamic: false, typeOnly });
    }
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const argument = node.arguments[0];
      if (argument && ts.isStringLiteral(argument)) {
        edges.push({ specifier: argument.text, dynamic: true, typeOnly: false });
      }
    }
    node.forEachChild(visit);
  };
  source.forEachChild(visit);
  return edges;
}

function toRelative(file: string): string {
  return relative(SRC_ROOT, file).split('\\').join('/');
}

function reachableFrom(entry: string): string[] {
  const seen = new Set<string>();
  const queue = [resolve(SRC_ROOT, entry)];
  while (queue.length > 0) {
    const file = queue.pop()!;
    if (seen.has(file)) {
      continue;
    }
    seen.add(file);
    for (const edge of importEdges(file)) {
      const target = resolveSpecifier(file, edge.specifier);
      if (target) {
        queue.push(target);
      }
    }
  }
  return [...seen].map(toRelative).sort();
}

describe('floating tool layer import graph', () => {
  it('reaches exactly the allowlisted modules', () => {
    expect(reachableFrom('dev/tools/index.ts')).toEqual([...TOOL_LAYER_GRAPH].sort());
  });

  it('reaches no scenario, session, mock or fixture module', () => {
    const offenders = reachableFrom('dev/tools/index.ts').filter((file) => FORBIDDEN_PATH.test(file));
    expect(offenders).toEqual([]);
  });

  it('carries no simulated-data vocabulary in its TypeScript sources', () => {
    const offenders = reachableFrom('dev/tools/index.ts')
      .filter((file) => /\.tsx?$/.test(file))
      .filter((file) => {
        const text = readFileSync(resolve(SRC_ROOT, file), 'utf8');
        const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
        const withoutComments = ts.createPrinter({ removeComments: true }).printFile(source);
        return FORBIDDEN_TEXT.test(withoutComments);
      });
    expect(offenders).toEqual([]);
  });

  it('is imported by the fixture tools for types only', () => {
    const fixtureToolFiles = reachableFrom('dev/fixture-tools/index.ts')
      .filter((file) => file.startsWith('dev/fixture-tools/'));
    expect(fixtureToolFiles.length).toBeGreaterThan(0);
    const runtimeEdges: string[] = [];
    for (const file of fixtureToolFiles) {
      const absolute = resolve(SRC_ROOT, file);
      for (const edge of importEdges(absolute)) {
        const target = resolveSpecifier(absolute, edge.specifier);
        if (target && toRelative(target).startsWith('dev/tools/') && !edge.typeOnly) {
          runtimeEdges.push(`${file} -> ${edge.specifier}`);
        }
      }
    }
    expect(runtimeEdges).toEqual([]);
  });
});

describe('fixture composition root reaches the tool layer through the seam', () => {
  const compositionFile = resolve(SRC_ROOT, 'dev/parity-composition.tsx');
  const edges = importEdges(compositionFile).map((edge) => ({
    ...edge,
    target: toRelative(resolveSpecifier(compositionFile, edge.specifier) ?? edge.specifier),
  }));

  it('loads the tool layer with exactly one dynamic import', () => {
    const toolEdges = edges.filter((edge) => edge.target === 'dev/tools/index.ts');
    expect(toolEdges).toHaveLength(1);
    expect(toolEdges[0]?.dynamic).toBe(true);
  });

  it('does not statically import modules that bundle the tool layer', () => {
    const staticTargets = edges.filter((edge) => !edge.dynamic).map((edge) => edge.target);
    expect(staticTargets).not.toContain('dev/index.ts');
    expect(staticTargets).not.toContain('dev/DevPreviewApp.tsx');
    expect(staticTargets.filter((target) => target.startsWith('dev/tools/'))).toEqual([]);
    expect(staticTargets).toContain('dev/fixture-tools/index.ts');
  });

  it('mounts the tools through mountDevTools and not through its own React root', () => {
    const text = readFileSync(compositionFile, 'utf8');
    expect(text).toMatch(/mountDevTools\(ensureDevToolsContainer\(root\), createFixtureDevToolsContext\(\)\)/);
    expect(text).not.toMatch(/<DevPreviewApp/);
    expect(text.match(/createRoot\(/g)).toHaveLength(2);
  });

  it('mounts the tool layer before it renders the product root', () => {
    const text = readFileSync(compositionFile, 'utf8');
    const devEntry = text.slice(text.indexOf('export async function renderDevEntry'));
    const importAt = devEntry.indexOf("await import('./tools')");
    const mountAt = devEntry.indexOf('mountDevTools(');
    const productRenderAt = devEntry.indexOf('createRoot(root).render(');
    expect(importAt).toBeGreaterThan(-1);
    expect(mountAt).toBeGreaterThan(importAt);
    expect(productRenderAt).toBeGreaterThan(mountAt);
  });

  it('creates the portal host in a layout effect so the layer commits synchronously', () => {
    const layer = readFileSync(resolve(SRC_ROOT, 'dev/tools/impl/DevToolsLayer.tsx'), 'utf8');
    expect(layer).toMatch(/useLayoutEffect\(\(\) => \{\s*const element = document\.createElement\('div'\);/);
  });

  it('loads the tool layer from the formal entry with one dynamic import, after connected(), guarded by session.devTools', () => {
    const entryFile = resolve(SRC_ROOT, 'main.tsx');
    const toolEdges = importEdges(entryFile)
      .map((edge) => ({ ...edge, target: toRelative(resolveSpecifier(entryFile, edge.specifier) ?? edge.specifier) }))
      .filter((edge) => edge.target.startsWith('dev/'));
    expect(toolEdges.filter((edge) => edge.target.startsWith('dev/tools/'))).toHaveLength(1);
    expect(toolEdges.find((edge) => edge.target === 'dev/tools/index.ts')?.dynamic).toBe(true);
    expect(toolEdges.filter((edge) => !edge.dynamic)).toEqual([]);

    const text = readFileSync(entryFile, 'utf8');
    const connectedAt = text.indexOf('connected(bootstrap);');
    const guardAt = text.indexOf('bootstrap.session.devTools');
    const importAt = text.indexOf("await import('./dev/tools')");
    const mountAt = text.indexOf('mountDevTools(');
    expect(connectedAt).toBeGreaterThan(-1);
    expect(guardAt).toBeGreaterThan(connectedAt);
    expect(importAt).toBeGreaterThan(guardAt);
    expect(mountAt).toBeGreaterThan(importAt);
    expect(text).toMatch(/mountDevTools\(ensureDevToolsContainer\(root\), \{ dataSource: 'backend', apiDocs: devTools\.apiDocs \}\)/);
    expect(text.match(/mountDevTools\(/g)).toHaveLength(1);
  });
});
