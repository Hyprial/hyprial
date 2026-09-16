import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { changedPaths, classify, select } from './ci-scope.mjs';

for (const [name, paths, project, expected] of [
  ['monorepo GUI UI', ['src/gui/dashboard/src/App.jsx'], 'gui', [false, true]],
  ['monorepo GUI documentation', ['src/gui/README.md'], 'gui', [false, false]],
  ['monorepo GUI scope workflow', ['.forgejo/workflows/gui.yml'], 'gui', [true, true]],
  ['shared daemon input', ['src/hyprial/daemon.py'], 'gui', [true, true]],
  ['release builder', ['scripts/build_gui_release.py'], 'gui', [true, true]],
  ['README script is code', ['README.py'], 'h2b', [true, false]],
  ['translated README', ['README.zh-CN.md'], 'gui', [false, false]],
  ['README only', ['README.md'], 'gui', [false, false]],
  ['sidecar documentation', ['sidecar/h2b-tsnet/README.md'], 'h2b', [false, false]],
  ['guide only', ['docs/team-feature-guide.md'], 'gui', [false, false]],
  ['H2B docs', ['README.md', 'notes/design.md'], 'h2b', [false, false]],
  ['empty change', [], 'gui', [false, false]],
  ['DSH client', ['imskin-plugin.js', 'static/client.js'], 'gui', [true, false]],
  ['GUI browser regression', ['tests/gui-contact-layout.browser.mjs'], 'gui', [true, true]],
  ['GUI Studio UI', ['client/gui-studio.inc.js'], 'gui', [true, true]],
  ['GUI layout provider', ['packages/gui-layout/workspace-extension.js'], 'gui', [true, true]],
  ['Dashboard UI', ['dashboard/src/App.jsx'], 'gui', [false, true]],
  ['Dashboard server needs Node tests too', ['dashboard/server.mjs'], 'gui', [true, true]],
  ['Dashboard fixture shared with Node', ['dashboard/tests/fixtures.mjs'], 'gui', [true, true]],
  ['Dashboard Node test', ['tests/dashboard.test.mjs'], 'gui', [true, true]],
  ['root lockfile', ['package-lock.json'], 'gui', [true, true]],
  ['Dashboard lockfile', ['dashboard/package-lock.json'], 'gui', [true, true]],
  ['launcher', ['scripts/start-gui.sh'], 'gui', [true, true]],
  ['workflow', ['.forgejo/workflows/tests.yml'], 'gui', [true, true]],
  ['unknown input', ['new-component/config.json'], 'gui', [true, true]],
  ['mixed docs and UI', ['README.md', 'dashboard/src/App.jsx'], 'gui', [false, true]],
  ['both UIs', ['client/workflow.js', 'dashboard/src/App.jsx'], 'gui', [true, true]],
  ['H2B source', ['src/h2b/gui_apps.py'], 'h2b', [true, false]],
  ['runtime Markdown', ['src/h2b/skills/h2b-ops/SKILL.md'], 'h2b', [true, false]],
  ['runtime doc template', ['docs/squire/SKILL-template.md'], 'h2b', [true, false]],
  ['contract doc input', ['docs/cutover-runbook.md'], 'h2b', [true, false]],
  ['script under docs is not prose', ['docs/example.py'], 'h2b', [true, false]],
]) test(name, () => {
  const result = classify(paths, project);
  assert.deepEqual([result.unit, result.dashboard], expected);
});

