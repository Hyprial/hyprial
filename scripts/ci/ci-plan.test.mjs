// Dev-first routing accepts every source and binds suites to the complete diff.
// Allen 2026-11-10: the Python suite binds to the product tree (src/hyprial/)
// only; tests, scripts, Python config, shared CI, unknown files and dev
// landings no longer force it, while full/manual/main/deep-refactor lanes
// keep theirs.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, renameSync, rmSync, writeFileSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';
import { makePlan, component, infrastructure } from './ci-plan.mjs';

function repo(t) {
  const root = mkdtempSync(join(tmpdir(), 'ci-routing-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git('init', '-b', 'dev'); git('config', 'user.email', 'ci@example.invalid'); git('config', 'user.name', 'CI fixture');
  mkdirSync(join(root, '.github'), { recursive: true });
  writeFileSync(join(root, '.github/git-flow.json'), readFileSync(new URL('../../.github/git-flow.json', import.meta.url)));
  function commit(path, content = 'fixture') {
    mkdirSync(join(root, path, '..'), { recursive: true }); writeFileSync(join(root, path), content);
    git('add', '-A'); git('commit', '-m', 'fixture'); return git('rev-parse', 'HEAD');
  }
  const base = commit('README.md');
  git('update-ref', 'refs/remotes/origin/dev', base);
  return { root, git, commit, base };
}
function pr(r, sha, head = 'feature/example', base = 'dev') {
  return { pull_request: { base: { sha: r.base, ref: base }, head: { sha, ref: head } } };
}
const plan = (r, event, name = 'pull_request') => makePlan(event, name, r.root, { fetch: false });

test('root GUI paths plan without a component-source restriction', t => {
  const r = repo(t), head = r.commit('gui/src/App.tsx');
  assert.equal(component('gui/src/App.tsx'), 'gui');
  const direct = plan(r, pr(r, head));
  assert.equal(direct.gui, true);
  assert.equal(direct.gui_integration, true);
  assert.equal(direct.browser, true);
  assert.equal(direct.python, false);
  assert.equal(direct.full, true);
  assert.equal(plan(r, pr(r, head, 'feat/org', 'intg/gui-init')).gui, true);
  assert.equal(component('gui/docs/org/README.md'), null);
});

test('Python selection follows the product tree, Python tests, dependency pins and the suite runner', t => {
  for (const [path, python] of [
    ['src/hyprial/daemon/example.py', true],
    ['src/hyprial/daemon/schema.json', true],
    ['pyproject.toml', true], ['uv.lock', true],
    ['scripts/refactor/pyproject.toml', false],
    ['tests/shell/gui/lifecycle/test_gui_runtime.py', true],
    ['tests/daemon/teleport/test_frames.py', true],
    ['tests/fixtures/network/payload.json', false],
    ['scripts/ci/pytest_impact.py', true], ['scripts/ci/pytest-durations.json', true],
    ['scripts/ci/check_windows_portability.py', true], ['scripts/ci/windows_portability_semantics.py', true],
    ['scripts/ci/windows-portability-baseline.json', true], ['scripts/ci/check_node_platform_idioms.py', true],
    ['scripts/ci/node-platform-idioms-baseline.json', true], ['scripts/ci/check_pyright_windows.py', true],
    ['.github/workflows/gate-python.yml', true],
    ['.github/workflows/gate-gui.yml', false],
    ['scripts/ci/ci-plan.mjs', false], ['gui/package.json', false],
  ]) {
    const r = repo(t), head = r.commit(path);
    const result = plan(r, pr(r, head));
    assert.equal(result.python, python, path);
    assert.equal(result.full, true, path);
  }
  const gui = repo(t), guiHead = gui.commit('gui/package.json');
  const guiResult = plan(gui, pr(gui, guiHead));
  assert.equal(guiResult.gui, true);
  assert.equal(guiResult.gui_integration, true);
  const lifecycle = repo(t);
  const lifecycleResult = plan(lifecycle, pr(lifecycle,
    lifecycle.commit('tests/shell/gui/lifecycle/test_gui_runtime.py')));
  assert.equal(lifecycleResult.python, true);
  assert.equal(lifecycleResult.gui, true);
  const guiHome = repo(t);
  const guiHomeResult = plan(guiHome, pr(guiHome,
    guiHome.commit('tests/support/gui_home/prepare.py')));
  assert.equal(component('tests/support/gui_home/prepare.py'), 'gui');
  assert.equal(guiHomeResult.gui, true);
  assert.equal(guiHomeResult.browser, true);
  assert.equal(guiHomeResult.python, true);
});

const assertAllRefactorGates = result => {
  for (const key of ['python', 'gui_integration', 'gui', 'browser', 'codex', 'package', 'sidecar']) {
    assert.equal(result[key], true, key);
  }
};

test('deep-refactor topics into the exact approved base run every gate', t => {
  const r = repo(t);
  r.commit('src/gui/client/index.mjs');
  const head = r.commit('sidecar/hyprial-tailcat/cmd/hyprial-tailcat/main.go');
  assertAllRefactorGates(plan(r, pr(r, head, 'feat/tailcat-cutover', 'refactor/reconstruct-modules')));
});

test('deep-refactor integration into dev keeps its complete validation', t => {
  const r = repo(t);
  r.commit('src/gui/client/index.mjs');
  const head = r.commit('sidecar/hyprial-tailcat/cmd/hyprial-tailcat/main.go');
  assertAllRefactorGates(plan(r, pr(r, head, 'refactor/reconstruct-modules', 'dev')));
});

test('release lane: intg/gui* into release/* is accepted and runs the GUI suites', t => {
  const r = repo(t), head = r.commit('gui/src/App.tsx');
  const result = plan(r, pr(r, head, 'intg/gui-a', 'release/v0.5.5.3'));
  assert.equal(result.gui, true);
  assert.equal(result.browser, true);
});
test('release lane: feature sources into release/* plan instead of throwing', t => {
  const r = repo(t), head = r.commit('gui/src/App.tsx');
  const guiResult = plan(r, pr(r, head, 'feature/b', 'release/v0.5.5.3'));
  assert.equal(guiResult.gui, true);
  assert.equal(guiResult.full, true);
  const sidecar = repo(t);
  const sidecarHead = sidecar.commit('sidecar/hyprial-tailcat/internal/serve/a.go', 'sidecar');
  const sidecarResult = plan(sidecar, pr(sidecar, sidecarHead, 'feature/b', 'release/v0.5.5.3'));
  assert.equal(sidecarResult.sidecar, true);
  assert.equal(sidecarResult.full, true);
});
test('release preparation and hotfix sources into dev run the same changed component suites', t => {
  const r = repo(t);
  r.commit('gui/src/App.tsx');
  const head = r.commit('sidecar/hyprial-tailcat/internal/serve/a.go', 'sidecar');
  for (const source of ['release/v0.5.5.3', 'hotfix/example']) {
    const result = plan(r, pr(r, head, source, 'dev'));
    assert.equal(result.gui, true, source);
    assert.equal(result.browser, true, source);
    assert.equal(result.sidecar, true, source);
  }
  const python = plan(r, pr(r, r.commit('src/hyprial/daemon/example.py'), 'release/v0.5.5.3', 'dev'));
  assert.equal(python.python, true);
  assert.equal(python.gui, true);
});
test('dev accepts every source branch with the same required validation', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  for (const source of ['feature/c', 'intg/gui-d', 'intg/sidecar-d', 'intg/batch-1008', 'intg/cli-command-surface',
    'intg/pac-v2-full-impl', 'releases/v1', 'feature/release/v1', 'hotfixes/x']) {
    assert.equal(plan(r, pr(r, head, source, 'dev')).python, true, source);
  }
  const docs = r.commit('README.md', 'docs only');
  r.base = head;
  const result = plan(r, pr(r, docs, 'feature/c', 'dev'));
  assert.equal(result.full, false);
  assert.equal(result.python, false);
});

test('ordinary dev PR selects every changed component without a release or intg name', t => {
  const r = repo(t);
  r.commit('gui/src/App.tsx');
  const head = r.commit('sidecar/hyprial-tailcat/internal/serve/a.go');
  const result = plan(r, pr(r, head, 'feat/cross-component', 'dev'));
  for (const suite of ['gui', 'gui_integration', 'browser', 'codex', 'sidecar']) {
    assert.equal(result[suite], true, suite);
  }
  assert.equal(result.python, false);
  assert.equal(result.package, false);
});

test('missing or invalid central Git flow config fails closed without branch fallbacks', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  for (const contents of ['{', '{}', JSON.stringify({schemaVersion: 99}),
    JSON.stringify({schemaVersion: 1, mode: 'release-first'})]) {
    writeFileSync(join(r.root, '.github/git-flow.json'), contents);
    assert.throws(() => plan(r, pr(r, head, 'release/current', 'dev')), /CI_ROUTE: invalid Git flow config/);
  }
  rmSync(join(r.root, '.github/git-flow.json'));
  assert.throws(() => plan(r, pr(r, head, 'release/current', 'dev')), /CI_ROUTE: invalid Git flow config/);
});
test('deep-refactor exact branch pushes run every gate', t => {
  const r = repo(t);
  r.commit('src/gui/client/index.mjs');
  const after = r.commit('sidecar/hyprial-tailcat/cmd/hyprial-tailcat/main.go');
  assertAllRefactorGates(plan(r, { ref: 'refs/heads/refactor/reconstruct-modules', before: r.base, after }, 'push'));
});

