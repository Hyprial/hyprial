"""Static properties of scripts/build/package-macos-shell.sh.

The pins, the Node 24 rules, the order of the stages, the collect rules and
the "never signs, never publishes, reads no secret" rules used to be asserted
on the workflow's inline step scripts; they now live in this script, which the
workflow only calls. Behavioural checks with fake tools are in
``test_candidate_steps.py`` and ``test_package_script_run.py``.
"""

from __future__ import annotations

import re

from pathlib import Path

from tests.tooling.release.shell.candidate.script_support import SCRIPT, make_repo, run_script, tools

TEXT = SCRIPT.read_text(encoding="utf-8")
STAGES = ["source", "host", "node", "toolchain", "tauri_cli", "wheel", "stage", "verify_staged", "build_app", "verify_bundle", "smoke_sigterm", "smoke_kill9"]


def _code() -> str:
    return "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", TEXT, re.S | re.M)
    assert match is not None, name
    return match.group(1)


def test_the_pins_are_written_once_in_one_variable_block() -> None:
    block = TEXT.split("set -euo pipefail\n", 1)[1].split("ROOT=", 1)[0]
    assert re.findall(r'^([A-Z0-9_]+)="([^"]*)"$', block, re.M) == [
        ("UV_VERSION", "0.11.7"),
        ("CARGO_VERSION", "1.93.1"),
        ("TAURI_CLI_VERSION", "2.12.2"),
        ("NODE_MAJOR", "24"),
        ("PYTHON_VERSION", "3.12"),
        ("SHORT_TMP", "${SHORT_TMP:-/tmp}"),
        ("NODE24_BIN_CANDIDATES", "${NODE24_BIN_CANDIDATES:-/opt/homebrew/opt/node@24/bin}"),
    ]
    assert 'export UV_PYTHON="${PYTHON_VERSION}"' in block
    code = _code()
    assert code.count("0.11.7") == 1 and code.count("1.93.1") == 1 and code.count("2.12.2") == 1
    assert "--python 3.12" not in code and code.count('"3.12"') == 1


def test_the_script_is_bash_with_errexit_and_a_decision_block_in_the_header() -> None:
    assert TEXT.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in TEXT
    header = TEXT.split("\nset -euo pipefail\n", 1)[0]
    assert "2026-11-17" in header
    assert "automated packaging" in header.lower()


def test_no_comment_outside_the_file_header() -> None:
    lines = TEXT.splitlines()
    first_code = next(index for index, line in enumerate(lines) if line and not line.startswith("#"))
    assert [line for line in lines[first_code:] if line.lstrip().startswith("#")] == []


def test_tauri_cli_is_pinned_locked_and_not_installed_into_a_persistent_cache() -> None:
    code = _code()
    assert 'cargo install tauri-cli --version "${TAURI_CLI_VERSION}" --locked --root "${TAURI_CLI_DIR}"' in code
    assert "-- --locked" in code
    assert ".cache" not in code
    assert 'OWN_TAURI_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tauri-cli.XXXXXX")"' in code
    assert 'rm -rf "${OWN_TAURI_DIR}"' in _function("cleanup")
    assert "trap cleanup EXIT" in code


def test_stage_uses_the_persistent_cache_root_when_ci_provides_one() -> None:
    run = _function("stage_stage")
    assert 'cache_root="${CI_CACHE_ROOT:-${WORK}}"' in run
    assert '--cache-dir "${cache_root}/pbs-cache"' in run


def test_the_stages_of_the_plan_run_in_order() -> None:
    main = _function("main")
    sequence = re.findall(r"^\s+(?:build_stage|smoke_stage) (\w+)$", main, re.M)
    assert sequence == STAGES
    code = _code()
    order = [
        "scripts/build/build-wheel.sh",
        "packaging/clients/stage_runtime.py",
        'verify_runtime.py \\\n        --runtime clients/native/src-tauri/runtime',
        "cargo tauri build --bundles app,dmg --config ../../../packaging/clients/tauri.bundle.json",
        "codesign --verify --deep --strict",
        '--runtime "${APP}/Contents/Resources/runtime"',
        "--stop sigterm --second-start",
        "--stop kill9 --app-home",
        "SHA256SUMS",
    ]
    positions = [code.index(needle) for needle in order]
    assert positions == sorted(positions)
    assert main.index("==> collect") > main.index("smoke_stage smoke_kill9")


