/**
 * Self-test for check_gui_mock_source.mjs. Run by gate-gui (it needs
 * gui/node_modules/typescript), not by gate-pr-hygiene, so the file name is
 * *.selftest.mjs and not *.test.mjs.
 *
 * Decision record (2026-11-15, one JavaScript Contract Mock, PR 1e-2):
 * - Every case builds a synthetic repository tree in a temporary directory, so a
 *   rule is proved on files the real repository does not have: clean trees pass;
 *   each finding kind fails when its form is added; the sanctioned forms stay
 *   legal; the ledger and the scanner configuration are compared with a git base
 *   in every direction the gate claims to catch.
 * - The tree helper seals a ledger from what the clean tree contains and writes
 *   the violating files afterwards, so a case fails for the one reason it names.
 * - The real repository is checked last: the committed ledger must describe it
 *   exactly.
 * - The launch patterns of the Python mock are assembled from pieces here, so
 *   this file does not match the gate it tests.
 */
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  CONFIG_RELATIVE_PATH,
  FINDING_KINDS,
  FIRST_SEED_SCANNER_SHA256,
  LEDGER_RELATIVE_PATH,
  REVIEWED_SCANNER_SHA256,
  _ledgerDocument,
  check as checkWithPin,
  compareScannerConfigs,
  parseLedger,
  scan,
  scannerDigest,
} from './check_gui_mock_source.mjs';

const check = (root, options = {}) => checkWithPin(root, { reviewedScannerSha256: null, ...options });
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const checker = join(repoRoot, 'scripts/ci/check_gui_mock_source.mjs');
const CONFIG_TEXT = readFileSync(join(repoRoot, CONFIG_RELATIVE_PATH), 'utf8');
const BASE_CONFIG = JSON.parse(CONFIG_TEXT);
const REASON = 'documented upstream requirement';
const PY_FLAG = `-m ${'contract'}_mock`;
const PY_SERVICE = `Mock${'Service'}`;

const OPENAPI = {
  openapi: '3.1.0',
  paths: {},
  'x-hyprial-methods': [
    { name: 'agent.list', 'x-hyprial-examples': [{ name: 'one-agent' }, { name: 'empty-registry' }] },
    { name: 'agent.create', 'x-hyprial-examples': [{ name: 'created' }] },
    { name: 'hosts', 'x-hyprial-examples': [{ name: 'online-host' }] },
  ],
};
const PRESETS = {
  version: 1,
  presets: { success: { scenario: 'success' }, error: { scenario: 'error' } },
  transitions: [],
  inputGuards: [{
    method: 'agent.create',
    requireOneOf: [{ path: ['setupToken'], equals: 'example-not-a-credential' }],
    refusal: { code: 'INVALID_ARGUMENT', message: 'documented sentinel only' },
  }],
};

const CLEAN = {
  'gui/src/app/page.ts': "export const page = () => 'page';\n",
  'gui/src/dev/FixtureApp.tsx': 'export const a = 1;\nexport const b = 2;\n',
  'gui/src/dev/mock/one.ts': 'export const one = 1;\n',
  'gui/src/dev/tools/helper.ts': 'export const helper = 1;\n',
  'gui/src/dev/contract-mock/index.ts': "import table from './contract.generated.json';\nimport { createContractMock } from '../../../contract-mock/index.ts';\nexport { table, createContractMock };\n",
  'gui/src/dev/contract-mock/contract.generated.json': '{"version":1}\n',
  'gui/src/client/generated/index.ts': "export const methodMetadata = { 'agent.list': { mutates: false } };\n",
  'gui/tsconfig.json': `${JSON.stringify({ compilerOptions: { paths: { '@/*': ['./src/*'] } } })}\n`,
  'gui/contract-mock/index.ts': "export const name = 'contract-mock';\n",
  'gui/contract-mock/presets.json': `${JSON.stringify(PRESETS)}\n`,
  'gui/scripts/generate-reference-client.mjs': "export const table = new URL('../src/dev/contract-mock/contract.generated.json', import.meta.url);\n",
  'gui/tests/contract-mock/one.test.ts': "import { name } from '../../contract-mock/index.ts';\nexport const sample = name;\nexport const double = { 'agent.list': 1 };\n",
  'contract/daemon-request/openapi.json': `${JSON.stringify(OPENAPI)}\n`,
  'contract/daemon-request/contract_mock/__init__.py': '',
  'contract/daemon-request/contract_mock/checks.py': 'def check_contract(contract):\n    return contract\n',
  'contract/daemon-request/contract_mock/contract.py': 'LOADER = 1\n',
  'contract/daemon-request/contract_mock/envelope.py': 'ENVELOPE = 1\n',
  'contract/daemon-request/contract_mock/sensitive.py': 'SENTINEL = 1\n',
  '.github/workflows/gate.yml': 'name: gate\njobs: {}\n',
  'src/hyprial/x.py': 'X = 1\n',
  'tests/x.py': 'X = 1\n',
  'scripts/x.sh': 'echo ok\n',
};