test('deep-refactor route still verifies the comparison and checkout SHA', t => {
  const r = repo(t), head = r.commit('src/gui/client/index.mjs');
  assert.throws(() => plan(r, pr(r, r.base, 'feat/example', 'refactor/reconstruct-modules')), /CI_ROUTE:/);
  const event = pr(r, head, 'feat/example', 'refactor/reconstruct-modules');
  event.pull_request.base.sha = '0'.repeat(40);
  assert.throws(() => plan(r, event), /CI_ROUTE:/);
});

test('deep-refactor lookalikes use changed components without enabling unrelated gates', t => {
  const r = repo(t), head = r.commit('src/gui/client/index.mjs');
  for (const [source, base] of [
    ['feature/refactor/reconstruct-modules', 'dev'],
    ['refactor/reconstruct-modules-extra', 'dev'],
  ]) {
    const result = plan(r, pr(r, head, source, base));
    assert.equal(result.gui, true, `${source}->${base}`);
    assert.equal(result.sidecar, false, `${source}->${base}`);
  }
  for (const [source, base] of [
    ['feat/example', 'refactor/unapproved'],
    ['refactor/reconstruct-modules', 'feat/example'],
  ]) {
    const result = plan(r, pr(r, head, source, base));
    assert.equal(result.gui, true, `${source}->${base}`);
    assert.equal(result.sidecar, false, `${source}->${base}`);
  }
});

test('semantic CI test folders retain shared infrastructure ownership', t => {
  for (const path of ['tests/tooling/ci/execution/test_ci_internal_origin.py',
    'tests/tooling/pytest/test_pytest_impact.py',
    'tests/shell/gui/ci/test_gui_ci_migration.py']) {
    assert.equal(infrastructure(path), true, path);
    assert.equal(component(path), null, path);
    const r = repo(t);
    // Keep the legacy packaged-GUI tree present so the shared plan still
    // selects the package gate; the retired-tree shape is covered below.
    r.base = r.commit('src/gui/package.json');
    const head = r.commit(path);
    const result = plan(r, pr(r, head));
    assert.equal(result.python, true, `${path}: python`);
    for (const gate of ['gui', 'sidecar', 'package']) assert.equal(result[gate], true, `${path}: ${gate}`);
  }
});
for (const [path, owner] of [
  ['tests/shell/gui/lifecycle/test_gui_runtime.py', 'gui'],
  ['tests/shell/gui/lifecycle/test_gui_apps.py', 'gui'],
  ['tests/shell/gui/lifecycle/test_gui_unpack.py', 'gui'],
  ['tests/daemon/forwarding/sidecar/test_forwarding_sidecar_controller.py', 'sidecar'],
  ['tests/fixtures/network/fake_forwarding_sidecar.py', 'sidecar'],
]) {
  test(`semantic migrated component keeps its suite owner: ${path}`, t => {
    assert.equal(component(path), owner);
    const r = repo(t), head = r.commit(path);
    assert.equal(plan(r, pr(r, head))[owner], true);
    assert.equal(plan(r, pr(r, head, 'feature/example', `intg/${owner}-next`))[owner], true);
  });
}
test('deletion of an old component test and addition at its new path stays in the diff', t => {
  const r = repo(t);
  r.base = r.commit('tests/test_gui_runtime.py');
  r.git('rm', 'tests/test_gui_runtime.py');
  const head = r.commit('tests/shell/gui/lifecycle/test_gui_runtime.py');
  const direct = plan(r, pr(r, head));
  assert.equal(direct.python, true);
  assert.equal(direct.gui, true);
  assert.equal(plan(r, pr(r, head, 'intg/gui-next')).gui, true);
});

for (const [path, python] of [
  ['src/hyprial/shell/impl/gui/adapter/host.py', true],
  ['tests/shell/gui/adapter/test_app_auth_host_wiring.py', true],
  ['tests/shell/gui/lifecycle/test_gui_launcher.py', true],
  ['tests/shell/gui/lifecycle/test_gui_signals.py', true],
]) {
  test(`CLI-command-surface consumer keeps its GUI component ownership: ${path}`, t => {
    assert.equal(component(path), 'gui');
    const r = repo(t), head = r.commit(path);
    const feature = plan(r, pr(r, head, 'ci/a04-command-surface', 'intg/cli-command-surface'));
    assert.equal(feature.python, python);
    assert.equal(feature.gui, true);
    assert.equal(plan(r, pr(r, head, 'intg/cli-command-surface')).python, python);
    const direct = plan(r, pr(r, head));
    assert.equal(direct.python, python);
    assert.equal(direct.gui, true);
    const lookalike = plan(r, pr(r, head, 'feature/example', 'intg/cli-command-surface-extra'));
    assert.equal(lookalike.python, python);
  });
}

