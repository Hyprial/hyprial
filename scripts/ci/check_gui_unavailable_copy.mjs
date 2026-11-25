#!/usr/bin/env node
/**
 * Fail closed when the GUI unavailable-copy ledger grows relative to the base branch.
 *
 * Decision record (PR 1f/F0, 2026-11-14):
 * - gui/tests/architecture/unavailable-copy.test.ts keeps the ledger exact
 *   against a scan of gui/src; it cannot see that an entry was added together
 *   with the banned string. This script is the shrink-only gate: it loads the
 *   ledger at HEAD and at --base-ref (git show) and compares them.
 * - The ledger stays a TypeScript module (one source of truth shared with
 *   vitest). It is read by transpiling the blob with the TypeScript compiler
 *   that gui/ already pins, the same dependency the request-architecture gate
 *   uses, and importing the result as a data: module.
 * - Copy maps are compared by (path, text) pairs. A pending bucket may not
 *   gain a pair or a duplicate. A permanent bucket (STATE, NOTIFICATION,
 *   ELSEWHERE, LOGIC_NOT_COPY, taken together) may not gain a pair unless the
 *   same diff removes that exact pair from the pending buckets taken together
 *   (re-bucketing existing copy). Moving a pair between buckets of one group
 *   is free. Count maps (PENDING_DISABLED, PENDING_COMPONENTS) may not gain a
 *   key or raise a count.
 * - An export whose name is not a known map fails, so a new bucket cannot
 *   become a way around the rules.
 * - First seed: a base without the ledger passes with an explicit first-seed
 *   result when the base branch is dev or main, and fails otherwise. A base
 *   ref that was requested but cannot be resolved fails closed. Without
 *   --base-ref only the ledger shape is checked.
 * - A file moved to a new path counts as new copy.
 * - The scanner configuration is compared too. The vitest gate reads its
 *   vocabulary, exclusions, scan root and ledger import from
 *   gui/tests/architecture/unavailable-copy.scan.ts; this script parses that
 *   file (and the relative imports of unavailable-copy.test.ts) with the
 *   TypeScript compiler at HEAD and at --base-ref and fails when the scan was
 *   narrowed: a removed vocabulary term, any change of a boundary or the
 *   machine-token pattern, a new skipped file or directory, a removed scanned
 *   extension, a changed scan root, a retargeted ledger or scan import, a
 *   removed or unknown constant. Added terms, removed skips and added
 *   extensions pass. Anything else that needs a human decision is a failure
 *   cleared only by REVIEWED_SCANNER_SHA256: when HEAD's configuration digest
 *   (--print-scanner-digest) equals that pinned constant the scanner change
 *   passes, so changing the scanner always edits this reviewed script in the
 *   same PR. A base without the scanner configuration is a first seed.
 * - An empty --base-ref fails, and a ledger or scanner blob with TypeScript
 *   syntax diagnostics fails instead of being read as empty.
 */

import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ts = (await import('../../gui/node_modules/typescript/lib/typescript.js')).default;
const DEFAULT_ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)));
export const LEDGER_RELATIVE_PATH = 'gui/tests/architecture/unavailable-copy.allow.ts';
export const PERMANENT_MAPS = ['STATE', 'NOTIFICATION', 'ELSEWHERE', 'LOGIC_NOT_COPY'];
export const PENDING_COPY_MAPS = [
  'PENDING_SHELL',
  'PENDING_AGENTS',
  'PENDING_ORG',
  'PENDING_CHAT',
  'PENDING_CONNECTIONS',
  'PENDING_SETTINGS',
  'PENDING_REMAINDER',
];
export const COUNT_MAPS = ['PENDING_DISABLED', 'PENDING_COMPONENTS'];
export const SCAN_CONFIG_RELATIVE_PATH = 'gui/tests/architecture/unavailable-copy.scan.ts';
export const SCAN_TEST_RELATIVE_PATH = 'gui/tests/architecture/unavailable-copy.test.ts';
export const REVIEWED_SCANNER_SHA256 = null;
const GROWABLE_LISTS = new Set(['VOCABULARY_TERMS', 'ENGLISH_TERMS', 'SOURCE_EXTENSIONS']);
const SHRINKABLE_LISTS = new Set(['SKIPPED_DIRECTORIES', 'SKIPPED_FILES']);
const EXACT_CONSTANTS = new Set([
  'LEDGER', 'SCAN_ROOT', 'ENGLISH_BOUNDARY_BEFORE', 'ENGLISH_BOUNDARY_AFTER', 'MACHINE_TOKEN',
]);
const KNOWN_CONSTANTS = new Set([...GROWABLE_LISTS, ...SHRINKABLE_LISTS, ...EXACT_CONSTANTS]);
const FIRST_SEED_BASES = new Set(['dev', 'main']);
const KNOWN = new Set([...PERMANENT_MAPS, ...PENDING_COPY_MAPS, ...COUNT_MAPS]);

