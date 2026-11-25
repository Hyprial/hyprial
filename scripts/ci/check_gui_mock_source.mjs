#!/usr/bin/env node
/**
 * Fail closed when the GUI grows a second mock source for daemon request data.
 *
 * Decision record (2026-11-15, one JavaScript Contract Mock, PR 1e-2):
 * - Owner rule: mock data has one source. The contract
 *   (contract/daemon-request/openapi.json) is the only place a method's
 *   examples live; gui/contract-mock is the only engine that serves them, in
 *   two forms (Node process, in-browser function). Everything else under gui/
 *   either asks that module or is a test double the tests own.
 * - Rules, each a finding kind: data files under gui/src that are not the
 *   generated table; code outside the generated client that keys, switches or
 *   compares on a daemon method name (a hand-written responder per method);
 *   an import or path literal of the generated table outside its two sanctioned
 *   readers; an import of gui/contract-mock from outside its sanctioned roots
 *   (product code cannot reach the engine); a method-name literal inside the
 *   engine; a preset document that carries a payload key or a reference that
 *   does not resolve (policy is data and holds references only); a Python file
 *   under contract/daemon-request that is not one of the validation files, a
 *   Python HTTP server pattern, a launch of the Python mock anywhere; and the
 *   hand-written fixtures under gui/src/dev, which may not appear, grow or
 *   outlive their file without a ledger row.
 * - Preset findings are never accepted by the ledger: they fail directly. Every
 *   other finding is keyed by kind, path and subject (an AST form such as
 *   `case-label`, a pattern, a literal) and counted; it must match an
 *   `allowlist` row (an exception with its own reason) or a `pending` row (work
 *   a later stage removes) exactly, so neither list rots.
 * - The ledger can only shrink. With --base-ref the ledger at the base commit is
 *   the reference: a new or larger allowlist/pending row, a new fixtures row or
 *   a raised maxLines fails. The base ref is mandatory when given: an empty or
 *   unresolvable ref, or an unreadable base ledger, fails closed. The first seed
 *   is accepted only when the base has no ledger at all, only with
 *   --allow-first-seed on a dev/main base and only if the ledger hashes to
 *   FIRST_SEED_LEDGER_SHA256 and the scanner configuration to
 *   FIRST_SEED_SCANNER_SHA256 (the first seed has no base scanner to compare
 *   with, so without the second pin the configuration could be loosened
 *   unseen); once the base carries a ledger neither digest is consulted.
 * - `mock-module-import` resolves every import-like form, not only relative
 *   specifiers: static import and re-export, `import()`, `require()`,
 *   `new URL(x, import.meta.url)` and `import.meta.glob`. A specifier that
 *   starts with an alias of gui/tsconfig.json `compilerOptions.paths` (the
 *   `@/*` pair, read from the file, not hard-coded; the default pair is used
 *   only when the file is absent) is mapped to its target, so
 *   `@/../contract-mock/index.ts` is caught like `../../../contract-mock`.
 * - The scanner configuration (gui-mock-source-scanner.json) is data, read from
 *   HEAD and from the --base-ref blob. Growable keys may gain entries; exemption
 *   keys (sanctioned readers and roots, validation files, skipped directories)
 *   may not; constant keys may not change. Any other change is cleared only by
 *   REVIEWED_SCANNER_SHA256, the digest of HEAD's configuration
 *   (--print-scanner-digest), so changing the scanner edits this reviewed script
 *   in the same PR.
 * - Fixture ledger unit: a per-file line ceiling (maxLines), so editing a
 *   fixture without growing it needs no ledger change and a new fixture needs a
 *   visible row. gui/src/dev/tools and gui/src/dev/contract-mock are exempt
 *   roots: the first holds development tooling, the second is the dev adapter
 *   of the engine and its generated table.
 * - Out of reach, by design: scanning logic is not pinned (a change to it is
 *   code review); a method name assembled by string construction is not seen;
 *   hand-written doubles inside gui/tests unit tests stay legal (D11).
 * - Code that names the launch patterns lives in the configuration, not here,
 *   and the self-test builds them by concatenation, so this gate does not
 *   trigger itself.
 */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, normalize, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ts = await import('../../gui/node_modules/typescript/lib/typescript.js');