test('CLI-command-surface plans carry product paths without a source-branch refusal', t => {
  for (const path of [
    'src/gui/client/gui-main.js',
    'app/src-tauri/tauri.conf.json',
    'sidecar/hyprial-tailcat/internal/serve/a.go',
  ]) {
    const r = repo(t), head = r.commit(path);
    const result = plan(r, pr(r, head, 'ci/a04-command-surface', 'intg/cli-command-surface'));
    assert.equal(result.full, true, path);
  }
  const otherGui = repo(t), guiHead = otherGui.commit('src/hyprial/shell/impl/gui/runtime.py');
  assert.equal(plan(otherGui, pr(otherGui, guiHead, 'ci/a04-command-surface', 'intg/cli-command-surface')).full, true);
});

for (const [path, domain] of [
  ['src/gui/client/gui-main.js', 'gui'], ['tests/test_gui_runtime.py', 'gui'],
  ['src/hyprial/gui_apps.py', 'gui'], ['src/hyprial/shell/impl/gui/unpack.py', 'gui'],
  ['sidecar/hyprial-tailcat/internal/serve/a.go', 'sidecar'], ['tests/test_sidecar.py', 'sidecar'],
]) {
  test(`${domain}: changed paths select suites for every source or target name: ${path}`, t => {
    const r = repo(t), head = r.commit(path);
    assert.equal(plan(r, pr(r, head))[domain], true);
    assert.equal(plan(r, pr(r, head, 'feature/example', `intg/${domain}-next`))[domain], true);
    assert.equal(plan(r, pr(r, head, `intg/${domain}-next`))[domain], true);
    assert.equal(plan(r, pr(r, head, `feature/intg/${domain}-fake`))[domain], true);
  });
}
test('retired root installer builder is no longer a GUI route owner', () => {
  assert.equal(component('scripts/build_gui_release.py'), null);
});
test('retired TSNet paths are not retained as explicit sidecar route owners', () => {
  for (const path of [
    'src/hyprial/tsnet/client.py',
    'tests/test_tsnet_sidecar.py',
    'tests/integration/network/join/test_identity_admission.py',
    'tests/tooling/ci/acquisition/test_ci_go_modules.py',
  ]) assert.equal(component(path), null, path);
  assert.equal(component('sidecar/hyprial-tailcat/internal/serve/main.go'), 'sidecar');
});
test('mixed component changes select both suites in every integration lane', t => {
  const r = repo(t); r.commit('src/gui/a.mjs'); const head = r.commit('sidecar/a.go');
  const guiLane = plan(r, pr(r, head, 'intg/gui-next'));
  assert.equal(guiLane.gui, true);
  assert.equal(guiLane.sidecar, true);
  const sidecarLane = plan(r, pr(r, head, 'intg/sidecar-next'));
  assert.equal(sidecarLane.sidecar, true);
  assert.equal(sidecarLane.gui, true);
});
test('whole PR range includes earlier component change', t => {
  const r = repo(t); r.commit('src/gui/a.mjs'); const head = r.commit('README.md', 'last change');
  const result = plan(r, pr(r, head));
  assert.equal(result.full, true);
  assert.equal(result.gui, true);
});
test('rename out of GUI keeps the old component path in the diff', t => {
  const r = repo(t); r.base = r.commit('src/gui/a.js');
  renameSync(join(r.root, 'src/gui/a.js'), join(r.root, 'README.md'));
  r.git('add', '-A'); r.git('commit', '-m', 'rename');
  const result = plan(r, pr(r, r.git('rev-parse', 'HEAD')));
  assert.equal(result.full, true);
  assert.equal(result.gui, true);
});
test('backend PR omits unrelated component suites; documentation omits Python too', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const result = plan(r, pr(r, head));
  assert.equal(result.python, true); assert.equal(result.gui, false); assert.equal(result.sidecar, false);
  r.base = head;
  const docs = plan(r, pr(r, r.commit('README.md', 'docs')));
  assert.equal(docs.python, false); assert.equal(docs.gui, false);
});
test('tailcat PR selects the Go gate while a pure Python PR does not', t => {
  const tailcat = repo(t);
  const tailcatHead = tailcat.commit('sidecar/hyprial-tailcat/internal/serve/gate_test.go');
  const tailcatPlan = plan(tailcat, pr(tailcat, tailcatHead, 'feature/go-gate', 'intg/sidecar-next'));
  assert.equal(tailcatPlan.sidecar, true);

  const python = repo(t);
  const pythonHead = python.commit('src/hyprial/daemon/example.py');
  assert.equal(plan(python, pr(python, pythonHead)).sidecar, false);
});
test('ordinary PRs into an integration batch run only the light plan', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const result = plan(r, pr(r, head, 'feature/example', 'intg/batch-1008'));
  assert.equal(result.full, false);
  assert.equal(result.light, true);
  for (const gate of ['python', 'gui_integration', 'gui', 'browser', 'codex', 'package', 'sidecar']) {
    assert.equal(result[gate], false, gate);
  }
});
test('a PR body edit fails closed to the full plan', t => {
  // gate-python does not listen for `edited`; pr-hygiene re-evaluates the body
  // on its own required workflow. A body edit therefore never produces a light
  // run. If the planner is invoked for one anyway, it must demand the full plan
  // rather than publish `unit` success with every shard skipped.
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const event = pr(r, head);
  event.action = 'edited';
  const result = plan(r, event);
  assert.equal(result.full, true);
  assert.equal(result.light, false);
  assert.equal(result.python, true);
});
test('an integration batch PR into a release branch keeps full Python validation', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const result = plan(r, pr(r, head, 'intg/batch-1008'));
  assert.equal(result.full, true);
  assert.equal(result.light, false);
  assert.equal(result.python, true);
});
test('component integration targets keep their component suites', t => {
  const r = repo(t), head = r.commit('src/gui/client/example.mjs');
  const result = plan(r, pr(r, head, 'feature/example', 'intg/gui-x'));
  assert.equal(result.full, true);
  assert.equal(result.light, false);
  assert.equal(result.gui, true);
  assert.equal(result.package, true);
});
test('integration branch runs its component even on docs-only PR or push', t => {
  const r = repo(t), head = r.commit('README.md', 'docs');
  for (const domain of ['gui', 'sidecar']) {
    assert.equal(plan(r, pr(r, head, `intg/${domain}-next`))[domain], true);
    assert.equal(plan(r, { ref: `refs/heads/intg/${domain}-next`, before: r.base, after: head }, 'push')[domain], true);
  }
});
test('dev landing keeps full validation and selects Python only on Python inputs', t => {
  const r = repo(t); r.commit('src/gui/client/gui-main.js'); const after = r.commit('sidecar/a.go');
  const result = plan(r, { before: r.base, after, ref: 'refs/heads/dev' }, 'push');
  assert.equal(result.python, false); assert.equal(result.gui, true); assert.equal(result.sidecar, true);
  assert.equal(result.full, true);
  const withPython = r.commit('src/hyprial/daemon/example.py');
  const pythonLanding = plan(r, { before: r.base, after: withPython, ref: 'refs/heads/dev' }, 'push');
  assert.equal(pythonLanding.python, true);
  assert.equal(pythonLanding.gui, true); assert.equal(pythonLanding.sidecar, true);
});
test('direct integration pushes include changed components outside their lane name', t => {
  const r = repo(t), after = r.commit('sidecar/a.go');
  const result = plan(r, { before: r.base, after, ref: 'refs/heads/intg/gui-next' }, 'push');
  assert.equal(result.gui, true);
  assert.equal(result.sidecar, true);
});
for (const eventName of ['workflow_dispatch', 'schedule']) test(`${eventName} runs full`, t => {
  const r = repo(t), result = plan(r, {}, eventName);
  assert.equal(result.python, true); assert.equal(result.gui, true); assert.equal(result.sidecar, true);
});
test('main PR/main push full', t => {
  const r = repo(t);
  assert.equal(plan(r, { pull_request: { base: { ref: 'main' } } }).gui, true);
  assert.equal(plan(r, { ref: 'refs/heads/main' }, 'push').sidecar, true);
  assert.equal(plan(r, { ref: 'refs/heads/main' }, 'push').python, true);
});
test('shared CI changes keep every non-Python gate while Python follows Python inputs', t => {
  const r = repo(t), head = r.commit('.github/workflows/gate-gui.yml');
  const result = plan(r, pr(r, head));
  assert.equal(result.sidecar, true);
  assert.equal(result.gui, true);
  assert.equal(result.full, true);
  assert.equal(result.python, false);
  assert.equal(result.package, false);
  const mixedWithProduct = plan(r, pr(r, r.commit('src/gui/a.js')));
  assert.equal(mixedWithProduct.gui, true);
  assert.equal(component('tests/test_gui_ci_migration.py'), null);
});

