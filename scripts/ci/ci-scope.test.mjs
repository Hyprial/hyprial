import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { changedPaths, classify, select } from './ci-scope.mjs';

for (const [name, paths, project, expected] of [
  ['README script is code', ['README.py'], 'hyprial', [true, false]],
  ['translated README', ['README.zh-CN.md'], 'gui', [false, false]],
  ['README only', ['README.md'], 'gui', [false, false]],
  ['sidecar documentation', ['sidecar/hyprial-tailcat/README.md'], 'hyprial', [false, false]],
  ['guide only', ['docs/domains/gui/team-hyprial-api-client-guide.md'], 'gui', [false, false]],
  ['HYPRIAL docs', ['README.md', 'docs/notes/design.md'], 'hyprial', [false, false]],
  ['ROADMAP only', ['ROADMAP.md'], 'hyprial', [false, false]],
  ['ROADMAP and ordinary docs', ['ROADMAP.md', 'docs/reference/known-problems.md'], 'gui', [false, false]],
  ['glossary is an exact test input', ['GLOSSARY.md'], 'hyprial', [true, false]],
  ['empty change', [], 'gui', [false, false]],
  ['legacy browser client', ['imskin-plugin.js', 'static/client.js'], 'gui', [true, false]],
  ['GUI browser UI', ['client/gui-workspace.inc.js'], 'gui', [true, true]],
  ['GUI browser server needs Node tests too', ['tools/browser/server.mjs'], 'gui', [true, true]],
  ['GUI browser fixture shared with Node', ['tools/browser/tests/fixtures.mjs'], 'gui', [true, true]],
  ['GUI browser Node test', ['tests/gui-contact-layout.browser.mjs'], 'gui', [true, true]],
  ['root lockfile', ['package-lock.json'], 'gui', [true, true]],
  ['GUI browser lockfile', ['tools/browser/package-lock.json'], 'gui', [true, true]],
  ['launcher', ['scripts/start-gui.sh'], 'gui', [true, true]],
  ['workflow', ['.github/workflows/gate-python.yml'], 'gui', [true, true]],
  ['unknown input', ['new-component/config.json'], 'gui', [true, true]],
  ['mixed docs and UI', ['README.md', 'client/gui-workspace.inc.js'], 'gui', [true, true]],
  ['both UIs', ['client/workflow.js', 'client/gui-workspace.inc.js'], 'gui', [true, true]],
  ['HYPRIAL source', ['src/hyprial/gui_apps.py'], 'hyprial', [true, false]],
  ['runtime Markdown', ['src/hyprial/skills/hyprial-ops/SKILL.md'], 'hyprial', [true, false]],
  ['runtime doc template', ['docs/domains/squire/SKILL-template.md'], 'hyprial', [true, false]],
  ['contract doc input', ['docs/guides/cutover-runbook.md'], 'hyprial', [true, false]],
  ['script under docs is not prose', ['docs/example.py'], 'hyprial', [true, false]],
]) test(name, () => {
  const result = classify(paths, project);
  assert.deepEqual([result.unit, result.browser], expected);
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
  assert.equal(select({ before, after }, 'push', 'gui', r.cwd, false).browser, true);
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
  assert.equal(result.unit, true); assert.equal(result.browser, true);
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
  assert.equal(readFileSync(output, 'utf8'), 'unit=false\nbrowser=false\n');
  assert.match(readFileSync(summary, 'utf8'), /documentation-only/);
});