const DEFAULT_ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)));
export const CONFIG_RELATIVE_PATH = 'scripts/ci/gui-mock-source-scanner.json';
export const FIRST_SEED_LEDGER_SHA256 = 'eb1a77b619f8e26cdbf3c56c5a1b1062b9e4b0989c35692ace2059ada109f02c';
export const FIRST_SEED_SCANNER_SHA256 = '447e6db211e96ef67f3145d940c70cfd1b99b8c65945fd791bfeb267d243e644';
export const REVIEWED_SCANNER_SHA256 = null;
const DEFAULT_CONFIG = JSON.parse(readFileSync(new URL('./gui-mock-source-scanner.json', import.meta.url), 'utf8'));
export const LEDGER_RELATIVE_PATH = DEFAULT_CONFIG.ledgerPath;
const FIRST_SEED_BASES = new Set(['dev', 'main']);
export const FINDING_KINDS = new Set([
  'data-file', 'method-keyed-responder', 'mock-table-import', 'mock-module-import', 'mock-engine-method-literal',
  'python-mock-file', 'python-mock-server', 'python-mock-launch',
]);
const DIRECT_KINDS = new Set(['preset-payload', 'preset-unresolved']);
const GROWABLE_KEYS = new Set([
  'sourceRoots', 'dataExtensions', 'fixtureRoots', 'importScanRoots', 'launchScanRoots', 'launchPatterns',
  'pythonServerPatterns', 'extensions', 'launchExtensions', 'launchFiles',
]);
const EXEMPTION_KEYS = new Set([
  'generatedTables', 'tableImporters', 'mockModuleRoots', 'mockModuleImporterRoots', 'generatedClientRoots',
  'fixtureExemptRoots', 'pythonValidationFiles', 'skippedDirectories',
]);
const EXACT_KEYS = new Set(['version', 'ledgerPath', 'contractPath', 'presetsPath', 'pythonMockRoot']);
const PATTERN_KEYS = ['launchPatterns', 'pythonServerPatterns'];
const SCENARIOS = new Set(['success', 'empty', 'unconfigured', 'error']);