test('manual and main full plans omit the retired package gate unless src/gui exists', t => {
  const retired = repo(t);
  assert.equal(plan(retired, {}, 'schedule').package, false);
  assert.equal(plan(retired, {}, 'workflow_dispatch').package, false);
  assert.equal(plan(retired, { ref: 'refs/heads/main' }, 'push').package, false);
  assert.equal(plan(retired, { pull_request: { base: { ref: 'main' } } }).package, false);

  const restored = repo(t);
  restored.commit('src/gui/client/gui-main.js');
  assert.equal(plan(restored, {}, 'schedule').package, true);
  assert.equal(plan(restored, { ref: 'refs/heads/main' }, 'push').package, true);
});

test('actual A04 shared delta keeps full validation without the retired package gate', t => {
  const r = repo(t);
  const paths = [
    '.github/workflows/gate-python.yml',
    '.github/workflows/gate-gui.yml',
    'scripts/ci/ci-plan.mjs',
    'scripts/verify/check_cli_help_inventory.py',
    'docs/reference/cli-command-inventory.json',
    'tests/tooling/ci/planning/test_ci_cli_help_inventory.py',
  ];
  let head = r.base;
  for (const path of paths) head = r.commit(path);
  const retired = plan(r, pr(r, head, 'ci/a04-command-surface', 'intg/cli-command-surface'));
  assert.equal(retired.full, true);
  assert.equal(retired.python, true);
  assert.equal(retired.gui, true);
  assert.equal(retired.package, false);

  const present = repo(t);
  present.base = present.commit('src/gui/package.json');
  const presentHead = present.commit('.github/workflows/gate-gui.yml');
  const restored = plan(present, pr(present, presentHead, 'ci/a04-command-surface', 'intg/cli-command-surface'));
  assert.equal(restored.package, true);
});