function git(root, ...args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

function refExists(root, ref) {
  try {
    git(root, 'rev-parse', '--verify', `${ref}^{commit}`);
    return true;
  } catch {
    return false;
  }
}

function blobAt(root, ref, path) {
  try {
    git(root, 'cat-file', '-e', `${ref}:${path}`);
  } catch {
    return null;
  }
  return git(root, 'show', `${ref}:${path}`);
}

function transpile(source, label) {
  const result = ts.transpileModule(source, {
    reportDiagnostics: true,
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  if (result.diagnostics?.length) {
    const detail = result.diagnostics
      .map(item => ts.flattenDiagnosticMessageText(item.messageText, ' '))
      .slice(0, 3)
      .join('; ');
    throw new Error(`${label} has TypeScript syntax errors: ${detail}`);
  }
  return result.outputText;
}

export async function parseLedger(source) {
  const output = transpile(source, 'ledger');
  const module = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`);
  const unknown = Object.keys(module).filter(name => !KNOWN.has(name));
  if (unknown.length) throw new Error(`unknown ledger export: ${unknown.sort().join(', ')}`);
  const ledger = { copy: new Map(), counts: new Map() };
  for (const name of [...PERMANENT_MAPS, ...PENDING_COPY_MAPS]) {
    const map = module[name] ?? {};
    for (const [path, texts] of Object.entries(map)) {
      if (!Array.isArray(texts) || texts.some(text => typeof text !== 'string')) {
        throw new Error(`${name}[${path}] must be a list of strings`);
      }
      for (const text of texts) {
        const key = `${path}\0${text}`;
        const group = PERMANENT_MAPS.includes(name) ? 'permanent' : 'pending';
        const row = ledger.copy.get(key) ?? { permanent: 0, pending: 0 };
        row[group] += 1;
        ledger.copy.set(key, row);
      }
    }
  }
  for (const name of COUNT_MAPS) {
    const map = module[name] ?? {};
    const counts = new Map();
    for (const [path, count] of Object.entries(map)) {
      if (!Number.isInteger(count) || count <= 0) throw new Error(`${name}[${path}] must be a positive integer`);
      counts.set(path, count);
    }
    ledger.counts.set(name, counts);
  }
  return ledger;
}

function literalValue(node) {
  let current = node;
  while (ts.isAsExpression(current) || ts.isParenthesizedExpression(current) || ts.isSatisfiesExpression(current)) {
    current = current.expression;
  }
  if (ts.isStringLiteral(current) || ts.isNoSubstitutionTemplateLiteral(current)) return { kind: 'string', value: current.text };
  if (ts.isArrayLiteralExpression(current)) {
    const items = [];
    for (const element of current.elements) {
      const item = literalValue(element);
      if (item.kind !== 'string') return { kind: 'expression', value: node.getText() };
      items.push(item.value);
    }
    return { kind: 'list', value: items };
  }
  return { kind: 'expression', value: node.getText() };
}

function relativeImports(sourceFile) {
  const specifiers = [];
  for (const statement of sourceFile.statements) {
    if ((ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement))
      && statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
      && statement.moduleSpecifier.text.startsWith('.')) {
      specifiers.push(statement.moduleSpecifier.text);
    }
  }
  return specifiers.sort();
}

export function extractScannerConfig(scanSource, testSource) {
  transpile(scanSource, 'scanner configuration');
  transpile(testSource, 'scanner test');
  const scan = ts.createSourceFile('scan.ts', scanSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const test = ts.createSourceFile('test.ts', testSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const constants = {};
  for (const statement of scan.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    if (!statement.modifiers?.some(modifier => modifier.kind === ts.SyntaxKind.ExportKeyword)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name) && declaration.initializer) {
        constants[declaration.name.text] = literalValue(declaration.initializer);
      }
    }
  }
  return { constants, scanImports: relativeImports(scan), testImports: relativeImports(test) };
}

export function scannerDigest(config) {
  const sortedConstants = Object.fromEntries(Object.entries(config.constants).sort(([a], [b]) => a.localeCompare(b)));
  const canonical = JSON.stringify({ ...config, constants: sortedConstants });
  return createHash('sha256').update(canonical).digest('hex');
}

export function compareScannerConfigs(base, head) {
  const violations = [];
  const fail = (kind, detail) => violations.push({ kind, ...detail });
  for (const [name, before] of Object.entries(base.constants)) {
    const after = head.constants[name];
    if (after === undefined) {
      fail('scanner-constant-removed', { name });
    } else if (after.kind !== before.kind) {
      fail('scanner-constant-changed', { name });
    } else if (before.kind === 'list' && GROWABLE_LISTS.has(name)) {
      const missing = before.value.filter(item => !after.value.includes(item));
      if (missing.length) fail('scanner-narrowed', { name, removed: missing });
    } else if (before.kind === 'list' && SHRINKABLE_LISTS.has(name)) {
      const added = after.value.filter(item => !before.value.includes(item));
      if (added.length) fail('scanner-exclusion-added', { name, added });
    } else if (JSON.stringify(before.value) !== JSON.stringify(after.value)) {
      fail('scanner-constant-changed', { name, base: before.value, head: after.value });
    }
  }
  for (const [name, after] of Object.entries(head.constants)) {
    if (base.constants[name] === undefined && !KNOWN_CONSTANTS.has(name)) fail('unknown-scanner-constant', { name });
    if (base.constants[name] === undefined && KNOWN_CONSTANTS.has(name) && SHRINKABLE_LISTS.has(name) && after.value.length) {
      fail('scanner-exclusion-added', { name, added: after.value });
    }
  }
  for (const [name, before, after] of [
    ['scan', base.scanImports, head.scanImports],
    ['test', base.testImports, head.testImports],
  ]) {
    if (JSON.stringify(before) !== JSON.stringify(after)) {
      fail('scanner-import-changed', { file: name, base: before, head: after });
    }
  }
  return violations;
}

function describe(key) {
  const [path, text] = key.split('\0');
  return { path, text };
}

export function compareLedgers(base, head) {
  const violations = [];
  const keys = new Set([...base.copy.keys(), ...head.copy.keys()]);
  for (const key of [...keys].sort()) {
    const before = base.copy.get(key) ?? { permanent: 0, pending: 0 };
    const after = head.copy.get(key) ?? { permanent: 0, pending: 0 };
    if (after.pending > before.pending) {
      violations.push({ kind: 'pending-copy-grew', ...describe(key), base: before.pending, head: after.pending });
    }
    if (after.permanent > before.permanent) {
      const rebucketed = before.pending > after.pending && after.permanent <= before.permanent + (before.pending - after.pending);
      if (!rebucketed) {
        violations.push({ kind: 'permanent-copy-grew', ...describe(key), base: before.permanent, head: after.permanent });
      }
    }
  }
  for (const name of COUNT_MAPS) {
    const before = base.counts.get(name) ?? new Map();
    const after = head.counts.get(name) ?? new Map();
    for (const [path, count] of [...after].sort(([left], [right]) => left.localeCompare(right))) {
      if (count > (before.get(path) ?? 0)) {
        violations.push({ kind: 'count-grew', map: name, path, base: before.get(path) ?? 0, head: count });
      }
    }
  }
  return violations;
}

export async function check(root, options = {}) {
  const resolved = resolve(root);
  const violations = [];
  let head;
  try {
    head = await parseLedger(readFileSync(join(resolved, LEDGER_RELATIVE_PATH), 'utf8'));
  } catch (error) {
    return { status: 'FAIL', baseRef: options.baseRef ?? null, firstSeed: false, violations: [{ kind: 'invalid-ledger', detail: error.message }] };
  }
  let firstSeed = false;
  let scannerOverride = false;
  let headConfig = null;
  try {
    headConfig = extractScannerConfig(
      readFileSync(join(resolved, SCAN_CONFIG_RELATIVE_PATH), 'utf8'),
      readFileSync(join(resolved, SCAN_TEST_RELATIVE_PATH), 'utf8'),
    );
  } catch (error) {
    return { status: 'FAIL', baseRef: options.baseRef ?? null, firstSeed: false, violations: [{ kind: 'invalid-scanner-config', detail: error.message }] };
  }
  if (options.baseRef === '') {
    violations.push({ kind: 'invalid-base-ref', detail: 'base ref is empty' });
  } else if (options.baseRef !== undefined && options.baseRef !== null) {
    try {
      if (!refExists(resolved, options.baseRef)) throw new Error(`base ref does not exist: ${options.baseRef}`);
      const text = blobAt(resolved, options.baseRef, LEDGER_RELATIVE_PATH);
      if (text === null) {
        if (FIRST_SEED_BASES.has(options.baseBranch ?? '')) firstSeed = true;
        else violations.push({ kind: 'missing-base-ledger', detail: `${LEDGER_RELATIVE_PATH} is absent at ${options.baseRef} and first seed is limited to dev/main` });
      } else {
        violations.push(...compareLedgers(await parseLedger(text), head));
        const baseScan = blobAt(resolved, options.baseRef, SCAN_CONFIG_RELATIVE_PATH);
        const baseTest = blobAt(resolved, options.baseRef, SCAN_TEST_RELATIVE_PATH);
        if (baseScan !== null && baseTest !== null) {
          const scannerViolations = compareScannerConfigs(extractScannerConfig(baseScan, baseTest), headConfig);
          const pinned = options.reviewedScannerSha256 === undefined ? REVIEWED_SCANNER_SHA256 : options.reviewedScannerSha256;
          if (scannerViolations.length && pinned !== null && pinned === scannerDigest(headConfig)) scannerOverride = true;
          else violations.push(...scannerViolations);
        }
      }
    } catch (error) {
      violations.push({ kind: 'invalid-base-ref', detail: error.message });
    }
  }
  return { status: violations.length ? 'FAIL' : 'PASS', baseRef: options.baseRef ?? null, firstSeed, scannerOverride, violations };
}

async function main(argv) {
  const options = { root: DEFAULT_ROOT, baseRef: null, baseBranch: null, json: false, printDigest: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--root') options.root = resolve(argv[++index]);
    else if (argument === '--base-ref') {
      options.baseRef = argv[++index];
      if (options.baseRef === undefined || options.baseRef === '') throw new Error('--base-ref requires a non-empty value');
    }
    else if (argument === '--print-scanner-digest') options.printDigest = true;
    else if (argument === '--base-branch') options.baseBranch = argv[++index];
    else if (argument === '--json') options.json = true;
    else throw new Error(`unknown argument: ${argument}`);
  }
  if (options.printDigest) {
    console.log(scannerDigest(extractScannerConfig(
      readFileSync(join(options.root, SCAN_CONFIG_RELATIVE_PATH), 'utf8'),
      readFileSync(join(options.root, SCAN_TEST_RELATIVE_PATH), 'utf8'),
    )));
    return 0;
  }
  const report = await check(options.root, options);
  if (options.json) console.log(JSON.stringify(report, null, 2));
  else {
    const note = (report.firstSeed ? ' (first seed: the base has no ledger)' : '')
      + (report.scannerOverride ? ' (scanner change cleared by REVIEWED_SCANNER_SHA256)' : '');
    console.log(`${report.status} GUI unavailable-copy ledger${note}`);
    for (const row of report.violations) console.log(JSON.stringify(row));
  }
  return report.status === 'PASS' ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exitCode = await main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