function git(root, ...args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

function blobAt(root, ref, path) {
  try {
    git(root, 'cat-file', '-e', `${ref}:${path}`);
  } catch {
    return null;
  }
  return git(root, 'show', `${ref}:${path}`);
}

export function parseScannerConfig(value, label = 'scanner configuration') {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} is not an object`);
  const known = new Set([...GROWABLE_KEYS, ...EXEMPTION_KEYS, ...EXACT_KEYS]);
  const unknown = Object.keys(value).filter(name => !known.has(name));
  if (unknown.length) throw new Error(`${label} has unknown keys: ${unknown.sort().join(',')}`);
  for (const name of known) {
    if (!(name in value)) throw new Error(`${label} is missing key ${name}`);
    const entry = value[name];
    if (name === 'version') {
      if (entry !== 1) throw new Error(`${label} version must be 1`);
    } else if (EXACT_KEYS.has(name)) {
      if (typeof entry !== 'string' || !entry) throw new Error(`${label} key ${name} must be a non-empty string`);
    } else if (!Array.isArray(entry) || entry.some(item => typeof item !== 'string' || !item)) {
      throw new Error(`${label} key ${name} must be a list of non-empty strings`);
    }
  }
  for (const name of PATTERN_KEYS) for (const source of value[name]) new RegExp(source);
  return value;
}

export function scannerDigest(config) {
  const canonical = JSON.stringify(Object.fromEntries(Object.entries(config).sort(([left], [right]) => (left < right ? -1 : 1))));
  return createHash('sha256').update(canonical).digest('hex');
}

export function compareScannerConfigs(base, head) {
  const violations = [];
  const fail = (kind, detail) => violations.push({ kind, ...detail });
  for (const name of Object.keys(base)) {
    const before = base[name];
    const after = head[name];
    if (after === undefined) fail('scanner-key-removed', { name });
    else if (GROWABLE_KEYS.has(name)) {
      const removed = before.filter(item => !after.includes(item));
      if (removed.length) fail('scanner-narrowed', { name, removed });
    } else if (EXEMPTION_KEYS.has(name)) {
      const added = after.filter(item => !before.includes(item));
      if (added.length) fail('scanner-exclusion-added', { name, added });
    } else if (JSON.stringify(before) !== JSON.stringify(after)) {
      fail('scanner-constant-changed', { name, base: before, head: after });
    }
  }
  for (const name of Object.keys(head)) if (base[name] === undefined) fail('scanner-key-unknown', { name });
  return violations;
}

function key(row) {
  return `${row.kind}\0${row.path}\0${row.subject}`;
}

function rows(findings) {
  const counts = new Map();
  for (const finding of findings) counts.set(key(finding), (counts.get(key(finding)) ?? 0) + 1);
  return [...counts.entries()].sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0)).map(([entry, count]) => {
    const [kind, path, subject] = entry.split('\0');
    return { kind, path, subject, count };
  });
}

function relativeName(root, path) {
  return relative(root, path).replaceAll('\\', '/');
}

function walk(root, prefix, accept, skipped) {
  const result = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((left, right) => (left.name < right.name ? -1 : 1))) {
      if (entry.isDirectory()) {
        if (!skipped.has(entry.name)) visit(join(directory, entry.name));
      } else if (accept(join(directory, entry.name))) {
        result.push(join(directory, entry.name));
      }
    }
  };
  const directory = join(root, prefix);
  if (existsSync(directory)) visit(directory);
  return result;
}

function extensionOf(path) {
  const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  const dot = path.lastIndexOf('.');
  return dot > slash ? path.slice(dot) : '';
}

function under(name, prefixes) {
  return prefixes.some(prefix => name === prefix.replace(/\/$/, '') || name.startsWith(prefix.endsWith('/') ? prefix : `${prefix}/`));
}

function parseText(name, text) {
  const kind = /x$/.test(name) ? ts.ScriptKind.TSX : /\.[cm]?js$/.test(name) ? ts.ScriptKind.JS : ts.ScriptKind.TS;
  return ts.createSourceFile(name, text, ts.ScriptTarget.ESNext, true, kind);
}

function stringLike(node) {
  return node !== undefined && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node));
}

function isImportMeta(node) {
  return ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword;
}

function specifiersOf(node) {
  if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
    return [node.moduleSpecifier.text];
  }
  if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && stringLike(node.moduleReference.expression)) {
    return [node.moduleReference.expression.text];
  }
  if (ts.isCallExpression(node)) {
    if (stringLike(node.arguments[0])
        && (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === 'require'))) {
      return [node.arguments[0].text];
    }
    if (ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'glob' && isImportMeta(node.expression.expression)) {
      const first = node.arguments[0];
      if (stringLike(first)) return [first.text];
      if (first && ts.isArrayLiteralExpression(first)) return first.elements.filter(stringLike).map(element => element.text);
    }
  }
  if (ts.isNewExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'URL'
      && stringLike(node.arguments?.[0]) && node.arguments[1] && ts.isPropertyAccessExpression(node.arguments[1])
      && node.arguments[1].name.text === 'url' && isImportMeta(node.arguments[1].expression)) {
    return [node.arguments[0].text];
  }
  return [];
}

export function resolveSpecifier(name, specifier, aliases) {
  if (specifier.startsWith('.')) return normalize(join(dirname(name), specifier)).replaceAll('\\', '/');
  for (const [prefix, target] of aliases) {
    if (specifier.startsWith(prefix)) return normalize(join(target, specifier.slice(prefix.length))).replaceAll('\\', '/');
  }
  return null;
}

const ALIAS_CONFIG_PATH = 'gui/tsconfig.json';

function readAliases(root) {
  const path = join(root, ALIAS_CONFIG_PATH);
  const found = [];
  if (existsSync(path)) {
    const parsed = ts.parseConfigFileTextToJson(path, readFileSync(path, 'utf8'));
    const paths = parsed.config?.compilerOptions?.paths ?? {};
    const base = dirname(ALIAS_CONFIG_PATH);
    for (const [pattern, targets] of Object.entries(paths)) {
      if (!pattern.endsWith('*') || !Array.isArray(targets)) continue;
      for (const target of targets) {
        if (typeof target === 'string' && target.endsWith('*')) found.push([pattern.slice(0, -1), normalize(join(base, target.slice(0, -1))).replaceAll('\\', '/')]);
      }
    }
  }
  return found.length ? found : [['@/', 'gui/src/']];
}

const COMPARISONS = new Set([
  ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
]);

function readJson(path, label) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    throw new Error(`cannot read ${label} ${path}: ${error.message}`);
  }
}

function contractFacts(root, config) {
  const document = readJson(join(root, config.contractPath), 'contract');
  const methods = new Map();
  for (const entry of Array.isArray(document['x-hyprial-methods']) ? document['x-hyprial-methods'] : []) {
    if (!entry || typeof entry.name !== 'string') continue;
    const names = new Set((Array.isArray(entry['x-hyprial-examples']) ? entry['x-hyprial-examples'] : []).map(example => example?.name));
    const operation = document.paths?.[`/daemon/request/${entry.name}`]?.post;
    for (const example of Array.isArray(operation?.['x-hyprial-examples']) ? operation['x-hyprial-examples'] : []) names.add(example?.name);
    methods.set(entry.name, names);
  }
  return { methods, dotted: new Set([...methods.keys()].filter(name => name.includes('.'))) };
}

function scanCode(name, source, ctx, findings) {
  const tableBasenames = ctx.config.generatedTables.map(path => path.slice(path.lastIndexOf('/') + 1));
  const isEngineRoot = under(name, ctx.config.mockModuleRoots);
  const generated = under(name, ctx.config.generatedClientRoots);
  const importer = under(name, ctx.config.mockModuleImporterRoots);
  const tableImporter = ctx.config.tableImporters.includes(name);
  const checkResponders = under(name, ctx.config.sourceRoots) && !generated;
  const visit = (node) => {
    if (checkResponders) {
      if ((ts.isPropertyAssignment(node) || ts.isMethodDeclaration(node) || ts.isShorthandPropertyAssignment(node)) && node.name) {
        const label = ts.isComputedPropertyName(node.name) ? node.name.expression : node.name;
        if ((stringLike(label)) && ctx.dotted.has(label.text)) findings.push({ kind: 'method-keyed-responder', path: name, subject: 'object-key' });
      }
      if (ts.isCaseClause(node) && stringLike(node.expression) && ctx.dotted.has(node.expression.text)) {
        findings.push({ kind: 'method-keyed-responder', path: name, subject: 'case-label' });
      }
      if (ts.isBinaryExpression(node) && COMPARISONS.has(node.operatorToken.kind)
          && [node.left, node.right].some(side => stringLike(side) && ctx.dotted.has(side.text))) {
        findings.push({ kind: 'method-keyed-responder', path: name, subject: 'comparison' });
      }
    }
    if (stringLike(node)) {
      if (isEngineRoot && ctx.dotted.has(node.text)) findings.push({ kind: 'mock-engine-method-literal', path: name, subject: node.text });
      if (!tableImporter && tableBasenames.some(base => node.text === base || node.text.endsWith(`/${base}`))) {
        findings.push({ kind: 'mock-table-import', path: name, subject: node.text.slice(node.text.lastIndexOf('/') + 1) });
      }
    }
    if (!importer) {
      for (const specifier of specifiersOf(node)) {
        const target = resolveSpecifier(name, specifier, ctx.aliases);
        if (target !== null && under(target, ctx.config.mockModuleRoots)) findings.push({ kind: 'mock-module-import', path: name, subject: target });
      }
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(source, visit);
}

function lineCount(path) {
  const bytes = readFileSync(path);
  let lines = 0;
  for (const byte of bytes) if (byte === 0x0a) lines += 1;
  return bytes.length > 0 && bytes[bytes.length - 1] !== 0x0a ? lines + 1 : lines;
}

function checkPresets(root, config, facts) {
  const problems = [];
  const path = join(root, config.presetsPath);
  if (!existsSync(path)) return problems;
  let document;
  try {
    document = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    return [{ kind: 'preset-payload', detail: `${config.presetsPath} is not JSON: ${error.message}` }];
  }
  const payload = (where, value, allowed) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      problems.push({ kind: 'preset-payload', detail: `${where} is not an object` });
      return false;
    }
    for (const name of Object.keys(value)) {
      if (!allowed.includes(name)) problems.push({ kind: 'preset-payload', detail: `${where} carries the key "${name}"` });
    }
    return true;
  };
  const unresolved = detail => problems.push({ kind: 'preset-unresolved', detail });
  const method = (where, name) => {
    if (!facts.methods.has(name)) unresolved(`${where} names the unknown method "${name}"`);
    return facts.methods.get(name);
  };
  if (!payload('presets', document, ['version', 'presets', 'transitions', 'inputGuards'])) return problems;
  const presets = document.presets && typeof document.presets === 'object' ? document.presets : {};
  for (const [id, preset] of Object.entries(presets)) {
    const where = `preset ${id}`;
    if (!payload(where, preset, ['scenario', 'methodScenarios', 'cases'])) continue;
    if (!SCENARIOS.has(preset.scenario)) unresolved(`${where} has the unknown scenario "${preset.scenario}"`);
    for (const [name, scenario] of Object.entries(preset.methodScenarios ?? {})) {
      method(where, name);
      if (!SCENARIOS.has(scenario)) unresolved(`${where} gives ${name} the unknown scenario "${scenario}"`);
    }
    for (const [name, caseName] of Object.entries(preset.cases ?? {})) {
      const names = method(where, name);
      if (names !== undefined && !names.has(caseName)) unresolved(`${where} names the unknown case "${caseName}" of ${name}`);
    }
  }
  for (const row of Array.isArray(document.transitions) ? document.transitions : []) {
    if (!payload('transition', row, ['preset', 'method', 'next'])) continue;
    for (const id of [row.preset, row.next]) if (!(id in presets)) unresolved(`transition names the unknown preset "${id}"`);
    method('transition', row.method);
  }
  for (const guard of Array.isArray(document.inputGuards) ? document.inputGuards : []) {
    if (!payload('input guard', guard, ['method', 'requireOneOf', 'refusal'])) continue;
    method('input guard', guard.method);
    payload('input guard refusal', guard.refusal, ['code', 'message']);
    for (const requirement of Array.isArray(guard.requireOneOf) ? guard.requireOneOf : []) {
      payload('input guard requirement', requirement, ['path', 'equals']);
    }
  }
  return problems;
}

export function scan(root, config = DEFAULT_CONFIG) {
  const resolved = resolve(root);
  const skipped = new Set(config.skippedDirectories);
  const facts = contractFacts(resolved, config);
  const ctx = { config, dotted: facts.dotted, aliases: readAliases(resolved) };
  const findings = [];
  const code = path => config.extensions.includes(extensionOf(path));
  for (const prefix of config.sourceRoots) {
    for (const path of walk(resolved, prefix, () => true, skipped)) {
      const name = relativeName(resolved, path);
      if (config.dataExtensions.includes(extensionOf(path)) && !config.generatedTables.includes(name)) {
        findings.push({ kind: 'data-file', path: name, subject: extensionOf(path) });
      }
    }
  }
  const codeFiles = new Set();
  for (const prefix of new Set([...config.sourceRoots, ...config.importScanRoots, ...config.mockModuleRoots.map(root => root.replace(/\/$/, ''))])) {
    for (const path of walk(resolved, prefix, code, skipped)) codeFiles.add(path);
  }
  for (const path of [...codeFiles].sort()) {
    const name = relativeName(resolved, path);
    scanCode(name, parseText(name, readFileSync(path, 'utf8')), ctx, findings);
  }
  const serverPatterns = config.pythonServerPatterns.map(source => new RegExp(source));
  for (const path of walk(resolved, config.pythonMockRoot, file => extensionOf(file) === '.py', skipped)) {
    const name = relativeName(resolved, path);
    if (!config.pythonValidationFiles.includes(name)) findings.push({ kind: 'python-mock-file', path: name, subject: 'py' });
    const text = readFileSync(path, 'utf8');
    for (const pattern of serverPatterns) {
      const hits = text.match(new RegExp(pattern.source, 'g'));
      for (let count = 0; count < (hits?.length ?? 0); count += 1) findings.push({ kind: 'python-mock-server', path: name, subject: pattern.source });
    }
  }
  const launchPatterns = config.launchPatterns.map(source => new RegExp(source, 'g'));
  const launchPaths = new Set(config.launchFiles.map(file => join(resolved, file)).filter(existsSync));
  for (const prefix of config.launchScanRoots) {
    for (const path of walk(resolved, prefix, file => config.launchExtensions.includes(extensionOf(file)), skipped)) launchPaths.add(path);
  }
  for (const path of [...launchPaths].sort()) {
    const text = readFileSync(path, 'utf8');
    const name = relativeName(resolved, path);
    for (const pattern of launchPatterns) {
      for (const _hit of text.matchAll(pattern)) findings.push({ kind: 'python-mock-launch', path: name, subject: pattern.source });
    }
  }
  const fixtures = [];
  for (const prefix of config.fixtureRoots) {
    for (const path of walk(resolved, prefix, () => true, skipped)) {
      const name = relativeName(resolved, path);
      if (!under(name, config.fixtureExemptRoots)) fixtures.push({ path: name, lines: lineCount(path) });
    }
  }
  fixtures.sort((left, right) => (left.path < right.path ? -1 : 1));
  return { rows: rows(findings), fixtures, problems: checkPresets(resolved, config, facts) };
}

const ROW_KEYS = {
  allowlist: ['count', 'kind', 'path', 'reason', 'subject'],
  pending: ['count', 'kind', 'path', 'subject'],
};

function parseRows(rowsValue, list, label) {
  if (!Array.isArray(rowsValue)) throw new Error(`${label}: ${list} is not an array`);
  const result = new Map();
  for (const [index, row] of rowsValue.entries()) {
    const where = `${label}: ${list} row #${index}`;
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error(`${where} is not an object`);
    const keys = Object.keys(row).sort();
    if (keys.join() !== ROW_KEYS[list].join()) throw new Error(`${where} has unknown or missing keys: ${keys.join(',')}`);
    if (!FINDING_KINDS.has(row.kind)) throw new Error(`${where} has unknown finding kind: ${row.kind}`);
    if (![row.path, row.subject].every(value => typeof value === 'string' && value.length > 0)) throw new Error(`${where} has an invalid path or subject`);
    if (!Number.isInteger(row.count) || row.count <= 0) throw new Error(`${where} has an invalid count`);
    if (list === 'allowlist' && (typeof row.reason !== 'string' || row.reason.trim().length < 10)) throw new Error(`${where} has no reason`);
    if (result.has(key(row))) throw new Error(`${where} is a duplicate`);
    result.set(key(row), row.count);
  }
  return result;
}

