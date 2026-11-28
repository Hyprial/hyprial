// Dev-first policy is defined in .github/git-flow.json. Every source branch
// may target dev; suites follow the complete diff rather than its branch name.
// Exact checkout/comparison validation and optional integration lanes remain.
// Allen 2026-11-03: the component-source rule (gui/sidecar changes must come
// from intg/<name>*) and the cli-command-surface carve-out that served it are
// removed; comparison/verification safety and component suite selection stay.
// Allen 2026-11-10: the Python suite binds to the product tree only
// (src/hyprial/): tests, scripts and Python config (pyproject.toml, uv.lock)
// no longer select it. Shared CI/unknown inputs keep full and the wide gates
// but no longer force Python, and the retired package gate stays selected
// only when a branch restores the src/gui tree; schedule/dispatch, main and
// the approved deep-refactor lane keep full Python.
// Allen 2026-11-14: queue time is the constraint, so the widening is narrow.
// Besides the product tree the Python suite is selected by what decides
// WHICH tests run and WITH WHAT: Python files under tests/, the dependency
// pins (pyproject.toml, uv.lock), and the suite's own runner (pytest_impact.py,
// pytest-durations.json, gate-python.yml). #2757 changed the shard planner
// and the workflow and merged with every pytest job skipped, on the PR and on
// dev. pytest_impact.py keeps the cost down: a changed test module that no
// other test refers to runs alone.
// Owner 2026-11-15 (CI load): two event rules live here and nowhere else.
// (1) native_arm64: the emulated linux/arm64 and the single macOS arm64 lanes
// of native-conformance are selected only when the native scope is selected
// AND the event is not a pull_request (dev push, schedule, manual dispatch) or
// the pull request targets main or release/** (the release gate needs arm64).
// (2) wip: a pull_request whose title starts with the case-sensitive prefix
// `WIP:` (or whose payload carries draft=true) gets the light plan, wip=true
// and no suite. Case-sensitive on purpose: Forgejo matches WIP prefixes
// case-insensitively and also knows `[WIP]`, so every title Forgejo treats as
// WIP and we do not recognise just gets the full plan (safe direction). PRs
// into main or release/** are never WIP-light. The decision is read from the
// event payload of THIS run, so removing the prefix (an `edited` event) plans
// the full set for the same head; an edit that keeps the WIP state replans the
// same way as any other event (no skip, because a skipped re-run could
// overwrite the earlier run's statuses for the same head).
// R4 (2026-11-21): daemon onboarding application and request_catalog paths
// participate in the 'gui' component scope so onboarding changes trigger
// gate-gui (including gui-acceptance) on PRs alongside gate-python.
import { execFileSync } from 'node:child_process';
import { appendFileSync, existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { changedPaths, isDocumentation,
  ComparisonError, comparisonInitialDepth, comparisonMaxDepth } from './ci-scope.mjs';
import { readGitFlow } from './git-flow.mjs';

const suiteKeys = ['python', 'gui_integration', 'gui', 'browser', 'codex', 'package', 'sidecar', 'native'];
export const outputKeys = [...suiteKeys, 'full', 'light', 'wip', 'native_arm64'];
export const wipPrefix = 'WIP:';
const guiBranch = ref => typeof ref === 'string' && ref.startsWith('intg/gui');
const sidecarBranch = ref => typeof ref === 'string' && ref.startsWith('intg/sidecar');
const batchBranch = ref => typeof ref === 'string' && /^intg\/batch-.+/.test(ref);
const pythonRunnerInputs = new Set([
  'pyproject.toml', 'uv.lock', 'scripts/ci/pytest_impact.py', 'scripts/ci/pytest-durations.json',
  'scripts/ci/check_windows_portability.py', 'scripts/ci/windows_portability_semantics.py',
  'scripts/ci/windows-portability-baseline.json', 'scripts/ci/check_node_platform_idioms.py',
  'scripts/ci/node-platform-idioms-baseline.json', 'scripts/ci/check_pyright_windows.py',
  '.github/workflows/gate-python.yml',
]);
const pythonDocumentInputs = new Set([
  'GLOSSARY.md', 'docs/domains/squire/SKILL-template.md', 'docs/guides/cutover-runbook.md',
]);
const pythonInput = path => path.startsWith('src/hyprial/') || pythonRunnerInputs.has(path)
  || pythonDocumentInputs.has(path) || (path.startsWith('tests/') && path.endsWith('.py'));
const nativeExactInputs = new Set([
  'pyproject.toml', 'uv.lock',
  'docs/design/actor-engine-native-abi-spec-2026-11-01.md',
  'docs/design/actor-engine-native-abi-spec-clarifications-2026-11-03.md',
  'docs/design/actor-rustify-build-distribution-adr-2026-10-24.md',
  'src/hyprial/kernel/interface.py', 'src/hyprial/kernel/__init__.py',
  'src/hyprial/shell/impl/packaging/native_engine.py', 'src/hyprial/gui_build_hook.py',
  'src/hyprial/shell/impl/packaging/native_editable.py',
  'scripts/build/build_native_engine.sh', 'scripts/build/manylinux_native_evidence.sh',
  'scripts/build/build-wheel.sh', 'scripts/ci/ci-ensure-rust.sh', 'scripts/ci/ci-ensure-uv.sh',
  'scripts/ci/ci-ensure-cpython-mirror.sh', 'scripts/ci/ci-uv-cache.sh',
  '.github/workflows/native-conformance.yml', '.github/workflows/gate-python.yml',
  '.github/workflows/kp027-native-candidate.yml', '.github/workflows/wheel-release.yml',
]);
export const nativeInput = path => path.startsWith('crates/') || path.startsWith('conformance/')
  || path.startsWith('src/hyprial/_core/') || path.startsWith('src/hyprial/kernel/impl/actor_runtime/')
  || nativeExactInputs.has(path);
// The approved PAC replacement spans daemon/CLI and the workflow workbench.
const pacBranch = ref => ref === 'intg/pac-v2-full-impl';
// The approved deep-module integration combines GUI, Python and sidecar work.
// Match the exact branch, not a prefix, and retain comparison/checkout checks.
const deepRefactorBranch = 'refactor/reconstruct-modules';
const prose = path => isDocumentation(path.startsWith('src/gui/') ? path.slice(8) : path.startsWith('gui/') ? path.slice(4) : path);
export function infrastructure(path) {
  return path.startsWith('.github/') || path.startsWith('scripts/ci/') || /^scripts\/(ci-|pytest)/.test(path)
    || /^tests\/test_(ci_|pytest_)/.test(path) || path === 'tests/test_gui_ci_migration.py'
    || path.startsWith('tests/tooling/ci/') || path.startsWith('tests/tooling/pytest/')
    || path.startsWith('tests/shell/gui/ci/')
    || /^src\/gui\/scripts\/ci-/.test(path);
}
export function component(path) {
  if (prose(path) || infrastructure(path)) return null;
  if (path.startsWith('gui/') || path.startsWith('src/gui/') || path.startsWith('src/hyprial/gui/') || /^src\/hyprial\/gui[^/]*\.py$/.test(path)
      || path.startsWith('src/hyprial/shell/impl/gui/')
      || path.startsWith('src/hyprial/daemon/impl/application/onboarding/')
      || path.startsWith('src/hyprial/daemon/impl/request_catalog/onboarding/')
      || path.startsWith('tests/gui/') || path.startsWith('tests/shell/gui/') || path.startsWith('tests/integration/gui/')
      || path.startsWith('tests/support/gui_home/')
      || /^tests\/test_gui_/.test(path)) return 'gui';
  if (path.startsWith('sidecar/')
      || /^tests\/test_(sidecar|forwarding_sidecar)/.test(path)
      || /^tests\/daemon\/forwarding\/sidecar\/test_(?:sidecar|forwarding_sidecar)/.test(path)
      || ['tests/fixtures/fake_forwarding_sidecar.py', 'tests/fixtures/network/fake_forwarding_sidecar.py'].includes(path)) return 'sidecar';
  return null;
}

function gates(value, reason, { full = value, light = false } = {}) {
  return { ...Object.fromEntries(suiteKeys.map(key => [key, value])), full, light, reason };
}
function gitRaw(root, ...args) {
  return execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 60000 });
}
function git(root, ...args) { return gitRaw(root, ...args).trim(); }

