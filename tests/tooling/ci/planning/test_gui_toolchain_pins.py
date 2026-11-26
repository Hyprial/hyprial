"""Pins on the GUI toolchain that the root GUI gate used to check next to its workflow.

The workflow half (which job runs which npm script under which condition) was
read from ``gate-gui.yml`` and retired with that file. What remains here are
properties of files that still exist:

* ``gui/package.json``: the build script runs the module-boundary check before
  the production bundle and verifies the copy afterwards, the unit test
  script builds first, and the host-conformance suite names the four
  daemon-request-host test files that must be on disk;
* ``scripts/ci/ci-ensure-cjk-font.sh``: a CJK font that is already installed
  short-circuits the install, and the install is explicit about the packages;
* ``gui/vitest.config.ts``: the include pattern that the suite files depend on.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HOST_CONFORMANCE_SUITES = ("host", "contract-mock-loopback", "real-daemon-loopback", "session-contract")


def gui_scripts() -> dict:
    return json.loads((ROOT / "gui/package.json").read_text())["scripts"]


def test_gui_build_enforces_module_boundaries_before_and_copy_checks_after_the_bundle():
    command = gui_scripts()["build"]
    assert command.index("npm run check:module-boundaries") < command.index("vite build")
    assert command.index("vite build") < command.index("node scripts/verify-production-copy.mjs dist")


def test_gui_unit_tests_build_first_and_run_vitest():
    scripts = gui_scripts()
    assert scripts["pretest"] == "npm run build"
    assert "vitest run" in scripts["test"]


def test_gui_host_conformance_names_the_four_daemon_request_host_suites_that_exist():
    scripts = gui_scripts()
    assert not [name for name in scripts if name.startswith("test:host-conformance:")]
    assert "start" not in scripts
    command = scripts["test:host-conformance"]
    assert "HYPRIAL_HOST_IMPL" not in command
    vitest = (ROOT / "gui/vitest.config.ts").read_text()
    assert "tests/**/*.test.{ts,tsx,mjs}" in vitest
    for suite in HOST_CONFORMANCE_SUITES:
        assert f"tests/daemon-request-host/{suite}.test.ts" in command, suite
        assert (ROOT / f"gui/tests/daemon-request-host/{suite}.test.ts").is_file(), suite


def test_cjk_font_script_installs_the_named_packages_only_when_no_cjk_font_is_present():
    script = (ROOT / "scripts/ci/ci-ensure-cjk-font.sh").read_text()
    assert "fc-list ':lang=zh'" in script
    assert "apt-get" in script and "install -y --no-install-recommends" in script
    assert "fonts-wqy-zenhei" in script and "fontconfig" in script
    assert script.index("if has_zh_font; then") < script.index("install -y")
