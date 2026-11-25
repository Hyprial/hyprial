// gui/scripts/verify-dev-product-parity.mjs
//
// Decisions (gui dev/product parity, docs/design/gui-dev-product-parity-2026-11-09.md):
// - Static gate per target doc sections 2.2 and 4.3: product sources must not
//   contain import.meta.env.DEV/PROD branches, __HYPRIAL_DEV_* identifiers,
//   dev-only imports, or dev markers (dev-* classes/data attributes/devLabels).
// - Uses the pinned `typescript` compiler API (no new dependency) so that
//   comments, import specifiers and identifiers are distinguished precisely.
// - Dev-only material is path-classified (src/dev, src/dev-mode, src/fixtures,
//   src/features/org/dev, src/features/*/api/mock.*) plus any product import
//   reaching the tests/ tree or outside the gui root; dev-only files are exempt
//   from scanning so dev tools can use dev tokens freely.
// - __HYPRIAL_PARITY__ is the composition-root token of the dist-parity test
//   artifact (HYPRIAL_GUI_PARITY=1); __HYPRIAL_DEV_PREVIEW__ is the development
//   entry selector. Both may appear only in src/main.tsx under the explicit
//   exception below (each registered token is ratcheted: an unused registered
//   token fails the gate). Dev-only imports live in src/dev/parity-composition.tsx
//   under its own exception (ratcheted the same way). main.tsx may dynamically
//   import only that exact path; every other direct dev import stays red. The
//   registered dev-root exception is still checked even inside src/dev, and a
//   missing exception file fails closed. Ambient `declare const`
//   statements in .d.ts files are exempt (no runtime branch); runtime use of
//   dev tokens anywhere else is a violation. Legacy __HYPRIAL_DEV_* tokens stay
//   forbidden in all product runtime files.
// - DEV-UI-ORG-### (uppercase, exactly three digits, only under
//   src/features/org/) is a legacy product diagnostic-ID namespace that already
//   ships in the release: renaming the IDs would change diagnostic codes and
//   cross the contract red line. The exemption is exactly that narrow shape in
//   that scope; every lowercase, short-digit, suffixed or out-of-scope variant
//   and every other dev marker (dev-preview/dev-module/dev-session/dev-nav/
//   devLabels/…) and every import.meta.env branch stays forbidden. Brand-new
//   dev-only UI is caught by the browser parity gate instead.
// - Composition-root exceptions are a single explicit map; every exception must
//   remain exercised (file exists, registered token still used, declared dev
//   imports still present) or the gate fails, keeping the exception set minimal.
// - The gate fails closed: missing source root, missing composition root entry
//   or an empty source tree are hard failures, never a silent pass.
import ts from 'typescript';
import { existsSync, statSync } from 'node:fs';
import { readdir, readFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { posix } from 'node:path';
import { pathToFileURL } from 'node:url';
const DEV_ONLY_DIRECTORIES = [
  'src/dev',
  'src/dev-mode',
  'src/fixtures',
  'src/features/org/dev',
];
// src/features/onboarding/preview.tsx is the private preview.html entry (a
// composition root for the dev01 preview page); it composes the dev-only chat
// fixture by design and is never part of the product bundle inputs.
const DEV_ONLY_MODULE_PATTERNS = [/^src\/features\/[^/]+\/api\/mock\.tsx?$/u, /^src\/features\/onboarding\/preview\.tsx?$/u];
const FORBIDDEN_ENV_MEMBERS = new Set(['DEV', 'PROD']);
const DEV_TOKEN_PREFIX = '__HYPRIAL_DEV_';
const PARITY_TOKEN_PREFIX = '__HYPRIAL_PARITY';
const LEGACY_ORG_DIAGNOSTIC_SCOPE = 'src/features/org/';
const LEGACY_ORG_DIAGNOSTIC_PATTERN = /^DEV-UI-ORG-\d{3}$/;
const DEV_MARKER_PATTERN = /dev-(preview|module|session|scenario|data-source|nav-label|toolbar|footer|ui)[a-z0-9-]*/giu;
const DEV_IDENTIFIER_NAMES = new Set(['devLabels']);
const SOURCE_SUFFIXES = new Set(['.ts', '.tsx', '.css']);

export const COMPOSITION_ROOT_EXCEPTIONS = {
  'src/main.tsx': {
    allowTokens: ['__HYPRIAL_DEV_PREVIEW__', '__HYPRIAL_PARITY__'],
    allowDevImportSpecifiers: ['./dev/parity-composition', './dev/tools'],
  },
  'src/dev/parity-composition.tsx': {
    allowDevImports: true,
  },
};

export function isDevOnlyModule(relPath) {
  const normalized = posix.normalize(relPath);
  if (DEV_ONLY_DIRECTORIES.some(dir => normalized === dir || normalized.startsWith(`${dir}/`))) {
    return true;
  }
  return DEV_ONLY_MODULE_PATTERNS.some(pattern => pattern.test(normalized));
}

function toPosix(path) {
  return path.split('\\').join('/');
}

async function collectSourceFiles(srcRoot) {
  const collected = [];
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        await walk(path);
      } else if (SOURCE_SUFFIXES.has(entry.name.slice(entry.name.lastIndexOf('.')))) {
        collected.push(path);
      }
    }
  }
  await walk(srcRoot);
  collected.sort();
  return collected;
}