for (const path of ['scripts/ci/ci-scope.mjs', 'scripts/ci/check_structural_limits.py']) {
  test(`repository CI policy is infrastructure, not GUI product: ${path}`, () => {
    assert.equal(component(path), null);
  });
}
test('missing PR history, metadata or wrong checkout refuses rather than emits an empty green plan', t => {
  const r = repo(t);
  assert.throws(() => plan(r, {}), /CI_ROUTE:/);
  assert.throws(() => plan(r, pr(r, 'a'.repeat(40))), /CI_ROUTE:/);
  const head = r.commit('README.md', 'new'); r.commit('README.md', 'newer');
  assert.throws(() => plan(r, pr(r, head)), /CI_ROUTE:/);
});
test('CLI ordinary dev PR succeeds and emits content-selected suites', t => {
  const r = repo(t), head = r.commit('src/gui/a.js'); r.git('remote', 'add', 'origin', r.root);
  const payload = join(r.root, 'event.json'), output = join(r.root, 'output');
  writeFileSync(payload, JSON.stringify(pr(r, head, 'feature/gui-x', 'dev'))); writeFileSync(output, '');
  const result = spawnSync(process.execPath, [new URL('./ci-plan.mjs', import.meta.url).pathname], {
    cwd: r.root, env: { ...process.env, GITHUB_EVENT_NAME: 'pull_request', GITHUB_EVENT_PATH: payload, GITHUB_OUTPUT: output }, encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(readFileSync(output, 'utf8'), /gui=true\n/);
  assert.match(readFileSync(output, 'utf8'), /full=true\n/);
});

test('CLI invalid policy exits nonzero without success outputs or config contents', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const payload = join(r.root, 'event.json'), output = join(r.root, 'output');
  writeFileSync(payload, JSON.stringify(pr(r, head)));
  writeFileSync(output, '');
  writeFileSync(join(r.root, '.github/git-flow.json'), '{private-fixture-marker');
  const result = spawnSync(process.execPath, [new URL('./ci-plan.mjs', import.meta.url).pathname], {
    cwd: r.root, env: { ...process.env, GITHUB_EVENT_NAME: 'pull_request', GITHUB_EVENT_PATH: payload, GITHUB_OUTPUT: output }, encoding: 'utf8',
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /CI_ROUTE: invalid Git flow config/);
  assert.doesNotMatch(result.stderr, /private-fixture-marker/);
  assert.equal(readFileSync(output, 'utf8'), '');
});

function mergedDev(t) {
  const r = repo(t);
  r.base = r.commit('src/gui/client/gui-main.js', 'original GUI');
  r.git('checkout', '-b', 'intg/sidecar-next');
  const before = r.commit('sidecar/a.go', 'sidecar work');
  r.git('checkout', 'dev');
  const upstream = r.commit('src/gui/client/gui-main.js', 'GUI already accepted on dev');
  r.git('update-ref', 'refs/remotes/origin/dev', upstream);
  r.git('checkout', 'intg/sidecar-next');
  r.git('merge', '--no-ff', 'dev', '-m', 'merge dev');
  const after = r.git('rev-parse', 'HEAD');
  return { ...r, before, upstream, after };
}

test('integration push excludes GUI imported from dev, matching the PR decision', t => {
  const r = mergedDev(t);
  const push = plan(r, { before: r.before, after: r.after, ref: 'refs/heads/intg/sidecar-next' }, 'push');
  const pull = plan(r, pr({ ...r, base: r.upstream }, r.after, 'intg/sidecar-next'));
  assert.equal(push.sidecar, true); assert.equal(push.gui, false);
  assert.equal(push.python, pull.python);
  assert.equal(push.comparisonBase, r.upstream);
});

test('historical replay excludes the merge that already incorporated the pushed head', t => {
  const r = mergedDev(t);
  r.git('checkout', 'dev');
  r.git('merge', '--no-ff', 'intg/sidecar-next', '-m', 'land branch');
  r.git('update-ref', 'refs/remotes/origin/dev', r.git('rev-parse', 'HEAD'));
  r.git('checkout', '--detach', r.after);
  const result = plan(r, { before: r.before, after: r.after, ref: 'refs/heads/intg/sidecar-next' }, 'push');
  assert.equal(result.sidecar, true); assert.equal(result.gui, false);
  assert.equal(result.comparisonBase, r.upstream);
});

test('integration merge resolution changes stay in the comparison diff', t => {
  const r = mergedDev(t);
  r.commit('src/gui/client/gui-main.js', 'changed by sidecar merge resolution');
  r.git('reset', '--soft', 'HEAD~2');
  // A replacement merge with the same two parents and an edited result tree.
  const tree = r.git('write-tree');
  const after = r.git('commit-tree', tree, '-p', r.before, '-p', r.upstream, '-m', 'resolution');
  r.git('reset', '--soft', after);
  const result = plan(r, { before: r.before, after, ref: 'refs/heads/intg/sidecar-next' }, 'push');
  assert.equal(result.sidecar, true);
  assert.equal(result.gui, true);
});

test('earlier component work stays in the comparison range of a later documentation push', t => {
  const r = repo(t);
  const before = r.commit('src/gui/a.js');
  const after = r.commit('README.md', 'last change');
  const result = plan(r, { before, after, ref: 'refs/heads/intg/sidecar-next' }, 'push');
  assert.equal(result.sidecar, true);
  assert.equal(result.gui, true);
});

test('new integration branch still compares against dev with an all-zero before SHA', t => {
  const r = repo(t), after = r.commit('src/gui/a.js');
  const result = plan(r, { before: '0'.repeat(40), after, ref: 'refs/heads/intg/sidecar-next' }, 'push');
  assert.equal(result.sidecar, true);
  assert.equal(result.gui, true);
});

test('integration push refuses an unavailable or non-independent dev baseline', t => {
  const r = repo(t), after = r.commit('sidecar/a.go');
  r.git('update-ref', '-d', 'refs/remotes/origin/dev');
  const event = { before: r.base, after, ref: 'refs/heads/intg/sidecar-next' };
  assert.throws(() => plan(r, event, 'push'), /CI_ROUTE:/);
  r.git('update-ref', 'refs/remotes/origin/dev', after);
  assert.throws(() => plan(r, event, 'push'), /CI_ROUTE:/);
});

test('shallow CI checkout fetches the independent dev baseline before planning', t => {
  const r = mergedDev(t);
  const checkout = mkdtempSync(join(tmpdir(), 'ci-route-shallow-'));
  t.after(() => rmSync(checkout, { recursive: true, force: true }));
  execFileSync('git', ['clone', '--depth=1', '--branch', 'intg/sidecar-next', `file://${r.root}`, checkout], { stdio: 'pipe' });
  const result = makePlan({ before: r.before, after: r.after, ref: 'refs/heads/intg/sidecar-next' }, 'push', checkout);
  assert.equal(result.comparisonBase, r.upstream);
  assert.equal(result.sidecar, true); assert.equal(result.gui, false);
});

test('landing a branch later does not change its replay plan', t => {
  const r = repo(t); r.git('checkout', '-b', 'intg/sidecar-next');
  const after = r.commit('src/gui/a.js');
  r.git('checkout', 'dev'); r.git('merge', '--no-ff', 'intg/sidecar-next', '-m', 'land despite failed CI');
  r.git('update-ref', 'refs/remotes/origin/dev', r.git('rev-parse', 'HEAD'));
  r.git('checkout', '--detach', after);
  const result = plan(r, { before: r.base, after, ref: 'refs/heads/intg/sidecar-next' }, 'push');
  assert.equal(result.sidecar, true);
  assert.equal(result.gui, true);
});

test('PAC migration and lookalike names retain the actual mixed component suites', t => {
  const r = repo(t); r.commit('src/gui/client/workflow.js');
  const head = r.commit('src/hyprial/pac/workflow.py');
  const selected = plan(r, pr(r, head, 'intg/pac-v2-full-impl'));
  assert.equal(selected.gui, true); assert.equal(selected.python, true);
  assert.equal(selected.browser, true); assert.equal(selected.codex, true); assert.equal(selected.package, true);
  assert.equal(plan(r, pr(r, head, 'feature/pac', 'intg/pac-v2-full-impl')).gui, true);
  for (const fake of ['intg/pac-v2-full-impl-other', 'feature/intg/pac-v2-full-impl', 'intg/pac']) {
    assert.equal(plan(r, pr(r, head, fake)).gui, true, fake);
  }
  const mixed = r.commit('sidecar/a.go');
  const pacLane = plan(r, pr(r, mixed, 'intg/pac-v2-full-impl'));
  assert.equal(pacLane.gui, true);
  assert.equal(pacLane.sidecar, true);
});

test('a general integration branch (intg/batch-*) takes all PRs on the light plan', t => {
  const r = repo(t), backend = r.commit('src/hyprial/daemon/example.py');
  const result = plan(r, pr(r, backend, 'fix/example', 'intg/batch-1008'));
  assert.equal(result.light, true);
  assert.equal(result.python, false);
  assert.equal(result.gui, false);
  assert.equal(result.sidecar, false);
  for (const path of ['src/gui/client/index.mjs', 'sidecar/hyprial-tailcat/internal/serve/a.go']) {
    r.base = backend;
    const head = r.commit(path);
    const component = plan(r, pr(r, head, 'fix/example', 'intg/batch-1008'));
    assert.equal(component.light, true, path);
    assert.equal(component.full, false, path);
  }
});

test('shallow PR planning deepens the comparison instead of refusing the route', t => {
  const r = repo(t);
  r.git('config', 'uploadpack.allowReachableSHA1InWant', 'true');
  const base = r.base;
  r.git('checkout', '-b', 'pr');
  // 130 commits after the merge base: the fixed depth of 128 cannot reach it.
  for (let i = 0; i < 130; i += 1) r.commit('src/hyprial/daemon/example.py', `change ${i}`);
  const head = r.git('rev-parse', 'HEAD');
  const checkout = mkdtempSync(join(tmpdir(), 'ci-plan-shallow-pr-'));
  t.after(() => rmSync(checkout, { recursive: true, force: true }));
  execFileSync('git', ['clone', '--depth=1', '--branch', 'pr', `file://${r.root}`, checkout], { stdio: 'pipe' });
  const result = makePlan({ pull_request: {
    base: { sha: base, ref: 'release/v0.0.1' }, head: { sha: head, ref: 'feature/shallow' },
  } }, 'pull_request', checkout);
  assert.equal(result.python, true);
  assert.equal(result.gui, false);
});

test('unreachable comparison ref fails closed with a safe reason', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  r.git('remote', 'add', 'origin', r.root);
  const event = pr(r, head);
  event.pull_request.head.sha = 'a'.repeat(40);
  assert.throws(() => makePlan(event, 'pull_request', r.root),
    /comparison unavailable \(comparison fetch failed at depth 128\)/);
});

test('bounded deepening stops at the ceiling with a safe reason', t => {
  const r = repo(t);
  r.git('remote', 'add', 'origin', r.root);
  r.git('checkout', '--orphan', 'disconnected');
  const head = r.commit('README.md', 'unrelated root');
  assert.throws(() => makePlan(pr(r, head), 'pull_request', r.root),
    /comparison unavailable \(comparison merge base unavailable at depth 4096\)/);
});

test('checkout mismatch fails closed with a diagnostic reason', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  r.commit('README.md', 'checkout moved');
  assert.throws(() => plan(r, pr(r, head)),
    /comparison unavailable \(checkout mismatch\)/);
});

