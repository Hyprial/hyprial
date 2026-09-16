#!/usr/bin/env node
// A real browser/DSH gate. Never reuse an operator's DSH installation or profile.
import { inspectGuiSource } from './gui-source.mjs';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { copyFile, mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:net';
import { parse, stringify } from 'yaml';
import { verifyGuiLayoutProfile } from './gui-layout-package.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
assert.ok(args.length === 0 || (args.length === 3 && args[0] === '--runtime' && args[2] === '--release'),
  'Usage: verify-dsh-latest.mjs [--runtime candidate-directory --release]');
const released = args.length > 0;
const releaseDirectory = join(root, 'packages/dsh-runtime');
const stage = await mkdtemp(join(tmpdir(), 'gui-dsh-latest-'));
const artifacts = resolve(process.env.DSH_LATEST_ARTIFACTS || join(root, '.ci-cache/dsh-latest'));
await mkdir(artifacts, { recursive: true });
const registry = 'https://registry.npmjs.org';
const inherited = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  ['PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR', 'PNPM_HOME', 'XDG_RUNTIME_DIR',
    'PLAYWRIGHT_BROWSERS_PATH', 'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'SSL_CERT_DIR'].includes(key) ||
  /^(https?|all|no)_proxy$/i.test(key)));
const env = { ...inherited, DSH_HOME: join(stage, 'dsh'), H2B_HOME: join(stage, 'h2b'),
  HARNESS_STATE_DIR: join(stage, 'h2b/state'), HARNESS_SOCKET_PATH: join(stage, 'offline.sock'),
  H2B_DSH_DEMO_LEDGER: join(stage, 'h2b/state/dsh-web-injected.json'),
  H2B_GUI_STUDIO_DIR: join(stage, 'studio'), TASKDATA: join(stage, 'tasks'),
  HYPRIAL_HOME: join(stage, 'h2b'),
  XDG_CONFIG_HOME: join(stage, 'config'), XDG_CACHE_HOME: join(stage, 'cache'), XDG_DATA_HOME: join(stage, 'data'),
  npm_config_cache: join(stage, 'npm-cache'), npm_config_userconfig: join(stage, 'empty-npmrc'),
  npm_config_registry: registry };
await writeFile(env.npm_config_userconfig, '');
await mkdir(env.HARNESS_STATE_DIR, { recursive: true });
const report = { schema: 'h2b.dsh-latest-check/v1', status: 'failed', tag: released ? 'release' : 'latest', registry,
  checkedAt: new Date().toISOString(), checks: [], browserErrors: [], failedAssets: [], fixtureReads: [], failedRequests: [], businessTransport: 'offline-read-fixtures' };