function repository(t) {
  const cwd = mkdtempSync(join(tmpdir(), 'ci-scope-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const git = (...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git('init', '-b', 'main');
  git('config', 'user.email', 'ci-test@example.invalid');
  git('config', 'user.name', 'CI test');
  const commit = (path, content) => {
    mkdirSync(join(cwd, path, '..'), { recursive: true });
    writeFileSync(join(cwd, path), content);
    git('add', '-A'); git('commit', '-m', 'fixture');
    return git('rev-parse', 'HEAD');
  };
  return { cwd, git, commit };
}

test('PR diff uses merge base and ignores changes made only on target branch', t => {
  const r = repository(t);
  r.commit('README.md', 'base');
  r.git('checkout', '-b', 'pr');
  const head = r.commit('README.md', 'docs update');
  r.git('checkout', 'main');
  const base = r.commit('production.js', 'target change');
  const event = { pull_request: { base: { sha: base }, head: { sha: head } } };
  assert.deepEqual(changedPaths(event, 'pull_request', r.cwd, false), ['README.md']);
  assert.equal(select(event, 'pull_request', 'gui', r.cwd, false).unit, false);
});

test('multi-commit push retains earlier code even when final commit only edits docs', t => {
  const r = repository(t);
  const before = r.commit('README.md', 'base');
  r.commit('production.js', 'code');
  const after = r.commit('README.md', 'docs');
  assert.equal(select({ before, after }, 'push', 'gui', r.cwd, false).dashboard, true);
});

test('GUI main releases cannot skip full gates on documentation-only changes', t => {
  const r = repository(t);
  const before = r.commit('README.md', 'base');
  const after = r.commit('README.md', 'docs');
  assert.deepEqual(select({ before, after, ref: 'refs/heads/main' }, 'push', 'gui', r.cwd, false),
    { unit: true, dashboard: true, reason: 'main/dev is a release candidate; run all checks' });
  assert.equal(select({ before, after, ref: 'refs/heads/main' }, 'push', 'h2b', r.cwd, false).unit, false);
});

test('rename from code to docs and deletion both retain code impact', t => {
  const r = repository(t);
  const before = r.commit('production.js', 'code');
  renameSync(join(r.cwd, 'production.js'), join(r.cwd, 'README.md'));
  r.git('add', '-A'); r.git('commit', '-m', 'rename');
  const after = r.git('rev-parse', 'HEAD');
  const paths = changedPaths({ before, after }, 'push', r.cwd, false);
  assert.deepEqual(paths.sort(), ['README.md', 'production.js']);
  assert.equal(classify(paths, 'gui').unit, true);
});

test('newline filenames are not split into separate paths', t => {
  const r = repository(t);
  const before = r.commit('README.md', 'base');
  const after = r.commit('code\nREADME.md', 'code');
  assert.deepEqual(changedPaths({ before, after }, 'push', r.cwd, false), ['code\nREADME.md']);
});

for (const [name, event, eventName] of [
  ['manual dispatch', {}, 'workflow_dispatch'],
  ['missing push payload', {}, 'push'],
  ['new branch', { before: '0'.repeat(40), after: 'a'.repeat(40) }, 'push'],
  ['missing commit objects', { before: 'b'.repeat(40), after: 'a'.repeat(40) }, 'push'],
  ['invalid revision argument', { before: '--help', after: 'a'.repeat(40) }, 'push'],
]) test(`${name} runs all checks`, () => {
  const result = select(event, eventName, 'gui', process.cwd(), false);
  assert.equal(result.unit, true); assert.equal(result.dashboard, true);
});

test('CLI emits explicit false outputs for documentation-only push', t => {
  const r = repository(t);
  const before = r.commit('README.md', 'base');
  const after = r.commit('README.md', 'updated');
  r.git('remote', 'add', 'origin', r.cwd);
  const payload = join(r.cwd, 'event.json'), output = join(r.cwd, 'output'), summary = join(r.cwd, 'summary');
  writeFileSync(payload, JSON.stringify({ before, after }));
  execFileSync(process.execPath, [new URL('./ci-scope.mjs', import.meta.url).pathname, 'gui'], {
    cwd: r.cwd, env: { ...process.env, GITHUB_EVENT_NAME: 'push', GITHUB_EVENT_PATH: payload,
      GITHUB_OUTPUT: output, GITHUB_STEP_SUMMARY: summary },
  });
  assert.equal(readFileSync(output, 'utf8'), 'unit=false\ndashboard=false\n');
  assert.match(readFileSync(summary, 'utf8'), /documentation-only/);
});

test('monorepo dev pushes run full GUI gates', () => {
  assert.deepEqual(select({ ref: 'refs/heads/dev' }, 'push', 'gui', process.cwd(), false),
    { unit: true, dashboard: true, reason: 'main/dev is a release candidate; run all checks' });
});

test('GUI subdirectory diff retains backend changes and GUI package paths', t => {
  const r = repository(t);
  const before = r.commit('src/gui/README.md', 'base');
  r.commit('src/hyprial/daemon.py', 'backend');
  const after = r.commit('src/gui/dashboard/src/App.jsx', 'ui');
  const cwd = join(r.cwd, 'src/gui');
  assert.deepEqual(changedPaths({ before, after }, 'push', cwd, false).sort(),
    ['src/gui/dashboard/src/App.jsx', 'src/hyprial/daemon.py']);
  assert.equal(select({ before, after }, 'push', 'gui', cwd, false).unit, true);
});

const scopeKeys = ['python', 'gui_integration', 'unit', 'dashboard', 'codex', 'dsh', 'package'];
for (const [name, paths, expected] of [
  ['dashboard UI', ['src/gui/dashboard/src/main.jsx'], [false, true, false, true, false, false, true]],
  ['dashboard CSS and docs', ['src/gui/dashboard/src/style.css', 'docs/change.md'], [false, true, false, true, false, false, true]],
  ['client module', ['src/gui/client/gui-editor.inc.js'], [false, true, true, true, true, true, true]],
  ['generated client', ['src/gui/static/client.js'], [false, true, true, false, true, true, true]],
  ['ordinary client CSS', ['src/gui/client/workflow.css'], [false, true, true, false, true, true, true]],
  ['GUI prose', ['src/gui/README.md', 'docs/migration.md'], [false, false, false, false, false, false, false]],
  ['installer', ['src/gui/scripts/install-local.sh'], [true, true, true, true, true, true, true]],
  ['dashboard server', ['src/gui/dashboard/server.mjs'], [true, true, true, true, true, true, true]],
  ['client directory new config', ['src/gui/client/config.json'], [true, true, true, true, true, true, true]],
  ['unknown nested client', ['src/gui/client/new/server.js'], [true, true, true, true, true, true, true]],
  ['GUI provider', ['src/gui/packages/gui-layout/workspace-extension.js'], [true, true, true, true, true, true, true]],
  ['Python shared protocol', ['src/hyprial/protocol.py'], [true, true, true, true, true, true, true]],
  ['backend mixed with dashboard', ['src/hyprial/daemon.py', 'src/gui/dashboard/src/main.jsx'], [true, true, true, true, true, true, true]],
  ['workflow changes', ['.forgejo/workflows/gui.yml'], [true, true, true, true, true, true, true]],
  ['scope policy', ['src/gui/scripts/ci-scope.mjs'], [true, true, true, true, true, true, true]],
  ['root scope policy', ['scripts/ci-scope.mjs'], [true, true, true, true, true, true, true]],
  ['GUI package lock', ['src/gui/package-lock.json'], [true, true, true, true, true, true, true]],
  ['root dependency lock', ['uv.lock'], [true, true, true, true, true, true, true]],
  ['runtime markdown', ['src/gui/docs/squire/SKILL-template.md'], [true, true, true, true, true, true, true]],
  ['unrecognized path', ['future/component.js'], [true, true, true, true, true, true, true]],
]) test(`unified dependency scope: ${name}`, () => {
  const result = classify(paths, 'all');
  assert.deepEqual(scopeKeys.map(key => result[key]), expected);
  const python = classify(paths, 'hyprial');
  assert.equal(python.unit, result.python);
  assert.equal(python.gui_integration, result.gui_integration);
});

for (const eventName of ['workflow_dispatch', 'schedule', 'pull_request', 'push']) {
  test(`unified scope fails closed for unavailable ${eventName} comparison`, () => {
    const result = select({}, eventName, 'all', process.cwd(), false);
    assert.ok(scopeKeys.every(key => result[key] === true));
    const python = select({}, eventName, 'hyprial', process.cwd(), false);
    assert.equal(python.unit, true);
    assert.equal(python.gui_integration, true);
  });
}

for (const branch of ['main', 'dev']) test(`all ${branch} release checks remain mandatory`, () => {
  const result = select({ ref: `refs/heads/${branch}` }, 'push', 'all', process.cwd(), false);
  assert.ok(scopeKeys.every(key => result[key] === true));
  assert.equal(select({ ref: `refs/heads/${branch}` }, 'push', 'hyprial', process.cwd(), false).unit, true);
});

test('unified CLI emits all seven gate outputs for a browser-only PR', t => {
  const r = repository(t);
  const base = r.commit('README.md', 'base');
  const head = r.commit('src/gui/dashboard/src/main.jsx', 'ui');
  r.git('remote', 'add', 'origin', r.cwd);
  const payload = join(r.cwd, 'event.json'), output = join(r.cwd, 'output');
  writeFileSync(payload, JSON.stringify({ pull_request: { base: { sha: base }, head: { sha: head } } }));
  execFileSync(process.execPath, [new URL('./ci-scope.mjs', import.meta.url).pathname, 'all'], {
    cwd: r.cwd, env: { ...process.env, GITHUB_EVENT_NAME: 'pull_request', GITHUB_EVENT_PATH: payload, GITHUB_OUTPUT: output },
  });
  assert.equal(readFileSync(output, 'utf8'), 'python=false\ngui_integration=true\nunit=false\ndashboard=true\ncodex=false\ndsh=false\npackage=true\n');
});