function resolveImportTarget(srcRoot, fromAbsolute, specifier) {
  const rootDir = dirname(srcRoot);
  let base;
  if (specifier.startsWith('@/')) {
    base = resolve(srcRoot, specifier.slice(2));
  } else if (specifier.startsWith('.')) {
    base = resolve(dirname(fromAbsolute), specifier);
  } else {
    return { kind: 'bare' };
  }
  const candidates = [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    `${base}.css`,
    `${base}.d.ts`,
    join(base, 'index.ts'),
    join(base, 'index.tsx'),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return { kind: 'file', path: candidate, rel: toPosix(relative(rootDir, candidate)) };
    }
  }
  return { kind: 'unresolved', rel: toPosix(relative(rootDir, base)) };
}

function isForbiddenImportTarget(target) {
  if (target.kind === 'bare') return null;
  if (target.rel.startsWith('..')) return 'outside the gui tree';
  if (target.rel.startsWith('tests/')) return 'tests tree material';
  if (isDevOnlyModule(target.rel)) return 'dev-only module';
  return null;
}

function lineOf(text, position) {
  return text.slice(0, position).split('\n').length;
}

function isLegacyOrgDiagnostic(relPath, matchedText) {
  return relPath.startsWith(LEGACY_ORG_DIAGNOSTIC_SCOPE)
    && LEGACY_ORG_DIAGNOSTIC_PATTERN.test(matchedText);
}

function scanMarkupLiteral(fileText, literalText, position, violations, file) {
  DEV_MARKER_PATTERN.lastIndex = 0;
  let match = DEV_MARKER_PATTERN.exec(literalText);
  while (match !== null) {
    if (!isLegacyOrgDiagnostic(file, match[0])) {
      violations.push({
        file,
        line: lineOf(fileText, position),
        rule: 'dev-marker',
        detail: `dev marker "${match[0]}" in string literal`,
      });
    }
    match = DEV_MARKER_PATTERN.exec(literalText);
  }
}

function scanTsFile(srcRoot, absolutePath, relPath, text, violations, exceptions, state) {
  const scriptKind = relPath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(absolutePath, text, ts.ScriptTarget.Latest, true, scriptKind);
  const exception = exceptions[relPath];
  const allowedTokens = new Set(exception?.allowTokens ?? []);
  const usedTokens = new Set();
  let usedDevImport = false;
  const isAllowedDevImport = (specifier) => exception?.allowDevImports === true
    || exception?.allowDevImportSpecifiers?.includes(specifier) === true;

  const visit = (node) => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      const specifier = node.moduleSpecifier;
      if (specifier && ts.isStringLiteral(specifier)) {
        const forbidden = isForbiddenImportTarget(
          resolveImportTarget(srcRoot, absolutePath, specifier.text),
        );
        if (forbidden) {
          if (isAllowedDevImport(specifier.text)) {
            usedDevImport = true;
          } else {
            violations.push({
              file: relPath,
              line: lineOf(text, specifier.getStart(source)),
              rule: 'dev-import',
              detail: `${forbidden}: "${specifier.text}"`,
            });
          }
        }
      }
    }
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const argument = node.arguments[0];
      if (argument && ts.isStringLiteral(argument)) {
        const forbidden = isForbiddenImportTarget(
          resolveImportTarget(srcRoot, absolutePath, argument.text),
        );
        if (forbidden) {
          if (isAllowedDevImport(argument.text)) {
            usedDevImport = true;
          } else {
            violations.push({
              file: relPath,
              line: lineOf(text, argument.getStart(source)),
              rule: 'dev-import',
              detail: `${forbidden}: "${argument.text}"`,
            });
          }
        }
        return;
      }
    }
    if (ts.isIdentifier(node)) {
      if (node.text.startsWith(DEV_TOKEN_PREFIX) || node.text.startsWith(PARITY_TOKEN_PREFIX)) {
        if (allowedTokens.has(node.text)) {
          usedTokens.add(node.text);
        } else if (!relPath.endsWith('.d.ts')) {
          violations.push({
            file: relPath,
            line: lineOf(text, node.getStart(source)),
            rule: 'dev-token',
            detail: `composition root token ${node.text} outside the registered exception in ${relPath}`,
          });
        }
      } else if (DEV_IDENTIFIER_NAMES.has(node.text)) {
        violations.push({
          file: relPath,
          line: lineOf(text, node.getStart(source)),
          rule: 'dev-marker',
          detail: `dev identifier "${node.text}"`,
        });
      }
    }
    if (
      ts.isPropertyAccessExpression(node)
      && FORBIDDEN_ENV_MEMBERS.has(node.name.text)
      && node.expression.getText(source) === 'import.meta.env'
    ) {
      violations.push({
        file: relPath,
        line: lineOf(text, node.getStart(source)),
        rule: 'env-branch',
        detail: `build-mode branch on ${node.expression.getText(source)}.${node.name.text}`,
      });
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      const isModuleSpecifier = ts.isImportDeclaration(node.parent)
        || ts.isExportDeclaration(node.parent)
        || (ts.isCallExpression(node.parent) && node.parent.expression.kind === ts.SyntaxKind.ImportKeyword);
      if (!isModuleSpecifier) {
        scanMarkupLiteral(text, node.text, node.getStart(source), violations, relPath);
      }
    }
    if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      scanMarkupLiteral(text, node.text, node.getStart(source), violations, relPath);
    }
    node.forEachChild(visit);
  };
  source.forEachChild(visit);

  if (exception) {
    const unusedTokens = (exception.allowTokens ?? []).filter(token => !usedTokens.has(token));
    if (unusedTokens.length > 0 || (exception.allowDevImports && !usedDevImport)) {
      violations.push({
        file: relPath,
        line: 1,
        rule: 'stale-exception',
        detail: `composition root exception for ${relPath} is no longer exercised`
          + (unusedTokens.length > 0 ? ` (unused tokens: ${unusedTokens.join(', ')})` : '')
          + (exception.allowDevImports && !usedDevImport ? ' (no dev import remains)' : ''),
      });
    } else {
      state.exceptionsUsed.push(relPath);
    }
  }
}