function parseFixtures(value, label) {
  if (!Array.isArray(value)) throw new Error(`${label}: fixtures is not an array`);
  const result = new Map();
  for (const [index, row] of value.entries()) {
    const where = `${label}: fixtures row #${index}`;
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error(`${where} is not an object`);
    const keys = Object.keys(row).sort();
    if (keys.join() !== 'maxLines,path') throw new Error(`${where} has unknown or missing keys: ${keys.join(',')}`);
    if (typeof row.path !== 'string' || !row.path) throw new Error(`${where} has an invalid path`);
    if (!Number.isInteger(row.maxLines) || row.maxLines < 0) throw new Error(`${where} has an invalid maxLines`);
    if (result.has(row.path)) throw new Error(`${where} is a duplicate`);
    result.set(row.path, row.maxLines);
  }
  return result;
}

export function parseLedger(document, label) {
  if (!document || document.version !== 1 || typeof document.policy !== 'string') throw new Error(`${label}: version/policy invalid`);
  const keys = Object.keys(document).sort();
  if (keys.join() !== 'allowlist,fixtures,pending,policy,version') throw new Error(`${label}: unknown or missing top-level keys: ${keys.join(',')}`);
  return {
    allowlist: parseRows(document.allowlist, 'allowlist', label),
    pending: parseRows(document.pending, 'pending', label),
    fixtures: parseFixtures(document.fixtures, label),
  };
}