function ancestor(root, older, newer) {
  try { git(root, 'merge-base', '--is-ancestor', older, newer); return true; }
  catch (error) {
    if (error.status === 1) return false;
    throw new ComparisonError('comparison ancestor check failed');
  }
}

function fetchIntegration(root, after, depth) {
  try {
    git(root, 'fetch', '--no-tags', `--depth=${depth}`, 'origin',
      '+refs/heads/dev:refs/remotes/origin/dev', after);
  } catch {
    throw new ComparisonError(`comparison fetch failed at depth ${depth}`);
  }
}

function independentDevReference(root, after) {
  let candidates;
  try {
    candidates = git(root, 'rev-list', '--first-parent', '--max-count=128', 'refs/remotes/origin/dev').split('\n');
  } catch {
    throw new ComparisonError('independent dev history unavailable');
  }
  const reference = candidates.find(sha => {
    // On a fast-forwarded first-parent history we cannot establish the old dev
    // boundary from Git alone. Refuse rather than trust branch-authored parents.
    if (sha === after) throw new ComparisonError('no independent dev boundary');
    return !ancestor(root, after, sha);
  });
  if (!reference) throw new ComparisonError('independent dev history unavailable');
  return reference;
}

function integrationMergeBase(root, reference, after) {
  try {
    return git(root, 'merge-base', reference, after);
  } catch (error) {
    if (error.status === 1) return null;
    throw new ComparisonError('comparison merge-base failed');
  }
}