test('repository scope authority runs without any GUI package files', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'ci-scope-independent-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const source = readFileSync(new URL('./ci-scope.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /(?:from\s+|import\s*\()['"][^'"]*src\/gui\//);
  const entry = join(directory, 'ci-scope.mjs');
  writeFileSync(entry, source);
  const independent = await import(new URL(`file://${entry}`));
  assert.equal(independent.classify(['src/gui/client/gui-workspace.inc.js'], 'hyprial').unit, false);
  assert.equal(independent.classify(['src/gui/scripts/start-gui.sh'], 'hyprial').unit, true);
});

for (const path of ['src/gui/client/index.mjs', 'src/gui/session/index.mjs',
  'src/gui/studio/core.mjs', 'src/gui/host/index.mjs', 'src/gui/presentation/index.mjs',
  'src/gui/transport/index.mjs', 'src/gui/product/build.mjs', 'src/gui/scripts/check-architecture.mjs']) {
  test(`independent GUI module retains Python and GUI integration: ${path}`, () => {
    const result = classify([path], 'all');
    for (const key of ['python', 'gui_integration', 'unit', 'browser', 'package']) assert.equal(result[key], true, key);
  });
}

test('root CI policy changes retain every check', () => {
  const result = classify(['scripts/ci/ci-scope.mjs'], 'all');
  for (const key of ['python', 'gui_integration', 'unit', 'browser', 'codex', 'package']) assert.equal(result[key], true, key);
});

for (const path of ['tests/tooling/ci/execution/test_ci_internal_origin.py',
  'tests/shell/gui/lifecycle/test_gui_runtime.py', 'tests/unknown/test_new_policy.py']) {
  test(`semantic or unknown Python tests remain conservative: ${path}`, () => {
    const result = classify([path], 'all');
    for (const key of ['python', 'gui_integration', 'unit', 'browser', 'codex', 'package']) {
      assert.equal(result[key], true, `${path}: ${key}`);
    }
  });
}

const nativeSource = 'src/gui/integration/hyprial-cli.mjs';
const nativeTest = 'src/gui/tests/hyprial-cli.test.mjs';
test('audited native CLI changes preserve every GUI gate and Python integration', () => {
  const result = classify([nativeSource, nativeTest, 'docs/example.md'], 'all');
  assert.equal(result.python, false);
  for (const key of ['gui_integration', 'unit', 'browser', 'codex', 'package']) assert.equal(result[key], true, key);
});
for (const path of ['src/gui/integration/new-helper.mjs', 'src/gui/integration/pac-service.py',
  'src/gui/scripts/install-local.sh', 'src/gui/package-lock.json', '.github/workflows/gate-python.yml',
  'src/gui/scripts/ci-scope.mjs', 'src/hyprial/cli.py', 'tests/conftest.py']) {
  test(`native CLI mixed with ${path} retains full Python`, () => {
    assert.equal(classify([nativeSource, path], 'all').python, true);
  });
}
for (const operation of ['add', 'modify', 'delete', 'rename', 'symlink']) {
  test(`real Git native CLI ${operation} preserves the structural boundary`, t => {
    const r = repository(t);
    const before = r.commit(nativeSource, 'export const value = 1;');
    if (operation === 'add') r.commit(nativeTest, 'test');
    if (operation === 'modify') r.commit(nativeSource, 'export const value = 2;');
    if (operation === 'delete') r.git('rm', nativeSource);
    if (operation === 'rename') r.git('mv', nativeSource, 'src/gui/integration/unknown.mjs');
    if (operation === 'symlink') {
      rmSync(join(r.cwd, nativeSource));
      execFileSync('ln', ['-s', '/tmp/not-a-module', join(r.cwd, nativeSource)]);
    }
    if (['delete', 'rename', 'symlink'].includes(operation)) { r.git('add', '-A'); r.git('commit', '-m', operation); }
    const after = r.git('rev-parse', 'HEAD');
    const event = { pull_request: { base: { sha: before }, head: { sha: after } } };
    for (const cwd of [r.cwd, join(r.cwd, 'src/gui')]) {
      const result = select(event, 'pull_request', 'all', cwd, false);
      assert.equal(result.python, !['add', 'modify'].includes(operation));
      assert.equal(result.gui_integration, true);
    }
    for (const ref of ['refs/heads/dev', 'refs/heads/main']) {
      assert.equal(select({ before, after, ref }, 'push', 'all', r.cwd, false).python, true);
    }
  });
}

test('missing audited CLI history and manual execution cannot select a subset', () => {
  const event = { pull_request: { base: { sha: 'a'.repeat(40) }, head: { sha: 'b'.repeat(40) } } };
  for (const name of ['pull_request', 'workflow_dispatch']) {
    assert.equal(select(event, name, 'all', process.cwd(), false).python, true);
    assert.equal(select(event, name, 'hyprial', process.cwd(), false).unit, true);
  }
});

for (const target of ['dev', 'main', 'intg/gui']) {
  for (const path of ['README.md', 'src/gui/client/gui-workspace.inc.js', nativeSource,
    'src/hyprial/cli.py', '.github/workflows/gate-python.yml', 'src/gui/integration/unknown.mjs']) {
    test(`tier ${target}: ${path}`, t => {
      const r = repository(t);
      const base = r.commit('README.md', 'base');
      const head = r.commit(path, 'changed');
      const event = { pull_request: { base: { sha: base, ref: target }, head: { sha: head } } };
      const focused = target === 'intg/gui' &&
        ['README.md', 'src/gui/client/gui-workspace.inc.js', nativeSource].includes(path);
      for (const project of ['all', 'hyprial']) {
        const result = select(event, 'pull_request', project, r.cwd, false);
        assert.equal(project === 'all' ? result.python : result.unit, !focused);
        assert.equal(result.gui_integration, !(target === 'intg/gui' && path === 'README.md'));
      }
      const gui = select(event, 'pull_request', 'all', r.cwd, false);
      if (path !== 'README.md' || target !== 'intg/gui') {
        for (const gate of ['unit', 'browser', 'codex', 'package']) assert.equal(gui[gate], true);
      }
    });
  }
}
test('lane tier label follows the intg/gui* prefix, not one exact branch name', t => {
  const r = repository(t);
  const base = r.commit('README.md', 'base');
  const head = r.commit('src/gui/client/gui-workspace.inc.js', 'changed');
  for (const ref of ['intg/gui', 'intg/gui-next']) {
    const event = { pull_request: { base: { sha: base, ref }, head: { sha: head } } };
    assert.match(select(event, 'pull_request', 'gui', r.cwd, false).reason, /^gui integration tier: /);
  }
  const onMain = { pull_request: { base: { sha: base, ref: 'main' }, head: { sha: head } } };
  assert.doesNotMatch(select(onMain, 'pull_request', 'gui', r.cwd, false).reason, /integration tier/);
});
test('integration missing history remains full', () => {
  const event = { pull_request: { base: { ref: 'intg/gui', sha: 'a'.repeat(40) }, head: { sha: 'b'.repeat(40) } } };
  assert.equal(select(event, 'pull_request', 'all', process.cwd(), false).python, true);
});

test('shallow PR history deepens to a real diff instead of failing comparison', t => {
  const r = repository(t);
  r.git('config', 'uploadpack.allowReachableSHA1InWant', 'true');
  const base = r.commit('README.md', 'base');
  r.git('checkout', '-b', 'pr');
  // 130 commits between the merge base and head: the fixed depth of 128 is
  // three short, while the object itself remains present from the base fetch.
  for (let i = 0; i < 130; i += 1) r.commit('src/hyprial/daemon/example.py', `change ${i}`);
  const head = r.git('rev-parse', 'HEAD');
  const checkout = mkdtempSync(join(tmpdir(), 'ci-scope-shallow-pr-'));
  t.after(() => rmSync(checkout, { recursive: true, force: true }));
  execFileSync('git', ['clone', '--depth=1', '--branch', 'pr', `file://${r.cwd}`, checkout], { stdio: 'pipe' });
  const event = { pull_request: { base: { sha: base }, head: { sha: head } } };
  assert.deepEqual(changedPaths(event, 'pull_request', checkout, true), ['src/hyprial/daemon/example.py']);
});