test('shallow PR comparison deepens before selecting suites', t => {
  const r = repo(t);
  r.git('config', 'uploadpack.allowReachableSHA1InWant', 'true');
  const base = r.base;
  r.git('checkout', '-b', 'intg/gui-next');
  r.commit('sidecar/hyprial-tailcat/internal/serve/a.go', 'sidecar');
  for (let i = 0; i < 129; i += 1) r.commit('README.md', `docs ${i}`);
  const head = r.git('rev-parse', 'HEAD');
  const checkout = mkdtempSync(join(tmpdir(), 'ci-plan-shallow-route-'));
  t.after(() => rmSync(checkout, { recursive: true, force: true }));
  execFileSync('git', ['clone', '--depth=1', '--branch', 'intg/gui-next', `file://${r.root}`, checkout], { stdio: 'pipe' });
  const event = { pull_request: {
    base: { sha: base, ref: 'release/v0.0.1' }, head: { sha: head, ref: 'intg/gui-next' },
  } };
  const result = makePlan(event, 'pull_request', checkout);
  assert.equal(result.gui, true);
  assert.equal(result.sidecar, true);
});

test('shallow integration push deepens the dev comparison without changing route scope', t => {
  const r = repo(t);
  r.git('config', 'uploadpack.allowReachableSHA1InWant', 'true');
  r.git('checkout', '-b', 'intg/sidecar-next');
  r.commit('sidecar/hyprial-tailcat/internal/serve/a.go', 'sidecar');
  for (let i = 0; i < 129; i += 1) r.commit('README.md', `docs ${i}`);
  const after = r.git('rev-parse', 'HEAD');
  r.git('checkout', 'dev');
  const devTip = r.commit('src/hyprial/daemon/example.py', 'dev advance');
  r.git('update-ref', 'refs/remotes/origin/dev', devTip);
  const checkout = mkdtempSync(join(tmpdir(), 'ci-plan-shallow-push-'));
  t.after(() => rmSync(checkout, { recursive: true, force: true }));
  execFileSync('git', ['clone', '--depth=1', '--branch', 'intg/sidecar-next', `file://${r.root}`, checkout], { stdio: 'pipe' });
  const result = makePlan({ before: r.base, after, ref: 'refs/heads/intg/sidecar-next' }, 'push', checkout);
  assert.equal(result.sidecar, true);
  assert.equal(result.gui, false);
  assert.equal(result.comparisonBase, r.base);
});

test('legacy release baseline PRs retain validation based on their actual changes', t => {
  const r = repo(t);
  const backend = r.commit('src/hyprial/daemon/example.py');
  const result = plan(r, pr(r, backend, 'fix/maintenance', 'release/v0.5.5.2'));
  assert.equal(result.reason, 'dev-first flow and audited change scope');
  assert.equal(result.python, true);
  assert.equal(result.gui, false);
  assert.equal(result.full, true);

  r.base = backend;
  const docs = r.commit('docs/guides/release-rules.md', 'release note');
  const docsOnly = plan(r, pr(r, docs, 'fix/docs', 'release/v0.5.5.2'));
  assert.equal(docsOnly.reason, 'documentation-only');
  assert.equal(docsOnly.full, false);
  assert.equal(docsOnly.python, false);

  r.base = backend;
  const gui = r.commit('src/gui/client/index.mjs');
  assert.equal(plan(r, pr(r, gui, 'fix/gui-change', 'release/v0.5.5.2')).gui, true);
  assert.equal(plan(r, pr(r, gui, 'intg/gui-next', 'release/v0.5.5.2')).gui, true);
});