function gitIn(root, ...args) {
  return execFileSync('git', ['-C', root, '-c', 'user.email=t@example.invalid', '-c', 'user.name=t', ...args], { stdio: 'pipe' });
}

function put(root, files) {
  for (const [path, text] of Object.entries(files)) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), text);
  }
}

function seal(root, { allowlist = [], pending = null } = {}) {
  const measured = scan(root, BASE_CONFIG);
  const fixtures = measured.fixtures.map(file => ({ path: file.path, maxLines: file.lines }));
  const document = _ledgerDocument(allowlist, pending ?? measured.rows.filter(row => !allowlist.some(entry => entry.kind === row.kind && entry.path === row.path && entry.subject === row.subject)), fixtures);
  mkdirSync(dirname(join(root, LEDGER_RELATIVE_PATH)), { recursive: true });
  writeFileSync(join(root, LEDGER_RELATIVE_PATH), `${JSON.stringify(document, null, 2)}\n`);
  return document;
}

function tree(files = {}, sealOptions = {}) {
  const root = mkdtempSync(join(tmpdir(), 'gui-mock-source-'));
  put(root, { ...CLEAN, ...files, [CONFIG_RELATIVE_PATH]: CONFIG_TEXT });
  seal(root, sealOptions);
  return root;
}

function withTree(files, run, sealOptions) {
  const root = tree(files, sealOptions);
  try {
    return run(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function violationKinds(result) {
  return result.violations.map(row => row.kind);
}

function broken(extra, expected, { remove = [], finding } = {}) {
  withTree({}, (root) => {
    put(root, extra);
    for (const path of remove) rmSync(join(root, path));
    const result = check(root);
    assert.equal(result.status, 'FAIL');
    for (const kind of expected) assert.ok(violationKinds(result).includes(kind), `${kind} missing from ${violationKinds(result).join()}`);
    if (finding !== undefined) assert.ok(result.violations.some(row => row.finding === finding), `no ${finding} finding in ${JSON.stringify(result.violations)}`);
  });
}

function withBase(setup, run) {
  const root = tree();
  try {
    gitIn(root, 'init', '-q');
    setup?.base?.(root);
    gitIn(root, 'add', '-A');
    gitIn(root, 'commit', '-q', '-m', 'base');
    setup?.head?.(root);
    return run(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test('1. a clean tree passes, with every sanctioned form present', () => {
  withTree({}, (root) => {
    const result = check(root);
    assert.deepEqual(result.violations, []);
    assert.equal(result.status, 'PASS');
    assert.equal(result.fixtures, 2);
  });
});

test('2. a data file under gui/src that is not the generated table fails', () => {
  broken({ 'gui/src/app/data.json': '{}\n' }, ['new-mock-source-finding'], { finding: 'data-file' });
});

test('3. an unlisted file under gui/src/dev fails', () => {
  broken({ 'gui/src/dev/mock/two.ts': 'export const two = 2;\n' }, ['unlisted-fixture-file']);
});

test('4. a fixture file over its line ceiling fails, and one under it does not', () => {
  broken({ 'gui/src/dev/FixtureApp.tsx': 'export const a = 1;\nexport const b = 2;\nexport const c = 3;\n' }, ['fixture-file-grew']);
  withTree({}, (root) => {
    put(root, { 'gui/src/dev/FixtureApp.tsx': 'export const a = 1;\n' });
    assert.deepEqual(check(root).violations, []);
  });
});

test('5. a fixtures row for a missing file fails', () => {
  broken({}, ['stale-fixture-row'], { remove: ['gui/src/dev/mock/one.ts'] });
});

test('6. a switch case on a method name fails, and is counted per label', () => {
  broken({ 'gui/src/dev/mock/responder.ts': "export const f = (m: string) => { switch (m) { case 'agent.list': return 1; case 'agent.create': return 2; default: return 0; } };\n" }, ['new-mock-source-finding', 'unlisted-fixture-file'], { finding: 'method-keyed-responder' });
  withTree({ 'gui/src/dev/mock/responder.ts': "export const f = (m: string) => { switch (m) { case 'agent.list': return 1; default: return 0; } };\n" }, (root) => {
    assert.deepEqual(scan(root, BASE_CONFIG).rows.filter(row => row.kind === 'method-keyed-responder'), [
      { kind: 'method-keyed-responder', path: 'gui/src/dev/mock/responder.ts', subject: 'case-label', count: 1 },
    ]);
  });
});

test('7. a method name as an object key fails', () => {
  broken({ 'gui/src/app/table.ts': "export const table = { 'agent.list': 1 };\n" }, ['new-mock-source-finding'], { finding: 'method-keyed-responder' });
});

test('8. comparing with a method name fails, in either operand order', () => {
  for (const text of ["export const is = (m: string) => m === 'agent.list';\n", "export const is = (m: string) => 'agent.list' != m;\n"]) {
    broken({ 'gui/src/app/is.ts': text }, ['new-mock-source-finding'], { finding: 'method-keyed-responder' });
  }
});

test('9. the generated client root is exempt, and a hosts-like non-dotted name is not a method key', () => {
  withTree({ 'gui/src/app/hosts.ts': "export const table = { hosts: 1, 'not.a.method': 2 };\n" }, (root) => {
    assert.deepEqual(check(root).violations, []);
  });
});

test('10. the generated table imported from a page fails', () => {
  broken({ 'gui/src/app/page.ts': "import table from '../dev/contract-mock/contract.generated.json';\nexport const t = table;\n" }, ['new-mock-source-finding'], { finding: 'mock-table-import' });
});

test('11. a path literal of the generated table in gui/tests fails', () => {
  broken({ 'gui/tests/reads.test.ts': "export const file = '../../src/dev/contract-mock/contract.generated.json';\n" }, ['new-mock-source-finding'], { finding: 'mock-table-import' });
});

test('12. a product file importing gui/contract-mock fails; the sanctioned importers pass', () => {
  broken({ 'gui/src/app/page.ts': "import { name } from '../../contract-mock/index.ts';\nexport const n = name;\n" }, ['new-mock-source-finding'], { finding: 'mock-module-import' });
  withTree({}, (root) => {
    assert.deepEqual(scan(root, BASE_CONFIG).rows.filter(row => row.kind === 'mock-module-import'), []);
  });
});

test('12b. the alias, a dynamic import, a require, a URL and a glob into gui/contract-mock are caught like a relative import', () => {
  const forms = {
    alias: "import { name } from '@/../contract-mock/index.ts';\nexport const n = name;\n",
    aliasExport: "export * from '@/../contract-mock/index.ts';\n",
    dynamic: "export const load = () => import('@/../contract-mock/index.ts');\n",
    require: "const m = require('@/../contract-mock/index.ts');\nexport default m;\n",
    relativeDynamic: "export const load = () => import('../../../contract-mock/index.ts');\n",
    url: "export const u = new URL('../../../contract-mock/index.ts', import.meta.url);\n",
    glob: "export const g = import.meta.glob('../../../contract-mock/*.ts');\n",
  };
  for (const [label, text] of Object.entries(forms)) {
    withTree({}, (root) => {
      put(root, { 'gui/src/features/a/impl.ts': text });
      const rows = scan(root, BASE_CONFIG).rows.filter(row => row.kind === 'mock-module-import');
      assert.deepEqual(rows.map(row => row.path), ['gui/src/features/a/impl.ts'], label);
    });
  }
  withTree({}, (root) => {
    put(root, { 'gui/src/features/a/ok.ts': "import { createContractMock } from '@/dev/contract-mock/index.ts';\nexport const c = createContractMock;\n" });
    assert.deepEqual(scan(root, BASE_CONFIG).rows.filter(row => row.kind === 'mock-module-import'), []);
  });
  withTree({}, (root) => {
    put(root, {
      'gui/tsconfig.json': `${JSON.stringify({ compilerOptions: { paths: { '@/*': ['./src/*'], '~/*': ['./contract-mock/*'] } } })}\n`,
      'gui/src/features/a/impl.ts': "import { name } from '~/index.ts';\nexport const n = name;\n",
    });
    assert.deepEqual(scan(root, BASE_CONFIG).rows.filter(row => row.kind === 'mock-module-import').map(row => row.path), ['gui/src/features/a/impl.ts']);
  });
  withTree({}, (root) => {
    put(root, { 'gui/src/features/a/impl.ts': "import x from '@/../contract-mock/index.ts';\nexport default x;\n" });
    assert.ok(violationKinds(check(root)).includes('new-mock-source-finding'));
    put(root, { 'gui/src/features/a/impl.ts': "export const ok = 1;\n" });
    assert.deepEqual(check(root).violations, []);
  });
});

test('13. a method name literal inside the mock module fails', () => {
  broken({ 'gui/contract-mock/respond.ts': "export const first = 'agent.list';\n" }, ['new-mock-source-finding'], { finding: 'mock-engine-method-literal' });
});

test('14. a preset document with a payload key fails and is never ledgerable', () => {
  const dirty = { ...PRESETS, presets: { success: { scenario: 'success', result: { ok: true } } } };
  broken({ 'gui/contract-mock/presets.json': `${JSON.stringify(dirty)}\n` }, ['preset-payload']);
  assert.ok(!FINDING_KINDS.has('preset-payload') && !FINDING_KINDS.has('preset-unresolved'));
});

test('15. a preset reference that does not resolve fails', () => {
  const unknownCase = { ...PRESETS, presets: { success: { scenario: 'success', cases: { 'agent.list': 'no-such-case' } } } };
  broken({ 'gui/contract-mock/presets.json': `${JSON.stringify(unknownCase)}\n` }, ['preset-unresolved']);
  const unknownMethod = { ...PRESETS, presets: { success: { scenario: 'success', methodScenarios: { 'no.such': 'empty' } } } };
  broken({ 'gui/contract-mock/presets.json': `${JSON.stringify(unknownMethod)}\n` }, ['preset-unresolved']);
  const unknownPreset = { ...PRESETS, transitions: [{ preset: 'success', method: 'agent.list', next: 'missing' }] };
  broken({ 'gui/contract-mock/presets.json': `${JSON.stringify(unknownPreset)}\n` }, ['preset-unresolved']);
});

test('16. an extra Python file under contract/daemon-request fails', () => {
  broken({ 'contract/daemon-request/contract_mock/server.py': 'SERVER = 1\n' }, ['new-mock-source-finding'], { finding: 'python-mock-file' });
  broken({ 'contract/daemon-request/other.py': 'OTHER = 1\n' }, ['new-mock-source-finding'], { finding: 'python-mock-file' });
});

test('17. an HTTP server pattern in an allowed Python file fails', () => {
  for (const text of ['from http.server import HTTPServer\n', 'import socketserver\n', 'def start_server():\n    pass\n']) {
    broken({ 'contract/daemon-request/contract_mock/contract.py': text }, ['new-mock-source-finding'], { finding: 'python-mock-server' });
  }
});

test('18. a launch of the Python mock in a workflow, a script or a test fails', () => {
  broken({ '.github/workflows/gate.yml': `jobs:\n  a:\n    run: uv run python ${PY_FLAG}\n` }, ['new-mock-source-finding'], { finding: 'python-mock-launch' });
  broken({ 'scripts/start.sh': `uv run python ${PY_FLAG} --scenario success\n` }, ['new-mock-source-finding'], { finding: 'python-mock-launch' });
  broken({ 'tests/y.py': `from x import ${PY_SERVICE}\n` }, ['new-mock-source-finding'], { finding: 'python-mock-launch' });
});

function sealedGrowth(base, head, kinds) {
  withBase({
    base: (root) => base(root),
    head: (root) => head(root),
  }, (root) => {
    const result = check(root, { baseRef: 'HEAD' });
    assert.equal(result.status, 'FAIL');
    for (const kind of kinds) assert.ok(violationKinds(result).includes(kind), `${kind} missing from ${violationKinds(result).join()}`);
  });
}

test('19. ledger growth against a git base fails in four forms', () => {
  const responder = (labels) => `export const f = (m: string) => { switch (m) { ${labels.map(label => `case '${label}': return 1;`).join(' ')} default: return 0; } };\n`;
  const sealWith = (root) => {
    const measured = scan(root, BASE_CONFIG);
    const document = _ledgerDocument([], measured.rows, measured.fixtures.map(file => ({ path: file.path, maxLines: file.lines })));
    writeFileSync(join(root, LEDGER_RELATIVE_PATH), `${JSON.stringify(document, null, 2)}\n`);
  };
  sealedGrowth(() => {}, (root) => { put(root, { 'gui/src/app/r.ts': responder(['agent.list']) }); sealWith(root); }, ['pending-ledger-growth']);
  sealedGrowth(
    (root) => { put(root, { 'gui/src/app/r.ts': responder(['agent.list']) }); sealWith(root); },
    (root) => { put(root, { 'gui/src/app/r.ts': responder(['agent.list', 'agent.create']) }); sealWith(root); },
    ['pending-ledger-growth'],
  );
  sealedGrowth(() => {}, (root) => { put(root, { 'gui/src/dev/mock/new.ts': 'export const n = 1;\n' }); sealWith(root); }, ['fixtures-ledger-growth']);
  sealedGrowth(() => {}, (root) => { put(root, { 'gui/src/dev/FixtureApp.tsx': 'export const a = 1;\nexport const b = 2;\nexport const c = 3;\n' }); sealWith(root); }, ['fixtures-ledger-raised']);
  withBase({ head: (root) => { put(root, { 'gui/src/dev/FixtureApp.tsx': 'export const a = 1;\n' }); } }, (root) => {
    assert.deepEqual(check(root, { baseRef: 'HEAD' }).violations, []);
  });
  sealedGrowth(() => {}, (root) => {
    const document = JSON.parse(readFileSync(join(root, LEDGER_RELATIVE_PATH), 'utf8'));
    document.allowlist.push({ kind: 'data-file', path: 'gui/src/app/data.json', subject: '.json', count: 1, reason: REASON });
    put(root, { 'gui/src/app/data.json': '{}\n', [LEDGER_RELATIVE_PATH]: `${JSON.stringify(document, null, 2)}\n` });
  }, ['allowlist-ledger-growth']);
});

test('20. an empty or unresolvable base ref fails closed', () => {
  withBase({}, (root) => {
    assert.deepEqual(violationKinds(check(root, { baseRef: '' })), ['invalid-base-ref']);
    assert.deepEqual(violationKinds(check(root, { baseRef: 'no-such-ref' })), ['invalid-base-ref']);
  });
});

test('21. the first seed passes only with the flag, a dev/main base and the pinned digest', () => {
  const seeded = tree();
  try {
    gitIn(seeded, 'init', '-q');
    const ledger = readFileSync(join(seeded, LEDGER_RELATIVE_PATH));
    rmSync(join(seeded, LEDGER_RELATIVE_PATH));
    gitIn(seeded, 'add', '-A');
    gitIn(seeded, 'commit', '-q', '-m', 'base');
    writeFileSync(join(seeded, LEDGER_RELATIVE_PATH), ledger);
    const digest = createHash('sha256').update(ledger).digest('hex');
    const accepted = check(seeded, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: digest, firstSeedScannerDigest: scannerDigest(BASE_CONFIG) });
    assert.equal(accepted.status, 'PASS');
    assert.equal(accepted.firstSeed, true);
    assert.deepEqual(violationKinds(check(seeded, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: '0'.repeat(64) })), ['seed-digest-mismatch']);
    assert.deepEqual(
      violationKinds(check(seeded, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: digest, firstSeedScannerDigest: '0'.repeat(64) })),
      ['seed-scanner-mismatch'],
    );
    writeFileSync(join(seeded, CONFIG_RELATIVE_PATH), `${JSON.stringify({ ...BASE_CONFIG, launchPatterns: [] }, null, 2)}\n`);
    assert.ok(violationKinds(check(seeded, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'dev', firstSeedDigest: digest })).includes('seed-scanner-mismatch'));
    writeFileSync(join(seeded, CONFIG_RELATIVE_PATH), CONFIG_TEXT);
    assert.deepEqual(violationKinds(check(seeded, { baseRef: 'HEAD', allowFirstSeed: true, baseBranch: 'feature', firstSeedDigest: digest })), ['missing-base-ledger']);
    assert.deepEqual(violationKinds(check(seeded, { baseRef: 'HEAD', baseBranch: 'dev', firstSeedDigest: digest })), ['missing-base-ledger']);
  } finally {
    rmSync(seeded, { recursive: true, force: true });
  }
});

function configDecision(change, options = {}) {
  let result;
  withBase({}, (root) => {
    writeFileSync(join(root, CONFIG_RELATIVE_PATH), `${JSON.stringify({ ...BASE_CONFIG, ...change }, null, 2)}\n`);
    result = check(root, { baseRef: 'HEAD', ...options });
  });
  return result;
}

test('22. the scanner configuration is pinned to the base: narrowed fails, widened passes, a digest clears', () => {
  const narrowed = configDecision({ launchPatterns: BASE_CONFIG.launchPatterns.slice(1) });
  assert.ok(violationKinds(narrowed).includes('scanner-narrowed'));
  assert.ok(violationKinds(configDecision({ sourceRoots: [] })).includes('scanner-narrowed'));
  const widened = configDecision({ launchPatterns: [...BASE_CONFIG.launchPatterns, 'extra-pattern'] });
  assert.deepEqual(widened.violations, []);
  const exempt = { tableImporters: [...BASE_CONFIG.tableImporters, 'gui/src/app/page.ts'] };
  assert.ok(violationKinds(configDecision(exempt)).includes('scanner-exclusion-added'));
  assert.ok(violationKinds(configDecision({ skippedDirectories: [...BASE_CONFIG.skippedDirectories, 'dev'] })).includes('scanner-exclusion-added'));
  assert.ok(violationKinds(configDecision({ presetsPath: 'gui/contract-mock/other.json' })).includes('scanner-constant-changed'));
  const digest = scannerDigest({ ...BASE_CONFIG, ...exempt });
  const cleared = configDecision(exempt, { reviewedScannerSha256: digest });
  assert.equal(cleared.status, 'PASS');
  assert.equal(cleared.scannerOverride, true);
  assert.equal(configDecision(exempt, { reviewedScannerSha256: '0'.repeat(64) }).status, 'FAIL');
  assert.deepEqual(compareScannerConfigs(BASE_CONFIG, BASE_CONFIG), []);
});

test('23. an unknown key or kind, or an allowlist row without a reason, fails', () => {
  withTree({}, (root) => {
    const base = JSON.parse(readFileSync(join(root, LEDGER_RELATIVE_PATH), 'utf8'));
    const write = (document) => writeFileSync(join(root, LEDGER_RELATIVE_PATH), `${JSON.stringify(document)}\n`);
    write({ ...base, extra: [] });
    assert.deepEqual(violationKinds(check(root)), ['invalid-ledger']);
    write({ ...base, pending: [{ kind: 'preset-payload', path: 'a', subject: 'b', count: 1 }] });
    assert.deepEqual(violationKinds(check(root)), ['invalid-ledger']);
    write({ ...base, allowlist: [{ kind: 'data-file', path: 'gui/src/app/data.json', subject: '.json', count: 1 }] });
    assert.deepEqual(violationKinds(check(root)), ['invalid-ledger']);
    write({ ...base, allowlist: [{ kind: 'data-file', path: 'gui/src/app/data.json', subject: '.json', count: 1, reason: 'short' }] });
    assert.deepEqual(violationKinds(check(root)), ['invalid-ledger']);
    write({ ...base, fixtures: [{ path: 'gui/src/dev/FixtureApp.tsx' }] });
    assert.deepEqual(violationKinds(check(root)), ['invalid-ledger']);
    put(root, { 'gui/src/app/data.json': '{}\n' });
    write({ ...base, allowlist: [{ kind: 'data-file', path: 'gui/src/app/data.json', subject: '.json', count: 1, reason: REASON }] });
    assert.deepEqual(check(root).violations, []);
    assert.throws(() => parseLedger({ ...base, fixtures: [...base.fixtures, base.fixtures[0]] }, 'ledger'), /duplicate/);
  });
});

test('the command line exits 1 on a violation and prints one JSON object with --json', () => {
  withTree({}, (root) => {
    put(root, { 'gui/src/app/data.json': '{}\n' });
    const run = spawnSync(process.execPath, [checker, '--root', root, '--json'], { encoding: 'utf8' });
    assert.equal(run.status, 1);
    const parsed = JSON.parse(run.stdout);
    assert.equal(parsed.status, 'FAIL');
    assert.ok(parsed.violations.length > 0);
    assert.equal(spawnSync(process.execPath, [checker, '--root', root, '--no-such-flag'], { encoding: 'utf8' }).status, 1);
  });
});

test('--write-ledger seeds a missing ledger, then only lowers or removes', () => {
  const root = mkdtempSync(join(tmpdir(), 'gui-mock-source-write-'));
  try {
    put(root, { ...CLEAN, [CONFIG_RELATIVE_PATH]: CONFIG_TEXT });
    const run = (...args) => spawnSync(process.execPath, [checker, '--root', root, ...args], { encoding: 'utf8' });
    assert.equal(run('--write-ledger').status, 0);
    assert.equal(run().status, 0);
    put(root, { 'gui/src/dev/mock/two.ts': 'export const two = 2;\n' });
    assert.equal(run('--write-ledger').status, 1);
    rmSync(join(root, 'gui/src/dev/mock/two.ts'));
    rmSync(join(root, 'gui/src/dev/mock/one.ts'));
    assert.equal(run().status, 1);
    assert.equal(run('--write-ledger').status, 0);
    assert.equal(run().status, 0);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('the first-seed scanner digest constant is the digest of the committed configuration', () => {
  assert.equal(FIRST_SEED_SCANNER_SHA256, scannerDigest(BASE_CONFIG));
});

test('the review pin clears only a change whose configuration digest equals it', () => {
  const change = { tableImporters: [...BASE_CONFIG.tableImporters, 'gui/src/app/page.ts'] };
  const own = scannerDigest({ ...BASE_CONFIG, ...change });
  assert.equal(configDecision(change, { reviewedScannerSha256: own }).status, 'PASS');
  assert.equal(configDecision(change, { reviewedScannerSha256: scannerDigest(BASE_CONFIG) }).status, 'FAIL');
  assert.equal(configDecision(change, { reviewedScannerSha256: null }).status, 'FAIL');
});

test('the committed scanner configuration is valid and the review pin is null or a digest', () => {
  assert.ok(REVIEWED_SCANNER_SHA256 === null || /^[0-9a-f]{64}$/.test(REVIEWED_SCANNER_SHA256));
  assert.equal(JSON.parse(CONFIG_TEXT).version, 1);
});

test('24. the real repository passes, the ledger describes it exactly and no row is stale', () => {
  const result = check(repoRoot);
  assert.deepEqual(result.violations, []);
  assert.equal(result.status, 'PASS');
  const ledger = parseLedger(JSON.parse(readFileSync(join(repoRoot, LEDGER_RELATIVE_PATH), 'utf8')), 'ledger');
  const measured = scan(repoRoot, BASE_CONFIG);
  assert.deepEqual(measured.problems, []);
  assert.deepEqual(new Map(measured.rows.map(row => [`${row.kind}\0${row.path}\0${row.subject}`, row.count])), new Map([...ledger.pending, ...ledger.allowlist]));
  assert.deepEqual(measured.fixtures.map(file => file.path).sort(), [...ledger.fixtures.keys()].sort());
});
