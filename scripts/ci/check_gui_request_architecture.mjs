#!/usr/bin/env node
/** Fail closed when Web GUI or its BFF grows daemon-request architecture drift. */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ts = await import('../../gui/node_modules/typescript/lib/typescript.js');
const DEFAULT_ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)));
export const BASELINE_RELATIVE_PATH = 'scripts/ci/gui-request-architecture-baseline.json';
export const REVIEWED_BASELINE_SHA256 = 'a659b32aed73376f2512d653659fb68130e887bee903adb49e607967978f6a23';
const FIRST_SEED_BASES = new Set(['dev', 'main']);
const SOURCE_ROOTS = ['gui/src', 'gui/server'];
const TRANSPORT_ROOTS = ['gui/src/client', 'gui/src/app/backend'];

function git(root, ...args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

export function _digest(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

export function _refExists(root, ref) {
  try {
    git(root, 'rev-parse', '--verify', `${ref}^{commit}`);
    return true;
  } catch {
    return false;
  }
}

function rows(findings) {
  const counts = new Map();
  for (const finding of findings) {
    const key = `${finding.kind}\0${finding.path}\0${finding.subject}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([key, count]) => {
    const [kind, path, subject] = key.split('\0');
    return { kind, path, subject, count };
  });
}

function summary(findings) {
  const result = {};
  for (const row of findings) result[row.kind] = (result[row.kind] ?? 0) + row.count;
  return Object.fromEntries(Object.entries(result).sort(([left], [right]) => left.localeCompare(right)));
}

export function _baselineDocument(findings) {
  return {
    version: 1,
    policy: 'Exact GUI request-architecture findings with occurrence counts. Entries may only leave or shrink; new transport exceptions require a reviewed checker change, never ledger growth.',
    findings,
  };
}

function baselineCounts(document) {
  if (!document || document.version !== 1 || !Array.isArray(document.findings)) throw new Error('baseline version/findings invalid');
  const result = new Map();
  for (const [index, row] of document.findings.entries()) {
    if (!row || Object.keys(row).length !== 4 || !['kind', 'path', 'subject', 'count'].every(key => Object.hasOwn(row, key))) {
      throw new Error(`invalid finding #${index}`);
    }
    if (![row.kind, row.path, row.subject].every(value => typeof value === 'string' && value.length > 0)) {
      throw new Error(`invalid finding key #${index}`);
    }
    if (!Number.isInteger(row.count) || row.count <= 0) throw new Error(`invalid finding count #${index}`);
    const key = `${row.kind}\0${row.path}\0${row.subject}`;
    if (result.has(key)) throw new Error(`duplicate finding ${key}`);
    result.set(key, row.count);
  }
  return result;
}

function loadBaseline(path) {
  return baselineCounts(JSON.parse(readFileSync(path, 'utf8')));
}

function loadGitBaseline(root, ref) {
  if (!_refExists(root, ref)) throw new Error(`base ref does not exist: ${ref}`);
  try {
    git(root, 'cat-file', '-e', `${ref}:${BASELINE_RELATIVE_PATH}`);
  } catch {
    return null;
  }
  const text = git(root, 'show', `${ref}:${BASELINE_RELATIVE_PATH}`);
  return baselineCounts(JSON.parse(text));
}

function asCounts(findings) {
  return new Map(findings.map(row => [`${row.kind}\0${row.path}\0${row.subject}`, row.count]));
}

function catalog(root) {
  const document = JSON.parse(readFileSync(join(root, 'contract/daemon-request/openapi.json'), 'utf8'));
  const methods = document['x-hyprial-methods'].map(row => row.name);
  const paths = new Map();
  const camel = value => value.replace(/[-_](.)/g, (_, character) => character.toUpperCase());
  for (const method of methods) {
    const path = method.split('.').map(camel).join('.');
    paths.set(path, method);
  }
  return { methods: new Set(methods), paths };
}

function sourceFiles(root) {
  const result = [];
  function walk(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (/\.(?:ts|tsx|mts|cts|mjs|cjs)$/.test(entry.name)) result.push(path);
    }
  }
  for (const prefix of SOURCE_ROOTS) {
    const directory = join(root, prefix);
    if (existsSync(directory)) walk(directory);
  }
  return result.filter(path => {
    const name = relative(root, path).replaceAll('\\', '/');
    return !name.startsWith('gui/src/client/generated/')
      && !name.startsWith('gui/src/dev-mode/')
      && !name.includes('/dev/')
      && !name.includes('/__tests__/')
      && !/\.d\.[cm]?ts$/.test(name)
      && !/\.(?:test|spec)\.[cm]?[jt]sx?$/.test(name);
  });
}

