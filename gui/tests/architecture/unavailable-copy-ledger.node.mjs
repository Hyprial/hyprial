import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test, { after } from 'node:test';

const script = await import('../../../scripts/ci/check_gui_unavailable_copy.mjs');
const LEDGER = script.LEDGER_RELATIVE_PATH;
const SCAN = script.SCAN_CONFIG_RELATIVE_PATH;
const SCAN_TEST = script.SCAN_TEST_RELATIVE_PATH;
const REAL_ROOT = new URL('../../..', import.meta.url).pathname;
const REAL_SCAN = readFileSync(join(REAL_ROOT, SCAN), 'utf8');
const REAL_SCAN_TEST = readFileSync(join(REAL_ROOT, SCAN_TEST), 'utf8');
const temporaryRoots = [];

after(() => {
  for (const root of temporaryRoots) rmSync(root, { recursive: true, force: true });
});

function ledger(parts = {}) {
  const maps = {
    STATE: {}, NOTIFICATION: {}, ELSEWHERE: {}, LOGIC_NOT_COPY: {},
    PENDING_SHELL: {}, PENDING_AGENTS: {}, PENDING_ORG: {}, PENDING_CHAT: {},
    PENDING_CONNECTIONS: {}, PENDING_SETTINGS: {}, PENDING_REMAINDER: {},
    PENDING_DISABLED: {}, PENDING_COMPONENTS: {},
    ...parts,
  };
  return Object.entries(maps)
    .map(([name, value]) => `export const ${name}: Readonly<Record<string, unknown>> = ${JSON.stringify(value)};`)
    .join('\n');
}

function git(root, ...args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

function repo(baseLedger, baseScanner = true) {
  const root = mkdtempSync(join(tmpdir(), 'gui-unavailable-copy-'));
  temporaryRoots.push(root);
  git(root, 'init', '-q', '-b', 'dev');
  git(root, 'config', 'user.email', 'ci@example.invalid');
  git(root, 'config', 'user.name', 'ci');
  mkdirSync(join(root, 'gui/tests/architecture'), { recursive: true });
  writeFileSync(join(root, 'README.md'), 'x\n');
  if (baseLedger !== null) writeFileSync(join(root, LEDGER), baseLedger);
  if (baseScanner) {
    writeFileSync(join(root, SCAN), REAL_SCAN);
    writeFileSync(join(root, SCAN_TEST), REAL_SCAN_TEST);
  }
  git(root, 'add', '-A');
  git(root, 'commit', '-q', '-m', 'base');
  return root;
}

function setLedger(root, text) {
  writeFileSync(join(root, LEDGER), text);
  writeFileSync(join(root, SCAN), readIfPresent(join(root, SCAN)) ?? REAL_SCAN);
  writeFileSync(join(root, SCAN_TEST), readIfPresent(join(root, SCAN_TEST)) ?? REAL_SCAN_TEST);
}

function readIfPresent(path) {
  try {
    return readFileSync(path, 'utf8');
  } catch {
    return null;
  }
}

function editFile(root, relative, from, to) {
  const path = join(root, relative);
  const text = readFileSync(path, 'utf8');
  assert.ok(text.includes(from), `fixture text not found: ${from}`);
  writeFileSync(path, text.replace(from, to));
}

async function run(root, options = { baseRef: 'HEAD', baseBranch: 'dev' }) {
  return script.check(root, options);
}

const BASE = ledger({
  STATE: { 'a.tsx': ['状态不可用'] },
  PENDING_AGENTS: { 'b.tsx': ['创建暂不可用'] },
  PENDING_DISABLED: { 'b.tsx': 2 },
  PENDING_COMPONENTS: { 'b.tsx': 1 },
});

test('an unchanged ledger passes against its base', async () => {
  const root = repo(BASE);
  const report = await run(root);
  assert.equal(report.status, 'PASS');
  assert.equal(report.firstSeed, false);
});

test('shrinking every bucket passes', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({ STATE: {}, PENDING_AGENTS: {}, PENDING_DISABLED: { 'b.tsx': 1 }, PENDING_COMPONENTS: {} }));
  assert.equal((await run(root)).status, 'PASS');
});

test('a new banned string registered together with its entry in a permanent bucket fails', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({
    STATE: { 'a.tsx': ['状态不可用'], 'c.tsx': ['功能尚未开放'] },
    PENDING_AGENTS: { 'b.tsx': ['创建暂不可用'] },
    PENDING_DISABLED: { 'b.tsx': 2 },
    PENDING_COMPONENTS: { 'b.tsx': 1 },
  }));
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.deepEqual(report.violations.map(row => row.kind), ['permanent-copy-grew']);
  assert.equal(report.violations[0].path, 'c.tsx');
});