test('native conformance selects engine inputs and keeps document test inputs on Python', t => {
  for (const [path, native, python] of [
    ['ROADMAP.md', false, false],
    ['docs/reference/known-problems.md', false, false],
    ['GLOSSARY.md', false, true],
    ['docs/domains/squire/SKILL-template.md', false, true],
    ['docs/guides/cutover-runbook.md', false, true],
    ['docs/design/actor-engine-native-abi-spec-2026-11-01.md', true, false],
    ['docs/design/actor-engine-native-abi-spec-clarifications-2026-11-03.md', true, false],
    ['docs/design/actor-rustify-build-distribution-adr-2026-10-24.md', true, false],
    ['crates/hyprial-core/src/lib.rs', true, false],
    ['conformance/actor_engine/conftest.py', true, false],
    ['src/hyprial/_core/actors.abi3.so', true, true],
    ['src/hyprial/kernel/impl/actor_runtime/port/engine.py', true, true],
    ['src/hyprial/kernel/interface.py', true, true],
    ['src/hyprial/kernel/__init__.py', true, true],
    ['src/hyprial/shell/impl/packaging/native_engine.py', true, true],
    ['src/hyprial/shell/impl/packaging/native_editable.py', true, true],
    ['src/hyprial/gui_build_hook.py', true, true],
    ['scripts/build/build_native_engine.sh', true, false],
    ['scripts/build/manylinux_native_evidence.sh', true, false],
    ['scripts/ci/ci-ensure-rust.sh', true, false],
    ['pyproject.toml', true, true],
    ['uv.lock', true, true],
    ['.github/workflows/native-conformance.yml', true, false],
    ['.github/workflows/gate-python.yml', true, true],
    ['.github/workflows/kp027-native-candidate.yml', true, false],
    ['.github/workflows/wheel-release.yml', true, false],
    ['gui/src/App.tsx', false, false],
    ['sidecar/hyprial-tailcat/internal/serve/main.go', false, false],
    ['.github/workflows/gate-gui.yml', true, false],
    ['new-component/config.json', true, false],
    ['src/hyprial/daemon/impl/example.py', false, true],
    ['tests/kernel/actors/port/test_port_backend_selection.py', false, true],
  ]) {
    const r = repo(t), head = r.commit(path), result = plan(r, pr(r, head));
    assert.equal(result.native, native, path);
    assert.equal(result.python, python, path);
    if (['ROADMAP.md', 'docs/reference/known-problems.md'].includes(path)) {
      assert.equal(result.full, false, path);
      for (const key of ['gui_integration', 'gui', 'browser', 'codex', 'package', 'sidecar']) {
        assert.equal(result[key], false, `${path}: ${key}`);
      }
    }
    if (['GLOSSARY.md', 'docs/domains/squire/SKILL-template.md', 'docs/guides/cutover-runbook.md'].includes(path)) {
      assert.equal(result.full, true, path);
      assert.equal(result.gui, false, path);
      assert.equal(result.sidecar, false, path);
    }
  }
});

test('native conformance preserves explicit full and release routes', t => {
  for (const eventName of ['workflow_dispatch', 'schedule']) {
    const r = repo(t);
    assert.equal(plan(r, {}, eventName).native, true, eventName);
  }
  const mainPush = repo(t);
  assert.equal(plan(mainPush, { ref: 'refs/heads/main' }, 'push').native, true);
  const mainPr = repo(t);
  assert.equal(plan(mainPr, { pull_request: { base: { ref: 'main' } } }).native, true);
  for (const path of ['ROADMAP.md', 'crates/hyprial-core/src/lib.rs', 'new-component/config.json']) {
    const release = repo(t), head = release.commit(path);
    assert.equal(plan(release, pr(release, head, 'fix/release', 'release/v9')).native, true, path);
  }
  const batchEngine = repo(t), engineHead = batchEngine.commit('crates/hyprial-core/src/lib.rs');
  assert.equal(plan(batchEngine, pr(batchEngine, engineHead, 'fix/engine', 'intg/batch-native')).native, true);
  const batchDocs = repo(t), docsHead = batchDocs.commit('ROADMAP.md');
  assert.equal(plan(batchDocs, pr(batchDocs, docsHead, 'fix/docs', 'intg/batch-native')).native, false);
  const batchUnknown = repo(t), unknownHead = batchUnknown.commit('new-component/config.json');
  assert.equal(plan(batchUnknown, pr(batchUnknown, unknownHead, 'fix/unknown', 'intg/batch-native')).native, true);
});

test('native conformance comparison failure refuses the PR route', t => {
  const r = repo(t);
  r.git('remote', 'add', 'origin', r.root);
  const head = r.commit('ROADMAP.md');
  const event = pr(r, head);
  event.pull_request.head.sha = 'a'.repeat(40);
  assert.throws(() => makePlan(event, 'pull_request', r.root), /CI_ROUTE: comparison unavailable/);
});

test('arm64 native lanes are selected only for non-pull_request events on a selected native scope', t => {
  for (const eventName of ['workflow_dispatch', 'schedule']) {
    const r = repo(t), result = plan(r, {}, eventName);
    assert.equal(result.native, true, eventName);
    assert.equal(result.native_arm64, true, eventName);
  }
  const engine = repo(t), engineHead = engine.commit('crates/hyprial-core/src/lib.rs');
  const enginePr = plan(engine, pr(engine, engineHead));
  assert.equal(enginePr.native, true);
  assert.equal(enginePr.native_arm64, false);
  const enginePush = plan(engine, { before: engine.base, after: engineHead, ref: 'refs/heads/dev' }, 'push');
  assert.equal(enginePush.native, true);
  assert.equal(enginePush.native_arm64, true);
  const docs = repo(t), docsHead = docs.commit('ROADMAP.md');
  const docsPush = plan(docs, { before: docs.base, after: docsHead, ref: 'refs/heads/dev' }, 'push');
  assert.equal(docsPush.native, false);
  assert.equal(docsPush.native_arm64, false);
  for (const [base, arm64] of [['release/v9', true], ['intg/batch-native', false], ['dev', false]]) {
    const r = repo(t), head = r.commit('crates/hyprial-core/src/lib.rs');
    const result = plan(r, pr(r, head, 'fix/engine', base));
    assert.equal(result.native, true, base);
    assert.equal(result.native_arm64, arm64, base);
  }
  const mainPr = repo(t);
  const mainResult = plan(mainPr, { pull_request: { base: { ref: 'main' } } });
  assert.equal(mainResult.native, true);
  assert.equal(mainResult.native_arm64, true);
  const mainPush = repo(t);
  assert.equal(plan(mainPush, { ref: 'refs/heads/main' }, 'push').native_arm64, true);
});

function titled(event, title, extra = {}) {
  return { ...event, pull_request: { ...event.pull_request, title, ...extra } };
}
const lightSuites = ['python', 'gui_integration', 'gui', 'browser', 'codex', 'package', 'sidecar', 'native', 'native_arm64', 'full'];

test('a WIP: pull request gets the light plan with no suite and says so', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  for (const title of ['WIP: daemon work', 'WIP:', 'WIP:no space']) {
    const result = plan(r, titled(pr(r, head), title));
    assert.equal(result.wip, true, title);
    assert.equal(result.light, true, title);
    for (const key of lightSuites) assert.equal(result[key], false, `${title}: ${key}`);
    assert.match(result.reason, /WIP pull request: light checks only/);
  }
  const engine = repo(t), engineHead = engine.commit('crates/hyprial-core/src/lib.rs');
  const engineWip = plan(engine, titled(pr(engine, engineHead), 'WIP: engine'));
  assert.equal(engineWip.native, false);
  assert.equal(engineWip.wip, true);
  const draft = plan(r, titled(pr(r, head), 'daemon work', { draft: true }));
  assert.equal(draft.wip, true);
  assert.match(draft.reason, /draft is true/);
});