const POLICY = 'Mock-source exceptions of the GUI. allowlist rows are exceptions that stay for now and carry their own reason; pending rows are findings that later stages of PR 1e remove; fixtures rows cap the lines of each hand-written fixture file under gui/src/dev. No list may grow against the base branch: rows may only leave or shrink.';

export function _ledgerDocument(allowlist, pending, fixtures) {
  return { version: 1, policy: POLICY, allowlist, pending, fixtures };
}

function describe(entry, extra) {
  const [kind, path, subject] = entry.split('\0');
  return { finding: kind, path, subject, ...extra };
}

function baseLedger(root, ref, ledgerRelativePath) {
  const text = blobAt(root, ref, ledgerRelativePath);
  return text === null ? null : parseLedger(JSON.parse(text), 'base ledger');
}

function baseScanner(root, ref) {
  const text = blobAt(root, ref, CONFIG_RELATIVE_PATH);
  if (text === null) return { config: null, problem: null };
  try {
    return { config: parseScannerConfig(JSON.parse(text), 'base scanner configuration'), problem: null };
  } catch (error) {
    return { config: null, problem: { kind: 'invalid-base-scanner-config', detail: error.message } };
  }
}

function compareScannerToBase(ref, baseScan, config, options) {
  let violations;
  if (baseScan.problem !== null) violations = [baseScan.problem];
  else if (baseScan.config === null) {
    violations = [{ kind: 'missing-base-scanner-config', detail: `${CONFIG_RELATIVE_PATH} is absent at ${ref} while the base has a ledger` }];
  } else violations = compareScannerConfigs(baseScan.config, config);
  const pinned = options.reviewedScannerSha256 === undefined ? REVIEWED_SCANNER_SHA256 : options.reviewedScannerSha256;
  if (violations.length && pinned !== null && pinned === scannerDigest(config)) return { violations: [], scannerOverride: true };
  return { violations, scannerOverride: false };
}