for (const name of ['NOTIFICATION', 'ELSEWHERE', 'LOGIC_NOT_COPY']) {
  test(`growing ${name} fails`, async () => {
    const root = repo(BASE);
    setLedger(root, ledger({
      STATE: { 'a.tsx': ['状态不可用'] },
      [name]: { 'c.tsx': ['新文案暂不可用'] },
      PENDING_AGENTS: { 'b.tsx': ['创建暂不可用'] },
      PENDING_DISABLED: { 'b.tsx': 2 },
      PENDING_COMPONENTS: { 'b.tsx': 1 },
    }));
    const report = await run(root);
    assert.equal(report.status, 'FAIL');
    assert.equal(report.violations[0].kind, 'permanent-copy-grew');
  });
}

test('adding a string to a pending copy map fails, including the emptied shell map', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({
    STATE: { 'a.tsx': ['状态不可用'] },
    PENDING_SHELL: { 'Rail.tsx': ['帮助中心暂不可用。'] },
    PENDING_AGENTS: { 'b.tsx': ['创建暂不可用'] },
    PENDING_DISABLED: { 'b.tsx': 2 },
    PENDING_COMPONENTS: { 'b.tsx': 1 },
  }));
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.deepEqual(report.violations.map(row => row.kind), ['pending-copy-grew']);
});

test('a duplicated entry raises the count and fails', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({
    STATE: { 'a.tsx': ['状态不可用'] },
    PENDING_AGENTS: { 'b.tsx': ['创建暂不可用', '创建暂不可用'] },
    PENDING_DISABLED: { 'b.tsx': 2 },
    PENDING_COMPONENTS: { 'b.tsx': 1 },
  }));
  assert.equal((await run(root)).status, 'FAIL');
});

test('a higher always-disabled or placeholder count, or a new key, fails', async () => {
  for (const [disabled, components, kind] of [
    [{ 'b.tsx': 3 }, { 'b.tsx': 1 }, 'PENDING_DISABLED'],
    [{ 'b.tsx': 2, 'z.tsx': 1 }, { 'b.tsx': 1 }, 'PENDING_DISABLED'],
    [{ 'b.tsx': 2 }, { 'b.tsx': 2 }, 'PENDING_COMPONENTS'],
    [{ 'b.tsx': 2 }, { 'b.tsx': 1, 'z.tsx': 1 }, 'PENDING_COMPONENTS'],
  ]) {
    const root = repo(BASE);
    setLedger(root, ledger({
      STATE: { 'a.tsx': ['状态不可用'] },
      PENDING_AGENTS: { 'b.tsx': ['创建暂不可用'] },
      PENDING_DISABLED: disabled,
      PENDING_COMPONENTS: components,
    }));
    const report = await run(root);
    assert.equal(report.status, 'FAIL', kind);
    assert.deepEqual(report.violations.map(row => [row.kind, row.map]), [['count-grew', kind]]);
  }
});

test('re-bucketing a pending string into a permanent bucket is legal', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({
    STATE: { 'a.tsx': ['状态不可用'] },
    NOTIFICATION: { 'b.tsx': ['创建暂不可用'] },
    PENDING_DISABLED: { 'b.tsx': 2 },
    PENDING_COMPONENTS: { 'b.tsx': 1 },
  }));
  assert.equal((await run(root)).status, 'PASS');
});

test('moving a string between pending maps or between permanent maps is legal', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({
    NOTIFICATION: { 'a.tsx': ['状态不可用'] },
    PENDING_ORG: { 'b.tsx': ['创建暂不可用'] },
    PENDING_DISABLED: { 'b.tsx': 2 },
    PENDING_COMPONENTS: { 'b.tsx': 1 },
  }));
  assert.equal((await run(root)).status, 'PASS');
});

test('re-bucketing does not excuse a second copy of the string or a new path', async () => {
  const root = repo(BASE);
  setLedger(root, ledger({
    STATE: { 'a.tsx': ['状态不可用'] },
    NOTIFICATION: { 'b.tsx': ['创建暂不可用'], 'c.tsx': ['创建暂不可用'] },
    PENDING_DISABLED: { 'b.tsx': 2 },
    PENDING_COMPONENTS: { 'b.tsx': 1 },
  }));
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.equal(report.violations[0].path, 'c.tsx');
});