let server, browser, bootLog = '';
function run(command, args, timeout = 600000) {
  const result = spawnSync(command, args, { cwd: root, env, encoding: 'utf8', timeout, maxBuffer: 16 * 1024 * 1024 });
  if (result.error || result.status !== 0) throw new Error(`${command} ${args[0]} failed: ${result.error?.message || result.stderr || result.stdout}`);
  return result.stdout.trim();
}
function latest() {
  return JSON.parse(run('npm', ['view', '@deepseek-ai/dsh', 'dist-tags.latest', '--json', '--registry=' + registry], 60000));
}
const sanitize = text => String(text).replace(/([?&]token=)[^\s"&]+/g, '$1[redacted]');
try {
  const source = inspectGuiSource(root);
  report.commit = source.commit;
  report.sourceKind = source.kind;
  report.sourceVersion = source.version;
  report.workingTreeClean = source.workingTreeClean;
  report.dshVersion = JSON.parse(await readFile(join(releaseDirectory, 'package.json'), 'utf8')).dependencies['@deepseek-ai/dsh'];
  assert.match(report.dshVersion, /^\d+\.\d+\.\d+(?:-[\w.]+)?$/);
  if (!released) assert.equal(latest(), report.dshVersion, 'Release pin is not npm latest; update packages/dsh-runtime and retest before publishing');
  console.log(`DSH_LATEST resolved=${report.dshVersion}; isolated profile; no model requests`);
  const runtime = args.length ? resolve(args[1]) : join(stage, 'runtime');
  if (!released) {
    await mkdir(runtime, { recursive: true });
    for (const file of ['package.json', 'package-lock.json']) await copyFile(join(releaseDirectory, file), join(runtime, file));
    run('npm', ['ci', '--prefix', runtime, '--registry=' + registry, '--no-audit', '--no-fund']);
  }
  const locked = await readFile(join(releaseDirectory, 'package-lock.json'), 'utf8');
  assert.equal(await readFile(join(runtime, 'package-lock.json'), 'utf8'), locked, 'Candidate runtime differs from the GUI release dependency lock');
  report.runtimeLockSha256 = createHash('sha256').update(locked).digest('hex');
  const { applyDshHistoryCompatibility } = await import('./dsh-history-compat.mjs');
  const { verifyDshHistoryCompatibility } = await import('./verify-dsh-history.mjs');
  report.historyCompatibility = applyDshHistoryCompatibility(runtime);
  report.historyRegression = await verifyDshHistoryCompatibility(runtime);
  report.checks.push('audited-subagent-history-compatibility');
  const { applyDshGuiFocusCompatibility } = await import('./dsh-gui-focus-compat.mjs');
  const { verifyDshGuiFocusCompatibility } = await import('./verify-dsh-gui-focus.mjs');
  report.guiFocusCompatibility = applyDshGuiFocusCompatibility(runtime);
  report.guiFocusRegression = await verifyDshGuiFocusCompatibility(runtime);
  report.checks.push('audited-native-gui-focus-context');
  env.PATH = join(runtime, 'node_modules/.bin') + ':' + env.PATH;
  assert.equal(run('dsh', ['--version']), report.dshVersion);
  console.log('DSH_LATEST runtime installed; registering shipped plugins');
  // Caret dependencies can resolve beyond the CLI's own version. Preserve the
  // actual graph as evidence rather than claiming every package has that version.
  await writeFile(join(artifacts, 'dependencies.json'), run('npm', ['list', '--prefix', runtime, '--all', '--json']) + '\n');
  run(process.execPath, ['scripts/codex-package.mjs', 'install']);
  run(process.execPath, ['scripts/gui-layout-package.mjs', 'install']);
  run('dsh', ['plugin', '--profile', 'web', 'add', '--ignore-scripts', root]);
  report.checks.push('fresh-profile-plugin-install');
  // This headless gate must use the browser picker even on an attended Mac.
  // Pin both sides of the real picker in this disposable profile only; never
  // open an OS dialog on the operator's desktop or fake directory selection.
  const pickerPatchPath = join(env.DSH_HOME, 'profiles/web/cordis.patch.yml');
  const pickerPatches = parse(await readFile(pickerPatchPath, 'utf8'));
  assert.ok(Array.isArray(pickerPatches), 'fresh profile must contain a YAML patch list');
  pickerPatches.push(
    { id: 'directory-picker', disabled: true },
    { insert: [
      { id: 'gate-directory-picker-host', name: '@deepseek-ai/dsh-host-directory-picker-browse' },
      { id: 'gate-directory-picker-client', name: '@deepseek-ai/dsh-client-ui-directory-picker-browse' },
    ] },
  );
  await writeFile(pickerPatchPath, stringify(pickerPatches));
  report.checks.push('headless-browser-directory-picker');
  verifyGuiLayoutProfile(run('dsh', ['--profile', 'web', '--dump-config']));
  report.checks.push('composed-profile');
  const reservation = createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  env.H2B_GUI_LAUNCH_INFO_FILE = join(stage, 'launch.json');
  env.H2B_GUI_EXPECTED_ORIGIN = `http://127.0.0.1:${port}`;
  server = spawn(process.execPath, [join(root, 'scripts/launch-dsh.mjs'), '--profile', 'web', '--host', '127.0.0.1', '--port', String(port), '--no-open'],
    { cwd: stage, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  let bootError;
  server.on('error', error => { bootError = error; });
  for (const stream of [server.stdout, server.stderr]) stream.on('data', data => { bootLog = (bootLog + data).slice(-1000000); });
  let endpoint;
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    if (bootError) throw bootError;
    if (server.exitCode !== null) throw new Error('DSH exited during startup');
    try { endpoint = JSON.parse(await readFile(env.H2B_GUI_LAUNCH_INFO_FILE, 'utf8')).url; } catch {}
    if (endpoint) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  assert.ok(endpoint, 'DSH did not hand off a listening URL');
  const ready = await fetch(endpoint, { redirect: 'manual' });
  assert.ok([200, 303].includes(ready.status), 'Managed launch URL must serve the app or exchange its token');
  if (ready.status === 303) {
    assert.ok(ready.headers.get('set-cookie'), 'Token exchange must mint a browser cookie');
    assert.equal((await fetch(new URL(ready.headers.get('location'), endpoint), { headers: { cookie: ready.headers.get('set-cookie').split(';')[0] } })).status, 200);
  }
  report.checks.push('managed-authenticated-launch-readiness');
  run(process.execPath, ['scripts/verify-codex-models.mjs', join(env.DSH_HOME, 'profiles/web')]);
  report.checks.push('codex-model-catalog-and-resolution');
  const { chromium } = createRequire(join(root, 'dashboard/package.json'))('playwright');
  browser = await chromium.launch({ env });
  const responses = [];
  async function openPage() {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(20000);
    page.on('pageerror', error => report.browserErrors.push(sanitize(error.message)));
    page.on('console', message => {
      // Network failures are validated below with their exact request and body;
      // Chromium's generic resource error contains neither, and is not a JS error.
      if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) report.browserErrors.push(sanitize(message.text()));
    });
    // The smoke has no daemon. Fixture only these business reads; native DSH
    // APIs, plugin assets, session creation and GUI Studio still hit the server.
    // Never turn an arbitrary Host error into a passing compatibility result.
    await page.route('**/plugins/h2b-talk/rpc', async route => {
      const input = route.request().postDataJSON();
      let value;
      if (input?.method === 'h2b-demo-rpc' && input.args?.operation === 'chat-list') value = { ok: true, chats: [] };
      if (input?.method === 'h2b-targets') value = { ok: true, targets: [] };
      const overviewFixtures = {
        version: { version: 'isolated-fixture' },
        processes: { zenoh: { listen: [], connect: [] } },
        topology: { actors: [], quota: { sources: [] } },
        doctor: { checks: [] },
      };
      if (input?.method === 'h2b-control-query' && Object.hasOwn(overviewFixtures, input.args?.operation)) {
        value = { ok: true, document: overviewFixtures[input.args.operation] };
      }
      if (!value) return route.continue();
      report.fixtureReads.push({ method: input.method, operation: input.args?.operation });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, value }) });
    });
    page.on('response', response => {
      const path = new URL(response.url()).pathname;
      if (response.status() >= 400 && /\.(js|css)(?:$|\?)/.test(path)) report.failedAssets.push({ path, status: response.status() });
      if (response.status() >= 400) responses.push((async () => {
        const request = response.request();
        let input;
        try { input = request.postDataJSON(); } catch {}
        const body = await response.json().catch(() => null);
        report.failedRequests.push({ path, status: response.status(),
          method: input?.method, operation: input?.args?.operation, code: body?.error?.code });
      })().catch(error => report.failedRequests.push({ path, status: response.status(), error: sanitize(error.message) })));
    });
    return page;
  }
  let page = await openPage();
  await page.goto(endpoint, { waitUntil: 'domcontentloaded' });
  // Fail on plugin boot errors directly, without waiting for a selector timeout.
  await page.waitForFunction(() => document.querySelector('[data-gui-action="workspace-menu"]') || /Failed to load plugins/.test(document.body.innerText));
  const body = await page.locator('body').innerText();
  assert.ok(!body.includes('Failed to load plugins'), sanitize(body));
  await page.locator('[data-gui-action="workspace-menu"]').waitFor({ state: 'visible' });
  for (let i = 0; i < 12; i++) {
    for (const name of [/^(Continue|继续)$/, /^(Configure later|稍后配置)$/]) {
      const button = page.getByRole('button', { name });
      if (await button.isVisible().catch(() => false)) await button.click();
    }
    await page.waitForTimeout(200);
  }
  assert.equal(await page.locator('[data-gui-layout="1"]').count(), 1, 'exactly one GUI layout must mount');
  const modelWorkspace = join(stage, 'model-catalog-workspace');
  await mkdir(modelWorkspace);
  await page.getByRole('button', { name: /^(New session|New Session|新建会话|新会话)$/ }).first().click();
  await page.getByRole('button', { name: /^(Choose workspace|选择工作区)$/ }).click();
  // A fresh profile opens the directory picker directly for its first workspace.
  await page.getByRole('button', { name: /^(Edit path|编辑路径)$/ }).click();
  const pathInput = page.getByRole('textbox', { name: /^(Edit path|编辑路径)$/ });
  await pathInput.fill(modelWorkspace);
  await pathInput.press('Enter');
  await page.getByRole('button', { name: /^(Open|打开)$/ }).click();
  await page.getByRole('button', { name: /^(Select model|选择模型)/ }).click();
  const modelMenu = page.getByRole('menu', { name: /^(Model and reasoning effort|模型与推理等级)$/ });
  await modelMenu.waitFor();
  const modelPane = modelMenu.getByRole('menuitem', { name: /^(Model|模型)/ });
  if (await modelPane.count()) await modelPane.click();
  await modelMenu.getByRole('menuitemradio', { name: /GPT-6 Astra/i }).waitFor();
  assert.ok(!/加载失败|failed to load|Cannot read properties/i.test(await modelMenu.innerText()));
  await page.keyboard.press('Escape');
  report.checks.push('codex-browser-model-menu');
  for (const name of ['消息', '通讯录', '任务', '运维', '消息']) {
    await page.getByRole('button', { name, exact: true }).click();
    await page.waitForTimeout(250);
  }
  const profile = await page.evaluate(async () => {
    const response = await fetch('/plugins/h2b-talk/rpc', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ method: 'h2b-gui-studio', args: { operation: 'profile' } }) });
    return { status: response.status, body: await response.json() };
  });
  assert.equal(profile.status, 200);
  assert.equal(profile.body.ok, true, 'GUI Host RPC must be usable');
  assert.equal(await page.locator('[data-gui-action="studio"]').count(), 0, 'Design controls stay inside the closed workspace menu');
  await page.locator('[data-gui-action="workspace-menu"]').click();
  await page.getByRole('region', { name: '工作空间菜单', exact: true }).waitFor();
  await page.locator('[data-gui-action="studio"]').click();
  await page.getByRole('dialog', { name: 'GUI 设计工作台' }).waitFor();
  await page.waitForTimeout(1500);
  await Promise.all(responses);
  const sessionIds = async () => (await readdir(join(env.DSH_HOME, 'sessions'), { recursive: true, withFileTypes: true }))
    .filter(entry => entry.isDirectory() && entry.name.startsWith('session-')).map(entry => entry.name).sort();
  const beforeReload = await sessionIds();
  assert.ok(beforeReload.length >= 2, 'Navigation must exercise actual persisted utility sessions');
  // A genuinely cold browser has neither remembered IDs nor authentication
  // cookies. Close the old connection before exchanging the token again.
  await page.context().close();
  page = await openPage();
  await page.goto(endpoint, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-gui-action="workspace-menu"]').waitFor({ state: 'visible' });
  await page.waitForTimeout(1500);
  for (const name of ['通讯录', '运维', '任务', '消息']) {
    await page.getByRole('button', { name, exact: true }).click();
    await page.waitForTimeout(250);
  }
  assert.deepEqual(await sessionIds(), beforeReload, 'A cold browser must not create more utility sessions');
  report.checks.push('cold-browser-utility-session-reuse');
  report.checks.push('real-browser-layout-navigation-studio-host-rpc');
  await Promise.all(responses);
  assert.deepEqual(report.failedAssets, []);
  assert.deepEqual(report.failedRequests, []);
  assert.deepEqual(report.browserErrors, []);
  await page.screenshot({ path: join(artifacts, 'gui.png'), fullPage: true });
  if (!released) assert.equal(latest(), report.dshVersion, 'npm latest moved during testing; update the release pin and rerun');
  report.status = 'passed';
} catch (error) {
  const failedPage = browser?.contexts().flatMap(context => context.pages())[0];
  await failedPage?.screenshot({ path: join(artifacts, 'failure.png'), fullPage: true }).catch(() => {});
  report.error = sanitize(error.stack || error.message);
  console.error(report.error);
  process.exitCode = 1;
} finally {
  await browser?.close();
  if (server?.pid) {
    try { process.kill(-server.pid, 'SIGTERM'); } catch {}
    await Promise.race([new Promise(resolve => server.once('close', resolve)), new Promise(resolve => setTimeout(resolve, 2000))]);
    try { process.kill(-server.pid, 'SIGKILL'); } catch {}
  }
  await writeFile(join(artifacts, 'boot.log'), sanitize(bootLog));
  await writeFile(join(artifacts, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(`DSH_LATEST status=${report.status} report=${join(artifacts, 'report.json')}`);
}