function compareToBase(root, ledgerPath, current, config, options) {
  const violations = [];
  const ref = options.baseRef;
  if (typeof ref !== 'string' || ref.length === 0) return { violations: [{ kind: 'invalid-base-ref', detail: 'empty base ref' }] };
  try {
    git(root, 'rev-parse', '--verify', `${ref}^{commit}`);
  } catch {
    return { violations: [{ kind: 'invalid-base-ref', detail: ref }] };
  }
  const baseScan = baseScanner(root, ref);
  let base;
  try {
    base = baseLedger(root, ref, baseScan.config?.ledgerPath ?? config.ledgerPath);
  } catch (error) {
    return { violations: [{ kind: 'invalid-base-ledger', detail: error.message }] };
  }
  if (base === null) {
    if (!options.allowFirstSeed) return { violations: [{ kind: 'missing-base-ledger', detail: `${config.ledgerPath} is absent at ${ref}` }] };
    if (!FIRST_SEED_BASES.has(options.baseBranch ?? '')) return { violations: [{ kind: 'missing-base-ledger', detail: 'first seed is limited to dev/main' }] };
    const digest = createHash('sha256').update(readFileSync(ledgerPath)).digest('hex');
    const pinned = options.firstSeedDigest === undefined ? FIRST_SEED_LEDGER_SHA256 : options.firstSeedDigest;
    if (pinned === null || digest !== pinned) return { violations: [{ kind: 'seed-digest-mismatch', path: config.ledgerPath }] };
    const pinnedScanner = options.firstSeedScannerDigest === undefined ? FIRST_SEED_SCANNER_SHA256 : options.firstSeedScannerDigest;
    if (pinnedScanner === null || scannerDigest(config) !== pinnedScanner) {
      return { violations: [{ kind: 'seed-scanner-mismatch', path: CONFIG_RELATIVE_PATH }] };
    }
    return { violations: [], firstSeed: true };
  }
  for (const list of ['allowlist', 'pending']) {
    for (const [entry, count] of current[list]) {
      if (count > (base[list].get(entry) ?? 0)) {
        violations.push({ kind: `${list}-ledger-growth`, ...describe(entry, { count, base: base[list].get(entry) ?? 0 }) });
      }
    }
  }
  for (const [path, maxLines] of current.fixtures) {
    const before = base.fixtures.get(path);
    if (before === undefined) violations.push({ kind: 'fixtures-ledger-growth', path, maxLines });
    else if (maxLines > before) violations.push({ kind: 'fixtures-ledger-raised', path, maxLines, base: before });
  }
  const scanner = compareScannerToBase(ref, baseScan, config, options);
  violations.push(...scanner.violations);
  return { violations, scannerOverride: scanner.scannerOverride };
}