test('an unknown ledger export fails so a new bucket cannot hide entries', async () => {
  const root = repo(BASE);
  setLedger(root, `${BASE}\nexport const EXTRA: Readonly<Record<string, unknown>> = {};`);
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.equal(report.violations[0].kind, 'invalid-ledger');
});

test('a base without the ledger is a first seed on dev and fails elsewhere', async () => {
  const root = repo(null);
  setLedger(root, BASE);
  const seeded = await run(root);
  assert.equal(seeded.status, 'PASS');
  assert.equal(seeded.firstSeed, true);
  const release = await run(root, { baseRef: 'HEAD', baseBranch: 'release/1.0' });
  assert.equal(release.status, 'FAIL');
  assert.equal(release.violations[0].kind, 'missing-base-ledger');
});

test('a requested base ref that does not resolve fails closed', async () => {
  const root = repo(BASE);
  const report = await run(root, { baseRef: 'origin/dev', baseBranch: 'dev' });
  assert.equal(report.status, 'FAIL');
  assert.equal(report.violations[0].kind, 'invalid-base-ref');
});

test('an empty base ref fails, including on the command line', async () => {
  const root = repo(BASE);
  const report = await run(root, { baseRef: '', baseBranch: 'dev' });
  assert.equal(report.status, 'FAIL');
  assert.equal(report.violations[0].kind, 'invalid-base-ref');
  assert.throws(
    () => execFileSync('node', [new URL('../../../scripts/ci/check_gui_unavailable_copy.mjs', import.meta.url).pathname, '--root', root, '--base-ref', ''], { stdio: 'pipe' }),
  );
});

test('a ledger with TypeScript syntax errors fails at HEAD and at the base', async () => {
  const headBroken = repo(BASE);
  setLedger(headBroken, `${BASE}\nexport const BROKEN = {{;`);
  const head = await run(headBroken);
  assert.equal(head.status, 'FAIL');
  assert.equal(head.violations[0].kind, 'invalid-ledger');
  const baseBroken = repo(`${BASE}\nexport const BROKEN = {{;`);
  setLedger(baseBroken, BASE);
  const base = await run(baseBroken);
  assert.equal(base.status, 'FAIL');
  assert.equal(base.violations[0].kind, 'invalid-base-ref');
  assert.match(base.violations[0].detail, /syntax errors/);
});

test('the committed ledger parses and passes without a base', async () => {
  const report = await script.check(new URL('../../..', import.meta.url).pathname);
  assert.equal(report.status, 'PASS');
  assert.equal(report.baseRef, null);
});

function scannerKinds(report) {
  return report.violations.map(row => row.kind);
}

test('removing a vocabulary term fails', async () => {
  const root = repo(BASE);
  editFile(root, SCAN, "  '开发中',\n", '');
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.deepEqual(scannerKinds(report), ['scanner-narrowed']);
  assert.deepEqual(report.violations[0].removed, ['开发中']);
});

test('removing an English term or a scanned extension fails', async () => {
  for (const [from, to, name] of [
    ["['unavailable', 'unsupported']", "['unavailable']", 'ENGLISH_TERMS'],
    ["'.mjs', ", '', 'SOURCE_EXTENSIONS'],
  ]) {
    const root = repo(BASE);
    editFile(root, SCAN, from, to);
    const report = await run(root);
    assert.equal(report.status, 'FAIL', name);
    assert.equal(report.violations[0].name, name);
  }
});

test('adding an exclusion fails: a skipped file, a skipped directory or a changed machine token', async () => {
  for (const [from, to, kind] of [
    ["'client/generated/types.ts']", "'client/generated/types.ts', 'client/generated/new.ts']", 'scanner-exclusion-added'],
    ['SKIPPED_DIRECTORIES = [] as const', "SKIPPED_DIRECTORIES = ['dev'] as const", 'scanner-exclusion-added'],
    ["'^[a-z0-9]+", "'^[a-zA-Z0-9 ]+", 'scanner-constant-changed'],
    ["'(?![A-Za-z0-9_])'", "'(?![a-z])'", 'scanner-constant-changed'],
  ]) {
    const root = repo(BASE);
    editFile(root, SCAN, from, to);
    const report = await run(root);
    assert.equal(report.status, 'FAIL', to);
    assert.deepEqual(scannerKinds(report), [kind], to);
  }
});

test('removing a skipped file passes', async () => {
  const root = repo(BASE);
  editFile(root, SCAN, "['client/generated/index.ts', 'client/generated/types.ts']", "['client/generated/index.ts']");
  assert.equal((await run(root)).status, 'PASS');
});