function propertyChain(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isThis(node)) return 'this';
  if (ts.isPropertyAccessExpression(node)) return `${propertyChain(node.expression)}.${node.name.text}`;
  return null;
}

function requestFromCall(node, contract) {
  const chain = propertyChain(node.expression);
  if (!chain) return null;
  if ((chain.endsWith('.invoke') || chain.endsWith('.invokeDetailed')) && node.arguments.length > 0) {
    const first = node.arguments[0];
    return {
      method: ts.isStringLiteral(first) ? first.text : null,
      dynamic: !ts.isStringLiteral(first),
      transport: true,
    };
  }
  if (chain === 'callAgentMethod' && node.arguments.length > 0) {
    const first = node.arguments[0];
    return {
      method: ts.isStringLiteral(first) ? first.text : null,
      dynamic: !ts.isStringLiteral(first),
      transport: false,
    };
  }
  for (const [path, method] of [...contract.paths.entries()].sort(([left], [right]) => right.length - left.length)) {
    if (chain === path || chain.endsWith(`.${path}`)) return { method, dynamic: false, transport: false };
  }
  return null;
}

function scanSource(root, path, contract) {
  const name = relative(root, path).replaceAll('\\', '/');
  const findings = [];
  const text = readFileSync(path, 'utf8');
  const source = ts.createSourceFile(name, text, ts.ScriptTarget.ESNext, true, name.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const inTransportLayer = TRANSPORT_ROOTS.some(prefix => name.startsWith(prefix));
  if (source.parseDiagnostics?.length) findings.push({ kind: 'unreadable-source', path: name, subject: path.split('/').pop() });
  if (name.startsWith('gui/server/')) {
    findings.push({ kind: 'server-business-module', path: name, subject: dirname(name) });
  }
  const requests = [];
  function visit(node, owners, loopDepth) {
    if (ts.isImportDeclaration(node)) {
      const specifier = node.moduleSpecifier.text;
      if (['node:child_process', 'node:fs', 'node:fs/promises'].includes(specifier)) {
        findings.push({ kind: 'node-authority-import', path: name, subject: specifier });
      }
    }
    if (ts.isCallExpression(node)) {
      const chain = propertyChain(node.expression);
      const owner = owners.length ? owners.join('.') : '<module>';
      if (chain === 'fetch' || chain === 'globalThis.fetch' || chain === 'window.fetch'
          || chain === 'XMLHttpRequest' || chain === 'WebSocket' || chain === 'EventSource') {
        if (!TRANSPORT_ROOTS.some(prefix => name.startsWith(prefix))) {
          findings.push({ kind: 'direct-network', path: name, subject: chain });
        }
      }
      if (chain && /(?:^|\.)(?:localStorage|sessionStorage)\.(?:getItem|setItem|removeItem)$/.test(chain)) {
        findings.push({ kind: 'browser-storage', path: name, subject: chain });
      }
      const request = requestFromCall(node, contract);
      if (request) {
        requests.push({ owner, request });
        if (request.transport && !inTransportLayer) {
          findings.push({ kind: 'transport-bypass', path: name, subject: owner });
        }
        if (request.dynamic && !inTransportLayer) findings.push({ kind: 'dynamic-method', path: name, subject: owner });
        else if (request.method && !contract.methods.has(request.method)) {
          findings.push({ kind: 'undeclared-method', path: name, subject: `${owner}:${request.method}` });
        }
        if (loopDepth > 0) findings.push({ kind: 'request-in-loop', path: name, subject: owner });
      }
    }
    if (ts.isNewExpression(node)) {
      const chain = propertyChain(node.expression);
      if (['XMLHttpRequest', 'WebSocket', 'EventSource', 'globalThis.XMLHttpRequest',
           'globalThis.WebSocket', 'globalThis.EventSource'].includes(chain)
          && !TRANSPORT_ROOTS.some(prefix => name.startsWith(prefix))) {
        findings.push({ kind: 'direct-network', path: name, subject: chain });
      }
    }
    const functionNode = ts.isFunctionDeclaration(node)
      || ts.isFunctionExpression(node)
      || ts.isArrowFunction(node)
      || ts.isMethodDeclaration(node)
      || ts.isGetAccessorDeclaration(node)
      || ts.isSetAccessorDeclaration(node)
      || ts.isConstructorDeclaration(node);
    const nextOwners = functionNode
      ? [...owners, node.name?.getText(source) ?? '<anonymous>']
      : owners;
    const nextLoop = loopDepth + (ts.isForStatement(node) || ts.isForInStatement(node) || ts.isForOfStatement(node) || ts.isWhileStatement(node) || ts.isDoStatement(node) ? 1 : 0);
    ts.forEachChild(node, child => visit(child, nextOwners, nextLoop));
  }
  ts.forEachChild(source, node => visit(node, [], 0));
  const counts = new Map();
  for (const request of requests) counts.set(request.owner, (counts.get(request.owner) ?? 0) + 1);
  for (const [owner, count] of counts) {
    if (count > 1) for (let index = 0; index < count; index += 1) findings.push({ kind: 'request-fanout', path: name, subject: owner });
  }
  return findings;
}

export function scan(root) {
  const resolved = resolve(root);
  try {
    const contract = catalog(resolved);
    const findings = sourceFiles(resolved).flatMap(path => scanSource(resolved, path, contract));
    const result = rows(findings);
    return { status: result.length ? 'FAIL' : 'PASS', findings: result, violations: result, summary: summary(result) };
  } catch (error) {
    const row = { kind: 'invalid-catalog', path: 'contract/daemon-request/openapi.json', subject: error.message, count: 1 };
    return { status: 'FAIL', findings: [row], violations: [row], summary: { 'invalid-catalog': 1 } };
  }
}

function currentProblems(current, baseline) {
  const problems = [];
  for (const [key, count] of [...current.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    const [kind, path, subject] = key.split('\0');
    const previous = baseline.get(key);
    if (previous === undefined) problems.push({ kind: 'new-architecture-finding', path, subject, finding: kind, count });
    else if (count > previous) problems.push({ kind: 'architecture-finding-grew', path, subject, finding: kind, count, baseline: previous });
    else if (count < previous) problems.push({ kind: 'stale-baseline-entry', path, subject, finding: kind, count, baseline: previous });
  }
  for (const [key, previous] of [...baseline.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    if (!current.has(key)) {
      const [kind, path, subject] = key.split('\0');
      problems.push({ kind: 'stale-baseline-entry', path, subject, finding: kind, count: 0, baseline: previous });
    }
  }
  return problems;
}

function baseProblems(current, baseline, base) {
  const problems = [];
  for (const [key, count] of [...current.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    if (count > (base.get(key) ?? 0)) {
      const [kind, path, subject] = key.split('\0');
      problems.push({ kind: 'base-architecture-growth', path, subject, finding: kind, count, base: base.get(key) ?? 0 });
    }
  }
  for (const [key, count] of [...baseline.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    if (count > (base.get(key) ?? 0)) {
      const [kind, path, subject] = key.split('\0');
      problems.push({ kind: 'ledger-growth', path, subject, finding: kind, count, base: base.get(key) ?? 0 });
    }
  }
  return problems;
}

function firstSeedProblems(baseline, baseBranch, reviewedDigest) {
  if (!FIRST_SEED_BASES.has(baseBranch)) return [{ kind: 'missing-base-ledger', detail: 'first seed is limited to dev/main' }];
  if (_digest(baseline) !== reviewedDigest) {
    return [{ kind: 'seed-digest-mismatch', path: BASELINE_RELATIVE_PATH }];
  }
  return [];
}

export function check(root, baseline, options = {}) {
  const resolved = resolve(root);
  const scanned = scan(resolved);
  const current = asCounts(scanned.findings);
  let baselineCountsValue;
  try {
    baselineCountsValue = loadBaseline(baseline);
  } catch (error) {
    return { status: 'FAIL', violations: [{ kind: 'invalid-baseline', detail: error.message }], findings: scanned.findings, summary: scanned.summary };
  }
  const violations = scanned.findings.filter(row => {
    const key = `${row.kind}\0${row.path}\0${row.subject}`;
    return !baselineCountsValue.has(key) || row.count > baselineCountsValue.get(key);
  });
  violations.push(...currentProblems(current, baselineCountsValue));
  let firstSeedUsed = false;
  const reviewedDigest = options.reviewedDigest ?? REVIEWED_BASELINE_SHA256;
  if (options.baseRef) {
    try {
      const base = loadGitBaseline(resolved, options.baseRef);
      if (base === null) {
        if (options.allowFirstSeed) {
          const problems = firstSeedProblems(baseline, options.baseBranch ?? '', reviewedDigest);
          if (problems.length) violations.push(...problems);
          else firstSeedUsed = true;
        } else {
          violations.push({ kind: 'missing-base-ledger', detail: `${BASELINE_RELATIVE_PATH} is absent at ${options.baseRef}` });
        }
      } else {
        violations.push(...baseProblems(current, baselineCountsValue, base));
      }
    } catch (error) {
      violations.push({ kind: 'invalid-base-ref', detail: error.message });
    }
  }
  return {
    status: violations.length ? 'FAIL' : 'PASS',
    baseRef: options.baseRef ?? null,
    firstSeedUsed,
    findings: scanned.findings,
    summary: scanned.summary,
    violations,
  };
}

export function writeBaseline(root, baseline) {
  const resolved = resolve(root);
  const scanned = scan(resolved);
  if (existsSync(baseline)) {
    const previous = loadBaseline(baseline);
    const current = asCounts(scanned.findings);
    const problems = currentProblems(current, previous).filter(row => ['new-architecture-finding', 'architecture-finding-grew'].includes(row.kind));
    if (problems.length) return { status: 'FAIL', violations: problems };
  }
  mkdirSync(dirname(baseline), { recursive: true });
  writeFileSync(baseline, `${JSON.stringify(_baselineDocument(scanned.findings), null, 2)}\n`);
  return { status: 'PASS', violations: [], findings: scanned.findings, summary: scanned.summary };
}

function main(argv) {
  const options = { root: DEFAULT_ROOT, baseRef: null, baseBranch: null, allowFirstSeed: false, writeBaseline: false, json: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--root') options.root = resolve(argv[++index]);
    else if (argument === '--baseline') options.baseline = resolve(argv[++index]);
    else if (argument === '--base-ref') options.baseRef = argv[++index];
    else if (argument === '--base-branch') options.baseBranch = argv[++index];
    else if (argument === '--allow-first-seed') options.allowFirstSeed = true;
    else if (argument === '--write-baseline') options.writeBaseline = true;
    else if (argument === '--json') options.json = true;
    else throw new Error(`unknown argument: ${argument}`);
  }
  const baseline = options.baseline ?? join(options.root, BASELINE_RELATIVE_PATH);
  const report = options.writeBaseline ? writeBaseline(options.root, baseline) : check(options.root, baseline, options);
  if (options.json) console.log(JSON.stringify(report, null, 2));
  else {
    console.log(`${report.status} GUI daemon request architecture`);
    for (const row of report.violations) console.log(JSON.stringify(row));
  }
  return report.status === 'PASS' ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