function loadConfig(root, options) {
  if (options.config !== undefined) return parseScannerConfig(options.config);
  const path = join(root, CONFIG_RELATIVE_PATH);
  return parseScannerConfig(existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : DEFAULT_CONFIG);
}

export function check(root, options = {}) {
  const resolved = resolve(root);
  let config;
  try {
    config = loadConfig(resolved, options);
  } catch (error) {
    return { status: 'FAIL', violations: [{ kind: 'invalid-scanner-config', detail: error.message }] };
  }
  const ledgerPath = options.ledger ?? join(resolved, config.ledgerPath);
  let parsed;
  try {
    parsed = parseLedger(JSON.parse(readFileSync(ledgerPath, 'utf8')), 'ledger');
  } catch (error) {
    return { status: 'FAIL', violations: [{ kind: 'invalid-ledger', detail: error.message }] };
  }
  const { allowlist: allowed, pending, fixtures: ceilings } = parsed;
  const violations = [];
  let measured;
  try {
    measured = scan(resolved, config);
  } catch (error) {
    return { status: 'FAIL', violations: [{ kind: 'scan-failed', detail: error.message }] };
  }
  violations.push(...measured.problems);
  const current = new Map(measured.rows.map(row => [key(row), row.count]));
  for (const [entry, count] of allowed) {
    if ((current.get(entry) ?? 0) !== count) {
      violations.push({ kind: 'stale-allowlist-entry', ...describe(entry, { count: current.get(entry) ?? 0, allowed: count }) });
    }
    if (pending.has(entry)) violations.push({ kind: 'allowlisted-and-pending', ...describe(entry, {}) });
  }
  for (const [entry, count] of current) {
    if (allowed.has(entry)) continue;
    const expected = pending.get(entry);
    if (expected === undefined) violations.push({ kind: 'new-mock-source-finding', ...describe(entry, { count }) });
    else if (count > expected) violations.push({ kind: 'mock-source-finding-grew', ...describe(entry, { count, pending: expected }) });
    else if (count < expected) violations.push({ kind: 'stale-pending-entry', ...describe(entry, { count, pending: expected }) });
  }
  for (const [entry, expected] of pending) {
    if (!current.has(entry)) violations.push({ kind: 'stale-pending-entry', ...describe(entry, { count: 0, pending: expected }) });
  }
  const present = new Set(measured.fixtures.map(file => file.path));
  for (const file of measured.fixtures) {
    const ceiling = ceilings.get(file.path);
    if (ceiling === undefined) violations.push({ kind: 'unlisted-fixture-file', path: file.path, lines: file.lines });
    else if (file.lines > ceiling) violations.push({ kind: 'fixture-file-grew', path: file.path, lines: file.lines, maxLines: ceiling });
  }
  for (const path of ceilings.keys()) if (!present.has(path)) violations.push({ kind: 'stale-fixture-row', path });
  const against = options.baseRef === undefined ? { violations: [] } : compareToBase(resolved, ledgerPath, parsed, config, options);
  violations.push(...against.violations);
  return {
    status: violations.length ? 'FAIL' : 'PASS',
    baseRef: options.baseRef ?? null,
    firstSeed: against.firstSeed ?? false,
    scannerOverride: against.scannerOverride ?? false,
    violations,
    findings: current.size,
    allowlist: allowed.size,
    pending: pending.size,
    fixtures: ceilings.size,
  };
}