function scanCssFile(relPath, text, violations) {
  DEV_MARKER_PATTERN.lastIndex = 0;
  let match = DEV_MARKER_PATTERN.exec(text);
  while (match !== null) {
    if (!isLegacyOrgDiagnostic(relPath, match[0])) {
      violations.push({
        file: relPath,
        line: lineOf(text, match.index),
        rule: 'dev-marker',
        detail: `dev marker "${match[0]}" in css`,
      });
    }
    match = DEV_MARKER_PATTERN.exec(text);
  }
}

export async function analyzeTree(srcRoot, options = {}) {
  const exceptions = options.exceptions ?? COMPOSITION_ROOT_EXCEPTIONS;
  let sourceStat;
  try {
    sourceStat = statSync(srcRoot);
  } catch {
    throw new Error(`source root missing: ${srcRoot}`);
  }
  if (!sourceStat.isDirectory()) {
    throw new Error(`source root missing: ${srcRoot}`);
  }
  const rootDir = dirname(srcRoot);
  const prefix = toPosix(relative(rootDir, srcRoot));
  const absoluteFiles = await collectSourceFiles(srcRoot);
  const violations = [];
  const state = { exceptionsUsed: [] };
  const scanned = [];
  for (const absolutePath of absoluteFiles) {
    const relPath = toPosix(relative(rootDir, absolutePath));
    if (isDevOnlyModule(relPath) && exceptions[relPath]?.allowDevImports !== true) {
      continue;
    }
    scanned.push(relPath);
    const text = await readFile(absolutePath, 'utf8');
    if (relPath.endsWith('.css')) {
      scanCssFile(relPath, text, violations);
    } else {
      scanTsFile(srcRoot, absolutePath, relPath, text, violations, exceptions, state);
    }
  }
  if (!scanned.includes(`${prefix}/main.tsx`)) {
    violations.push({
      file: `${prefix}/main.tsx`,
      line: 1,
      rule: 'missing-entry',
      detail: 'composition root entry is missing from the scanned tree',
    });
  }
  if (scanned.length === 0) {
    throw new Error(`source tree is empty: ${srcRoot}`);
  }
  for (const [relPath] of Object.entries(exceptions)) {
    if (!scanned.includes(relPath)) {
      violations.push({
        file: relPath,
        line: 1,
        rule: 'stale-exception',
        detail: `registered composition root exception path does not exist: ${relPath}`,
      });
    }
  }
  violations.sort((left, right) => left.file.localeCompare(right.file) || left.line - right.line);
  return { files: scanned, violations, exceptionsUsed: state.exceptionsUsed };
}

export function formatViolations(violations) {
  return violations
    .map(({ file, line, rule, detail }) => `${file}:${line}: ${rule}: ${detail}`)
    .join('\n');
}

async function main() {
  const srcRoot = resolve(process.cwd(), 'src');
  const { files, violations, exceptionsUsed } = await analyzeTree(srcRoot);
  if (violations.length > 0) {
    process.stderr.write(`dev/product parity static gate FAILED: ${violations.length} violation(s)\n`);
    process.stderr.write(`${formatViolations(violations)}\n`);
    process.exitCode = 1;
    return;
  }
  process.stdout.write(
    `dev/product parity static gate passed: ${files.length} product sources,`
    + ` exceptions exercised: ${exceptionsUsed.length === 0 ? 'none' : exceptionsUsed.join(', ')}\n`,
  );
}

const isMain = process.argv[1]
  ? pathToFileURL(resolve(process.argv[1])).href === import.meta.url
  : false;
if (isMain) {
  await main();
}