def test_the_build_path_names_the_stages_the_note_explains() -> None:
    assert 'BUILD_PATH="source host node toolchain tauri_cli wheel stage verify_staged build_app"' in TEXT
    assert 'ALL_STAGES="' + " ".join(STAGES) + ' collect"' in TEXT


def test_node_24_comes_from_path_or_the_listed_host_location_only() -> None:
    node = _function("stage_node")
    main = _function("main")
    assert 'NODE24_BIN_CANDIDATES="${NODE24_BIN_CANDIDATES:-/opt/homebrew/opt/node@24/bin}"' in TEXT
    assert main.index("build_stage host") < main.index("build_stage node") < main.index("build_stage toolchain")
    assert 'PATH="$(cat "${WORK}/node-bin"):${PATH}"' in main
    assert "GITHUB_PATH" not in _code()
    assert "brew install node@${NODE_MAJOR}" in node
    for line in _code().splitlines():
        if re.search(r"\bbrew\b", line):
            assert line.lstrip().startswith("echo "), line
    for forbidden in ("curl", "wget", "nvm", "fnm", "volta", "nodejs.org", "corepack"):
        assert forbidden not in node, forbidden
    assert re.findall(r"\bv\d+|node@\d+", node) == []
    assert set(re.findall(r"node@\d+", _code())) == {"node@24"}
    assert node.count("v${NODE_MAJOR}.*)") == 2
    assert "/usr/local/opt" not in _code()


def test_the_node_and_npm_the_wheel_build_uses_are_checked_in_a_later_stage() -> None:
    run = _function("stage_toolchain")
    assert "for tool in git uv cargo node npm codesign hdiutil; do" in run
    assert 'case "$(node --version)" in' in run
    assert "v${NODE_MAJOR}.*)" in run
    assert "npm version --json" in run
    assert "node:${NODE_MAJOR}.*)" in run
    assert "GITHUB_PATH" not in run


def test_collect_never_writes_an_empty_checksum_file_and_explains_a_missing_build() -> None:
    run = _function("stage_collect")
    assert "shasum -a 256 *.dmg" not in run
    assert 'if [ -f "${file}" ]; then' in run
    assert "write_not_built" in run and "NOT-BUILT.txt" in _function("write_not_built")
    assert 'echo "build succeeded but no dmg"' in run
    assert "2>/dev/null" not in run
    assert "|| true" not in run


def test_every_stage_runs_in_a_subshell_with_errexit_and_collect_always_runs() -> None:
    assert '( set -euo pipefail; "${fn}" )' in _function("run_stage")
    main = _function("main")
    assert "( set -euo pipefail; stage_collect )" in main
    assert main.index("( set -euo pipefail; stage_collect )") > main.index("smoke_stage smoke_kill9")
    assert "|| true" in _function("list_app_data")


def test_the_script_never_signs_notarizes_publishes_or_reads_a_secret() -> None:
    code = _code().lower()
    mentions = [line.strip() for line in _code().splitlines() if "APPLE_" in line]
    assert mentions == ["for name in $(compgen -v APPLE_) $(compgen -v TAURI_SIGNING_PRIVATE_KEY); do", "The script pins Python 3.12 itself (UV_PYTHON) and unsets every APPLE_* and"]
    assert "unset" in _function("clear_signing_variables")
    for forbidden in ("notarytool", "stapler", "altool", "security import", "create-keychain", "keychain-profile", "--sign", "secrets.", "github_token", "runner_temp"):
        assert forbidden not in code, forbidden
    for forbidden in ("publish_", "release create", "tea release", "gh release", "upload-release", "attach_wheel_assets", "git tag", "git push", "rclone", "aws s3", "hyprial_build_source_remote"):
        assert forbidden not in code, forbidden
    assert re.findall(r"https?://", code) == []
    assert "codesign --verify" in code and "codesign -dv" in code


def test_the_help_lists_every_option(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    result = run_script(repo, ["--help"], [tools(tmp_path)])
    assert result.returncode == 0, result.stderr
    for option in ("--out", "--tauri-cli-dir", "--skip-smoke", "--source-sha", "--help", "NODE24_BIN_CANDIDATES", "Exit status:", "UV_PYTHON", "APPLE_*"):
        assert option in result.stdout, option
    assert "dist/shell-macos" in result.stdout