function writeLedger(root, ledgerPath, config) {
  const measured = scan(root, config);
  if (!existsSync(ledgerPath)) {
    const fixtures = measured.fixtures.map(file => ({ path: file.path, maxLines: file.lines }));
    writeFileSync(ledgerPath, `${JSON.stringify(_ledgerDocument([], measured.rows, fixtures), null, 2)}\n`);
    return { pending: measured.rows.length, fixtures: fixtures.length, seeded: true };
  }
  const previousDocument = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  const previous = parseLedger(previousDocument, 'ledger');
  const allowed = new Set(previous.allowlist.keys());
  const next = measured.rows.filter(row => !allowed.has(key(row)));
  const grown = next.filter(row => row.count > (previous.pending.get(key(row)) ?? 0));
  if (grown.length) throw new Error(`refusing to grow the pending list: ${grown.map(row => `${row.kind} ${row.path} ${row.subject}`).join('; ')}`);
  const fixtures = [];
  for (const file of measured.fixtures) {
    const ceiling = previous.fixtures.get(file.path);
    if (ceiling === undefined) throw new Error(`refusing to add a fixtures row: ${file.path}`);
    if (file.lines > ceiling) throw new Error(`refusing to raise a fixtures row: ${file.path}`);
    fixtures.push({ path: file.path, maxLines: Math.min(ceiling, file.lines) });
  }
  writeFileSync(ledgerPath, `${JSON.stringify(_ledgerDocument(previousDocument.allowlist, next, fixtures), null, 2)}\n`);
  return { pending: next.length, fixtures: fixtures.length, seeded: false };
}

function main(argv) {
  const options = { root: DEFAULT_ROOT, json: false, write: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--root') options.root = resolve(argv[++index]);
    else if (argument === '--ledger') options.ledger = resolve(argv[++index]);
    else if (argument === '--base-ref') options.baseRef = argv[++index] ?? '';
    else if (argument === '--base-branch') options.baseBranch = argv[++index];
    else if (argument === '--allow-first-seed') options.allowFirstSeed = true;
    else if (argument === '--json') options.json = true;
    else if (argument === '--write-ledger') options.write = true;
    else if (argument === '--print-scanner-digest') options.printDigest = true;
    else throw new Error(`unknown argument: ${argument}`);
  }
  const config = loadConfig(options.root, {});
  if (options.printDigest) {
    console.log(scannerDigest(config));
    return 0;
  }
  const ledgerPath = options.ledger ?? join(options.root, config.ledgerPath);
  if (options.write) {
    const written = writeLedger(options.root, ledgerPath, config);
    console.log(`${written.seeded ? 'seeded' : 'updated'} ${relative(options.root, ledgerPath)}: ${written.pending} pending row(s), ${written.fixtures} fixture row(s)`);
    return 0;
  }
  const result = check(options.root, { ledger: ledgerPath, baseRef: options.baseRef, baseBranch: options.baseBranch, allowFirstSeed: options.allowFirstSeed });
  if (options.json) console.log(JSON.stringify(result, null, 2));
  else {
    for (const violation of result.violations) console.error(`FAIL ${JSON.stringify(violation)}`);
    const note = (result.firstSeed ? ' (first seed: the base has no ledger)' : '') + (result.scannerOverride ? ' (scanner change cleared by REVIEWED_SCANNER_SHA256)' : '');
    console.log(`${result.status} GUI mock source${note} (findings=${result.findings} allowlist=${result.allowlist ?? 'n/a'} pending=${result.pending ?? 'n/a'} fixtures=${result.fixtures ?? 'n/a'})`);
  }
  return result.status === 'PASS' ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`FAIL ${error.message}`);
    process.exitCode = 1;
  }
}