function integrationComparison(event, root, fetch) {
  const after = event.after;
  if (!/^[a-f0-9]{40}$/.test(after ?? '') || /^0+$/.test(after)) throw new ComparisonError('invalid head');
  let depth = comparisonInitialDepth;
  if (fetch) fetchIntegration(root, after, depth);
  for (;;) {
    // A push range includes changes imported from dev. Route ownership instead
    // concerns the complete branch delta relative to an independent dev base.
    // Replaying after the branch landed must not select its own merge as a base:
    // that would make an illegal branch appear to have an empty diff.
    // Re-evaluate the boundary after each deepening: ancestor checks can only
    // become conclusive once the shallow window reaches the common history.
    const reference = independentDevReference(root, after);
    const base = integrationMergeBase(root, reference, after);
    if (base) {
      let paths;
      try {
        paths = gitRaw(root, 'diff', '--no-renames', '--name-only', '-z', base, after, '--').split('\0').filter(Boolean);
      } catch {
        throw new ComparisonError('comparison diff failed');
      }
      return { paths, comparisonBase: base, comparisonHead: after, comparisonKind: 'integration-vs-dev' };
    }
    if (!fetch || depth >= comparisonMaxDepth) {
      throw new ComparisonError(fetch
        ? `comparison merge base unavailable at depth ${depth}`
        : 'comparison merge base unavailable');
    }
    depth = Math.min(depth * 2, comparisonMaxDepth);
    fetchIntegration(root, after, depth);
  }
}

export function wipDecision(event, eventName) {
  if (eventName !== 'pull_request') return { wip: false, reason: 'not a pull_request event' };
  const pr = event?.pull_request ?? {};
  const target = pr.base?.ref;
  if (target === 'main' || (typeof target === 'string' && target.startsWith('release/'))) {
    return { wip: false, reason: `base ${target} is never WIP-light` };
  }
  if (typeof pr.title === 'string' && pr.title.startsWith(wipPrefix)) {
    return { wip: true, reason: `title starts with ${wipPrefix}` };
  }
  if (pr.draft === true) return { wip: true, reason: 'pull_request.draft is true' };
  return { wip: false, reason: `title does not start with ${wipPrefix} and draft is not true` };
}

export function makePlan(event, eventName, root, options = {}) {
  const plan = routePlan(event, eventName, root, options);
  const target = event?.pull_request?.base?.ref;
  const releaseTarget = target === 'main' || (typeof target === 'string' && target.startsWith('release/'));
  return { wip: false, ...plan, native_arm64: Boolean(plan.native) && (eventName !== 'pull_request' || releaseTarget) };
}