test('WIP detection is case-sensitive on the prefix and only a prefix', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  for (const title of ['wip: x', 'Wip: x', '[WIP] x', ' WIP: x', 'fix: WIP: x', 'WIP x', 'daemon work', '']) {
    const result = plan(r, titled(pr(r, head), title));
    assert.equal(result.wip, false, JSON.stringify(title));
    assert.equal(result.full, true, JSON.stringify(title));
    assert.equal(result.python, true, JSON.stringify(title));
  }
  assert.equal(plan(r, titled(pr(r, head), 'daemon work', { draft: false })).wip, false);
  assert.equal(plan(r, pr(r, head)).wip, false);
});

test('removing WIP: from the title on an edited event plans the full set for the same head', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const wipRun = plan(r, { ...titled(pr(r, head), 'WIP: daemon'), action: 'synchronize' });
  assert.equal(wipRun.wip, true);
  const edited = { ...titled(pr(r, head), 'daemon'), action: 'edited', changes: { title: { from: 'WIP: daemon' } } };
  const full = plan(r, edited);
  assert.equal(full.wip, false);
  assert.equal(full.full, true);
  assert.equal(full.python, true);
  const bodyOnly = plan(r, { ...titled(pr(r, head), 'daemon'), action: 'edited', changes: { body: { from: 'old' } } });
  assert.equal(bodyOnly.wip, false);
  assert.equal(bodyOnly.full, true);
  const stillWip = plan(r, { ...titled(pr(r, head), 'WIP: daemon 2'), action: 'edited', changes: { title: { from: 'WIP: daemon' } } });
  assert.equal(stillWip.wip, true);
  assert.equal(stillWip.python, false);
});

test('WIP never applies to push, schedule, dispatch, main or release pull requests', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const push = plan(r, { before: r.base, after: head, ref: 'refs/heads/dev' }, 'push');
  assert.equal(push.wip, false);
  assert.equal(push.full, true);
  for (const eventName of ['workflow_dispatch', 'schedule']) assert.equal(plan(r, {}, eventName).wip, false, eventName);
  const release = plan(r, titled(pr(r, head, 'fix/x', 'release/v9'), 'WIP: x'));
  assert.equal(release.wip, false);
  assert.equal(release.full, true);
  assert.equal(release.native_arm64, true);
  const main = plan(r, titled({ pull_request: { base: { ref: 'main' } } }, 'WIP: x'));
  assert.equal(main.wip, false);
  assert.equal(main.full, true);
});

test('a WIP plan still refuses a checkout that is not the event head', t => {
  const r = repo(t);
  r.commit('src/hyprial/daemon/example.py');
  const event = titled(pr(r, 'a'.repeat(40)), 'WIP: x');
  assert.throws(() => plan(r, event), /CI_ROUTE: checkout mismatch/);
});

test('the planner CLI prints the WIP decision and writes the wip and native_arm64 outputs', t => {
  const r = repo(t), head = r.commit('src/hyprial/daemon/example.py');
  const script = new URL('./ci-plan.mjs', import.meta.url).pathname;
  for (const [title, wip] of [['WIP: x', 'true'], ['WIP:', 'true']]) {
    const eventFile = join(r.root, 'event.json'), outFile = join(r.root, 'out.txt');
    writeFileSync(eventFile, JSON.stringify({ ...titled(pr(r, head), title), action: 'edited' }));
    writeFileSync(outFile, '');
    const run = spawnSync(process.execPath, [script], {
      cwd: r.root, encoding: 'utf8',
      env: { ...process.env, GITHUB_EVENT_PATH: eventFile, GITHUB_EVENT_NAME: 'pull_request', GITHUB_OUTPUT: outFile, GITHUB_STEP_SUMMARY: '' },
    });
    assert.equal(run.status, 0, run.stderr);
    assert.match(run.stdout, new RegExp(`CI_WIP action=edited wip=${wip} `));
    const outputs = readFileSync(outFile, 'utf8');
    assert.match(outputs, new RegExp(`^wip=${wip}$`, 'm'));
    assert.match(outputs, /^native_arm64=false$/m);
  }
});

test('the --wip-only mode decides from the payload alone, with no checkout or comparison', t => {
  const root = mkdtempSync(join(tmpdir(), 'ci-wip-only-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const script = new URL('./ci-plan.mjs', import.meta.url).pathname;
  const base = { base: { ref: 'dev' }, head: { ref: 'feat/x', sha: 'a'.repeat(40) } };
  for (const [name, event, wip] of [
    ['pull_request', { action: 'opened', pull_request: { ...base, title: 'WIP: x' } }, 'true'],
    ['pull_request', { action: 'edited', pull_request: { ...base, title: 'x' } }, 'false'],
    ['pull_request', { action: 'opened', pull_request: { ...base, title: 'x', draft: true } }, 'true'],
    ['pull_request', { action: 'opened', pull_request: { ...base, base: { ref: 'release/v9' }, title: 'WIP: x' } }, 'false'],
    ['push', { ref: 'refs/heads/dev' }, 'false'],
    ['workflow_dispatch', {}, 'false'],
  ]) {
    const eventFile = join(root, 'event.json'), outFile = join(root, 'out.txt');
    writeFileSync(eventFile, JSON.stringify(event));
    writeFileSync(outFile, '');
    const run = spawnSync(process.execPath, [script, '--wip-only'], {
      cwd: root, encoding: 'utf8',
      env: { ...process.env, GITHUB_EVENT_PATH: eventFile, GITHUB_EVENT_NAME: name, GITHUB_OUTPUT: outFile },
    });
    assert.equal(run.status, 0, run.stderr);
    assert.match(run.stdout, new RegExp(`^CI_WIP action=${event.action ?? 'none'} wip=${wip} \\(`));
    assert.equal(readFileSync(outFile, 'utf8'), `wip=${wip}\n`);
  }
});

test('daemon onboarding paths select both Python and GUI suites on PRs', t => {
  for (const path of [
    'src/hyprial/daemon/impl/application/onboarding/account_lifecycle.py',
    'src/hyprial/daemon/impl/request_catalog/onboarding/handlers.py',
  ]) {
    assert.equal(component(path), 'gui', path);
    const r = repo(t), head = r.commit(path);
    const result = plan(r, pr(r, head));
    assert.equal(result.python, true, `${path}: python`);
    assert.equal(result.gui, true, `${path}: gui`);
    assert.equal(result.gui_integration, true, `${path}: gui_integration`);
    assert.equal(result.browser, true, `${path}: browser`);
  }
});

test('Windows portability and Node/pyright checker inputs select the Python gate', t => {
  for (const path of [
    'scripts/ci/check_windows_portability.py',
    'scripts/ci/windows_portability_semantics.py',
    'scripts/ci/windows-portability-baseline.json',
    'scripts/ci/check_node_platform_idioms.py',
    'scripts/ci/node-platform-idioms-baseline.json',
    'scripts/ci/check_pyright_windows.py',
  ]) {
    const r = repo(t), head = r.commit(path);
    const result = plan(r, pr(r, head));
    assert.equal(result.python, true, path);
    assert.equal(result.full, true, path);
  }
});