test('changing the scan root fails', async () => {
  const root = repo(BASE);
  editFile(root, SCAN, "SCAN_ROOT = 'src'", "SCAN_ROOT = 'src/features'");
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.deepEqual(scannerKinds(report), ['scanner-constant-changed']);
  assert.equal(report.violations[0].name, 'SCAN_ROOT');
});

test('retargeting the ledger import, the scan import or a constant fails', async () => {
  const retargeted = repo(BASE);
  editFile(retargeted, SCAN, "from './unavailable-copy.allow'", "from './other-ledger'");
  const first = await run(retargeted);
  assert.equal(first.status, 'FAIL');
  assert.deepEqual(scannerKinds(first), ['scanner-import-changed']);
  assert.equal(first.violations[0].file, 'scan');
  const testRetargeted = repo(BASE);
  editFile(testRetargeted, SCAN_TEST, "from './unavailable-copy.scan'", "from './forked-scan'");
  const second = await run(testRetargeted);
  assert.equal(second.status, 'FAIL');
  assert.equal(second.violations[0].file, 'test');
  const removed = repo(BASE);
  editFile(removed, SCAN, "export const LEDGER = ledger;", '');
  assert.equal((await run(removed)).violations[0].kind, 'scanner-constant-removed');
});

test('an unknown scanner constant fails', async () => {
  const root = repo(BASE);
  writeFileSync(join(root, SCAN), `${REAL_SCAN}\nexport const EXTRA_SKIP = ['x'] as const;\n`);
  assert.deepEqual(scannerKinds(await run(root)), ['unknown-scanner-constant']);
});

test('adding a vocabulary term, an English term or an extension passes', async () => {
  const root = repo(BASE);
  editFile(root, SCAN, "  'coming soon',\n", "  'coming soon',\n  '即将上线',\n");
  editFile(root, SCAN, "['unavailable', 'unsupported']", "['unavailable', 'unsupported', 'unimplemented']");
  editFile(root, SCAN, "'.css']", "'.css', '.jsx']");
  assert.equal((await run(root)).status, 'PASS');
});

test('a narrowing change passes only when its digest equals the pinned reviewed digest', async () => {
  const root = repo(BASE);
  editFile(root, SCAN, "  '开发中',\n", '');
  const config = script.extractScannerConfig(readFileSync(join(root, SCAN), 'utf8'), readFileSync(join(root, SCAN_TEST), 'utf8'));
  const digest = script.scannerDigest(config);
  const pinned = await run(root, { baseRef: 'HEAD', baseBranch: 'dev', reviewedScannerSha256: digest });
  assert.equal(pinned.status, 'PASS');
  assert.equal(pinned.scannerOverride, true);
  const stale = await run(root, { baseRef: 'HEAD', baseBranch: 'dev', reviewedScannerSha256: '0'.repeat(64) });
  assert.equal(stale.status, 'FAIL');
  editFile(root, SCAN, "SCAN_ROOT = 'src'", "SCAN_ROOT = 'src/features'");
  const changedAfterReview = await run(root, { baseRef: 'HEAD', baseBranch: 'dev', reviewedScannerSha256: digest });
  assert.equal(changedAfterReview.status, 'FAIL');
});

test('the shipped pinned digest is empty until a reviewed scanner change needs it', () => {
  assert.equal(script.REVIEWED_SCANNER_SHA256, null);
});

test('a scanner file with TypeScript syntax errors fails', async () => {
  const root = repo(BASE);
  writeFileSync(join(root, SCAN), `${REAL_SCAN}\nexport const BROKEN = {{;\n`);
  const report = await run(root);
  assert.equal(report.status, 'FAIL');
  assert.equal(report.violations[0].kind, 'invalid-scanner-config');
});

test('a base without the scanner configuration is a scanner first seed', async () => {
  const root = repo(BASE, false);
  writeFileSync(join(root, SCAN), REAL_SCAN);
  writeFileSync(join(root, SCAN_TEST), REAL_SCAN_TEST);
  assert.equal((await run(root)).status, 'PASS');
});

test('the committed scanner configuration digest is printed by the command line', () => {
  const printed = execFileSync('node', [new URL('../../../scripts/ci/check_gui_unavailable_copy.mjs', import.meta.url).pathname, '--print-scanner-digest'], { encoding: 'utf8' }).trim();
  assert.match(printed, /^[0-9a-f]{64}$/);
});