function routePlan(event, eventName, root, { fetch = true } = {}) {
  const policy = readGitFlow(root);
  const integrationBranch = policy.integration.branch;
  // The retired packaged GUI has no tree to package.  Component selection and
  // full/manual plans use the checked-out head's actual tree shape.
  const legacyGuiPresent = existsSync(join(root, 'src', 'gui'));
  if (['schedule', 'workflow_dispatch'].includes(eventName)) {
    return { ...gates(true, 'explicit full validation'), package: legacyGuiPresent };
  }
  if (eventName === 'push' && event.ref === 'refs/heads/main') {
    return { ...gates(true, 'main full validation'), package: legacyGuiPresent };
  }
  if (eventName === 'pull_request' && event.pull_request?.base?.ref === 'main') {
    return { ...gates(true, 'main release PR: full validation'), package: legacyGuiPresent };
  }
  const pr = event.pull_request;
  const head = eventName === 'pull_request' ? pr?.head?.ref : event.ref?.replace(/^refs\/heads\//, '');
  const base = eventName === 'pull_request' ? pr?.base?.ref : '';
  if (!['pull_request', 'push'].includes(eventName) || !head || (eventName === 'pull_request' && !base)) {
    throw new Error('CI_ROUTE: missing event/branch metadata; cannot verify integration route');
  }
  const wip = wipDecision(event, eventName);
  if (wip.wip) {
    if (git(root, 'rev-parse', 'HEAD') !== pr.head.sha) throw new Error('CI_ROUTE: checkout mismatch; refusing unverified WIP route');
    return { ...gates(false, `WIP pull request: light checks only (${wip.reason})`, { full: false, light: true }), wip: true };
  }
  let paths;
  let comparison = {};
  const integrationPush = eventName === 'push' && (guiBranch(head) || sidecarBranch(head) || pacBranch(head));
  try {
    if (integrationPush) {
      ({ paths, ...comparison } = integrationComparison(event, root, fetch));
    } else {
      paths = changedPaths(event, eventName, root, fetch);
    }
    const expected = eventName === 'pull_request' ? pr.head.sha : event.after;
    if (git(root, 'rev-parse', 'HEAD') !== expected) throw new ComparisonError('checkout mismatch');
  } catch (error) {
    // Do not print transport stderr/event payloads; those can contain secrets.
    // Only reasons minted above, or fixed local validation messages, are safe.
    const detail = error instanceof ComparisonError ? error.reason
      : ['missing comparison commit', 'manual or unsupported event'].includes(error?.message)
        ? error.message : 'comparison failed';
    if (eventName === 'pull_request') throw new Error(`CI_ROUTE: comparison unavailable (${detail}); refusing unverified PR route`);
    if (integrationPush) throw new Error(`CI_ROUTE: independent dev comparison unavailable (${detail}); refusing unverified integration push`);
    return { ...gates(true, 'push comparison unavailable: full validation'), package: legacyGuiPresent };
  }
  const deepRefactor = (eventName === 'pull_request'
    && (base === deepRefactorBranch || (head === deepRefactorBranch && base === integrationBranch)))
    || (eventName === 'push' && head === deepRefactorBranch);
  if (deepRefactor) return {
    ...gates(true, 'approved deep-module integration: full validation'),
    package: legacyGuiPresent,
    ...comparison,
  };
  const pac = pacBranch(head) || (eventName === 'pull_request' && base !== integrationBranch && pacBranch(base));
  const code = paths.filter(path => !prose(path));
  const shared = code.some(path => infrastructure(path)
    || (!component(path) && !pythonDocumentInputs.has(path)
      && !path.startsWith('src/hyprial/') && !path.startsWith('tests/')));
  const native = shared || paths.some(nativeInput)
    || (eventName === 'pull_request' && base.startsWith('release/'));
  if (eventName === 'pull_request' && batchBranch(base)) {
    return {
      ...gates(false, 'integration batch target: local gates supply test evidence', { full: false, light: true }),
      native,
      ...comparison,
    };
  }
  if (shared) return {
    ...gates(true, 'shared CI/build/unknown input: full validation'),
    package: legacyGuiPresent,
    python: paths.some(pythonInput),
    ...comparison,
  };
  const changed = name => paths.some(path => component(path) === name);
  const gui = guiBranch(head) || guiBranch(base) || pac || changed('gui');
  const sidecar = sidecarBranch(head) || sidecarBranch(base) || changed('sidecar');
  const devPush = eventName === 'push' && head === integrationBranch;
  return {
    ...gates(
      false,
      devPush ? 'dev landing: full validation and changed component suites'
        : code.length ? 'dev-first flow and audited change scope' : 'documentation-only',
      { full: devPush || code.length > 0 },
    ),
    python: paths.some(pythonInput),
    gui_integration: gui,
    gui, browser: gui, codex: gui, package: gui && legacyGuiPresent, sidecar, native,
    ...comparison,
  };
}

export function run() {
  try {
    const event = JSON.parse(readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
    if (process.argv.includes('--wip-only')) {
      const only = wipDecision(event, process.env.GITHUB_EVENT_NAME);
      console.log(`CI_WIP action=${event.action ?? 'none'} wip=${only.wip} (${only.reason})`);
      appendFileSync(process.env.GITHUB_OUTPUT, `wip=${only.wip}\n`);
      return;
    }
    const plan = makePlan(event, process.env.GITHUB_EVENT_NAME, process.cwd());
    const decision = wipDecision(event, process.env.GITHUB_EVENT_NAME);
    console.log(`CI_WIP action=${event.action ?? 'none'} wip=${plan.wip} reason=${decision.reason}`);
    console.log(`CI_PLAN ${JSON.stringify(plan)}`);
    for (const key of outputKeys) appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${plan[key]}\n`);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `\nCI plan: ${JSON.stringify(plan)}\n`);
  } catch (error) {
    console.error(error.message?.startsWith('CI_ROUTE:') ? error.message : 'CI_ROUTE: invalid planning input');
    process.exitCode = 1;
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) run();
