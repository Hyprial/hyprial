"""Active GUI gates must execute the shipped package, not the retired tree."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[5]


def test_gui_workflow_commands_resolve_to_existing_packages_and_scripts():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    commands = '\n'.join(
        step.get('run', '')
        for job in workflow['jobs'].values() for step in job.get('steps', [])
    )
    matches = re.findall(r'npm --prefix (\S+) (ci|test|run)(?: ([\w:-]+))?', commands)
    assert matches, 'GUI workflow must run an actual package'
    for package, action, script in matches:
        manifest = ROOT / package / 'package.json'
        assert manifest.is_file(), f'active CI references absent package: {package}'
        data = json.loads(manifest.read_text())
        if action == 'ci':
            assert (manifest.parent / 'package-lock.json').is_file()
        else:
            assert (script if action == 'run' else action) in data['scripts']
    assert any(package == 'gui' and action == 'ci' for package, action, _ in matches)
    assert ('gui', 'run', 'typecheck') in matches
    assert ('gui', 'run', 'client:reference:check') in matches
    assert ('gui', 'run', 'client:fixture:check') in matches
    assert ('gui', 'test', '') in matches
    assert ('gui', 'run', 'build') in matches


def test_root_gui_gate_is_not_disabled_by_retirement():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    job = workflow['jobs']['gui-unit']
    assert 'if' not in job
    for step in job['steps']:
        if 'npm --prefix gui' in step.get('run', ''):
            assert step.get('if') == "success() && steps.scope.outputs.gui == 'true'"
            assert not step.get('continue-on-error', False)
    assert 'hyprial/_gui/release.json' not in (ROOT / '.forgejo/workflows/gate-gui.yml').read_text()


def test_gui_build_enforces_module_boundaries_and_artifact_for_every_gui_scope():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    steps = workflow['jobs']['gui-unit']['steps']
    build = next(step for step in steps if step['name'] == 'Build independent GUI')
    assert build['if'] == "success() && steps.scope.outputs.gui == 'true'"
    assert 'npm --prefix gui run build' in build['run']
    assert not build.get('continue-on-error', False)

    scripts = json.loads((ROOT / 'gui/package.json').read_text())['scripts']
    command = scripts['build']
    assert command.index('npm run check:module-boundaries') < command.index('vite build')
    assert command.index('vite build') < command.index('node scripts/verify-production-copy.mjs dist')


def test_gui_unit_builds_artifact_before_production_bundle_tests():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    steps = workflow['jobs']['gui-unit']['steps']
    names = [step['name'] for step in steps]
    assert names.index('Run independent GUI tests') < names.index('Build independent GUI')

    scripts = json.loads((ROOT / 'gui/package.json').read_text())['scripts']
    assert scripts['pretest'] == 'npm run build'
    assert 'vitest run' in scripts['test']


def test_gui_unit_provisions_chromium_before_the_unit_tests_and_fails_without_a_browser():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    steps = workflow['jobs']['gui-unit']['steps']
    names = [step['name'] for step in steps]
    provision = steps[names.index('Provision pinned Chromium for the browser gates')]
    tests = steps[names.index('Run independent GUI tests')]
    assert names.index('Install locked Node dependencies') < names.index(
        'Provision pinned Chromium for the browser gates'
    )
    assert names.index('Provision pinned Chromium for the browser gates') < names.index(
        'Run independent GUI tests'
    )
    assert 'scripts/ci/ci-playwright-cache.sh' in provision['run']
    assert provision['if'] == "success() && steps.scope.outputs.gui == 'true'"
    assert tests['env'] == {'CI': 'true'}
    assert names.count('Provision pinned Chromium for the browser gates') == 1


def test_gui_unit_guarantees_a_cjk_font_between_chromium_and_the_unit_tests():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    steps = workflow['jobs']['gui-unit']['steps']
    names = [step['name'] for step in steps]
    name = 'Ensure a CJK font and fontconfig for the browser screenshots'
    assert names.count(name) == 1
    assert names.index('Provision pinned Chromium for the browser gates') < names.index(name)
    assert names.index(name) < names.index('Run independent GUI tests')
    step = steps[names.index(name)]
    assert step['if'] == "success() && steps.scope.outputs.gui == 'true'"
    assert not step.get('continue-on-error', False)
    assert 'bash scripts/ci/ci-ensure-cjk-font.sh' in step['run']
    assert '''test -n "$(fc-list ':lang=zh')"''' in step['run']

    script = (ROOT / 'scripts/ci/ci-ensure-cjk-font.sh').read_text()
    assert "fc-list ':lang=zh'" in script
    assert 'apt-get' in script and 'install -y --no-install-recommends' in script
    assert 'fonts-wqy-zenhei' in script and 'fontconfig' in script
    assert script.index("if has_zh_font; then") < script.index('install -y')


def test_gui_unit_runs_the_host_conformance_suite_inside_the_unit_tests_against_the_python_host():
    workflow = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())
    steps = workflow['jobs']['gui-unit']['steps']
    names = [step['name'] for step in steps]
    assert 'Run GUI host conformance against the Python host' not in names
    run = 'Run independent GUI tests'
    assert names.count(run) == 1
    step = steps[names.index(run)]
    assert step['run'] == 'npm --prefix gui test'
    assert names.index('Sync Python environment for GUI integration tests') < names.index(run)

    package = json.loads((ROOT / 'gui/package.json').read_text())['scripts']
    assert not [name for name in package if name.startswith('test:host-conformance:')]
    assert 'start' not in package
    command = package['test:host-conformance']
    assert 'HYPRIAL_HOST_IMPL' not in command
    vitest = (ROOT / 'gui/vitest.config.ts').read_text()
    assert "tests/**/*.test.{ts,tsx,mjs}" in vitest
    for suite in ('host', 'contract-mock-loopback', 'real-daemon-loopback', 'session-contract'):
        assert f'tests/daemon-request-host/{suite}.test.ts' in command
        assert (ROOT / f'gui/tests/daemon-request-host/{suite}.test.ts').is_file()


def test_heavy_component_jobs_require_successful_plan_and_selected_component():
    """Verify gate-gui heavy job gating, driver selection and nightly native wiring."""
    gui = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())['jobs']
    for name, key in [('release-package', 'package')]:
        assert gui[name]['needs'] == ['gui-unit']
        assert gui[name]['if'] == f"needs.gui-unit.outputs.{key} == 'true'"
    assert 'needs' not in gui['gui-acceptance']
    steps = gui['gui-acceptance']['steps']
    rust_step = next(
        step for step in steps
        if step['name'] == 'Ensure pinned Rust 1.93.1 for nightly native acceptance'
    )
    assert rust_step['if'] == "success() && github.event_name == 'schedule' && steps.scope.outputs.gui == 'true'"
    build_native_step = next(
        step for step in steps
        if step['name'] == 'Build and verify native actor extension for nightly acceptance'
    )
    assert build_native_step['if'] == "success() && github.event_name == 'schedule' && steps.scope.outputs.gui == 'true'"
    assert 'scripts/build/build_native_engine.sh' in build_native_step['run']
    acceptance_run = next(
        step for step in steps
        if step['name'] == 'Run browser acceptance'
    )
    assert acceptance_run['if'] == "success() && steps.scope.outputs.gui == 'true'"
    assert 'HYPRIAL_ACCEPTANCE_DRIVERS' not in acceptance_run['env']
    assert (
        acceptance_run['env']['HYPRIAL_ACCEPTANCE_ACTOR_BACKEND']
        == "${{ github.event_name == 'schedule' && 'native' || 'reference-abi' }}"
    )
    assert set(gui) == {'gui-unit', 'gui-acceptance', 'release-package'}


def test_root_gui_and_source_lifecycle_do_not_depend_on_retired_inputs():
    jobs = yaml.safe_load((ROOT / '.forgejo/workflows/gate-gui.yml').read_text())['jobs']
    assert 'legacy' not in jobs['gui-unit']['outputs']
    steps = {step['name']: step for step in jobs['gui-unit']['steps']}
    for name in (
        'Install locked Node dependencies', 'Typecheck root GUI',
        'Run independent GUI tests', 'Build independent GUI',
    ):
        assert steps[name]['if'] == "success() && steps.scope.outputs.gui == 'true'"
        assert 'npm --prefix gui ' in steps[name]['run']
    source_mode = steps['Verify source-mode GUI lifecycle']
    assert source_mode['if'] == "success() && steps.scope.outputs.gui == 'true'"
    assert source_mode['env'] == {'NO_COLOR': '1', 'COLUMNS': '200'}
    assert 'tests/shell/gui/lifecycle/test_gui_signals.py' in source_mode['run']
    assert 'legacy' not in jobs['release-package']['if']
