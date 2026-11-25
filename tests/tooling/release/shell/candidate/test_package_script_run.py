"""Run the whole package script in a throw-away repository with fake host tools.

Covers what a developer and the manual workflow rely on: the stages run in
order, the first failure stops the build path while collect still runs, the
summary never reports a skipped smoke as passed, ``--source-sha`` refuses a
different commit or a dirty tree, ``--tauri-cli-dir`` reuses only the exact
pinned version, and without it the tauri-cli directory is fresh and removed.
Everything that needs a real Mac (the real tools, a window server) is not
covered here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.tooling.release.shell.candidate.script_support import call, fake_log, make_repo, node_dir, run_script, tools

EXPECTED_ORDER = [
    "build-wheel.sh",
    "stage_runtime.py",
    "verify_runtime.py --target-staged",
    "tauri build",
    "codesign --verify",
    "verify_runtime.py --target-bundle",
    "smoke_bundle.py --stop sigterm",
    "smoke_bundle.py --stop kill9",
]


def _setup(tmp_path: Path):
    repo, head = make_repo(tmp_path)
    path = [node_dir(tmp_path / "keg", "v24.22.3"), tools(tmp_path)]
    return repo, head, path


def _classified(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        if line.startswith("build-wheel.sh"):
            found.append("build-wheel.sh")
        elif "stage_runtime.py" in line:
            found.append("stage_runtime.py")
        elif "verify_runtime.py" in line:
            found.append("verify_runtime.py --target-bundle" if ".app/Contents" in line else "verify_runtime.py --target-staged")
        elif line.startswith("cargo tauri build"):
            found.append("tauri build")
        elif line.startswith("codesign --verify"):
            found.append("codesign --verify")
        elif "smoke_bundle.py" in line:
            found.append("smoke_bundle.py --stop sigterm" if "--stop sigterm" in line else "smoke_bundle.py --stop kill9")
    return found


def _summary(stdout: str) -> dict[str, str]:
    block = stdout.split("==> summary", 1)[1]
    rows = {}
    for line in block.splitlines():
        match = re.match(r"^  (\w+)\s+(.+)$", line)
        if match is not None:
            rows[match.group(1)] = match.group(2)
    return rows


def _names(directory: Path) -> list[str]:
    return sorted(item.name for item in directory.iterdir())


def test_a_full_run_goes_through_the_stages_in_order_and_collects_the_candidate(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    result = run_script(repo, [], path)
    assert result.returncode == 0, result.stdout + result.stderr
    log = fake_log(repo)
    assert _classified(log) == EXPECTED_ORDER
    assert "tauri build in src-tauri with CI=true" in log
    assert any(line.startswith("cargo tauri build --bundles app,dmg --config ../../../packaging/clients/tauri.bundle.json -- --locked") for line in log)
    rows = _summary(result.stdout)
    assert set(rows.values()) == {"PASSED"} and "collect" in rows and len(rows) == 13
    out = repo / "dist" / "shell-macos"
    assert _names(out) == sorted(
        ["BUILD-INFO.txt", "HyprialOS_0.0.0_aarch64.dmg", "SHA256SUMS", "runtime-manifest.json", "smoke-kill9.json", "smoke-sigterm.json", "verify-bundle.json", "verify-staged.json"]
    )
    info = (out / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert f"commit: {head}" in info and "tree dirty: no" in info and "requested source sha: none" in info
    assert "uv=uv 0.11.7" in info and "cargo=cargo 1.93.1" in info and "node=v24.22.3" in info and "tauri-cli=pinned 2.12.2" in info


def test_the_first_failing_stage_stops_the_build_path_but_collect_runs(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_FAIL": "stage"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert _classified(fake_log(repo)) == ["build-wheel.sh", "stage_runtime.py"]
    rows = _summary(result.stdout)
    assert rows["stage"] == "FAILED"
    for later in ("verify_staged", "build_app", "verify_bundle", "smoke_sigterm", "smoke_kill9"):
        assert rows[later] == "NOT RUN", later
    assert rows["collect"] == "PASSED"
    out = repo / "dist" / "shell-macos"
    assert _names(out) == ["BUILD-INFO.txt", "NOT-BUILT.txt"]
    assert (out / "NOT-BUILT.txt").read_text(encoding="utf-8").splitlines()[0] == "no app or dmg was built; the first stage that did not succeed: stage (failure)"


def test_a_failed_smoke_fails_the_run_but_keeps_the_built_candidate(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_FAIL": "smoke_sigterm"})
    assert result.returncode == 1
    rows = _summary(result.stdout)
    assert rows["build_app"] == "PASSED" and rows["smoke_sigterm"] == "FAILED" and rows["smoke_kill9"] == "PASSED"
    out = repo / "dist" / "shell-macos"
    assert "HyprialOS_0.0.0_aarch64.dmg" in _names(out) and "SHA256SUMS" in _names(out)
    assert "NOT-BUILT.txt" not in _names(out)


def test_skip_smoke_is_reported_as_skipped_never_as_passed(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, ["--skip-smoke"], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _classified(fake_log(repo)) == EXPECTED_ORDER[:-2]
    rows = _summary(result.stdout)
    assert rows["smoke_sigterm"] == "SKIPPED (not a pass)" and rows["smoke_kill9"] == "SKIPPED (not a pass)"
    assert rows["build_app"] == "PASSED"
    assert "smoke-sigterm.json" not in _names(repo / "dist" / "shell-macos")


def test_a_host_that_is_not_macos_arm64_is_refused(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_UNAME_S": "Linux", "FAKE_UNAME_M": "x86_64"})
    assert result.returncode == 1
    assert "this script packages macOS arm64 only, host is Linux/x86_64" in result.stdout
    assert _classified(fake_log(repo)) == []
    assert _summary(result.stdout)["host"] == "FAILED"


def test_a_wrong_uv_cargo_or_two_wheels_stop_the_run(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_UV_VERSION": "0.11.70"})
    assert result.returncode == 1 and "unexpected uv: uv 0.11.70" in result.stdout
    assert _summary(result.stdout)["toolchain"] == "FAILED"
    result = run_script(repo, [], path, {"FAKE_CARGO_VERSION": "1.93.10"})
    assert result.returncode == 1 and "unexpected cargo: cargo 1.93.10" in result.stdout
    result = run_script(repo, [], path, {"FAKE_TWO_WHEELS": "1"})
    assert result.returncode == 1
    assert _summary(result.stdout)["wheel"] == "FAILED"


def test_the_uv_version_check_is_anchored(tmp_path: Path) -> None:
    cases = (
        ("uv 0.11.7 (a1b2c3d4e 2026-11-01 aarch64-apple-darwin)", True),
        ("uv 0.11.7", True),
        ("uv 0.11.70 (a1b2c3d4e 2026-11-01)", False),
        ("uv 10.11.7", False),
    )
    for index, (text, passes) in enumerate(cases):
        root = tmp_path / f"root{index}"
        root.mkdir()
        result = call(f"version_word_is {text!r} uv \"${{UV_VERSION}}\"", root / "work", [tools(root)])
        assert (result.returncode == 0) is passes, text


def test_source_sha_must_match_head_and_a_clean_tree(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    other = "0" * 40
    refused = run_script(repo, ["--source-sha", other], path)
    assert refused.returncode == 2 and f"HEAD {head} is not the requested source {other}" in refused.stderr
    assert _classified(fake_log(repo)) == []
    assert run_script(repo, ["--source-sha", head[:12]], path).returncode == 2
    assert run_script(repo, ["--source-sha", head.upper()], path).returncode == 2

    ok = run_script(repo, ["--source-sha", head], path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert f"requested source sha: {head}" in (repo / "dist" / "shell-macos" / "BUILD-INFO.txt").read_text(encoding="utf-8")

    (repo / "stray.txt").write_text("untracked\n", encoding="utf-8")
    dirty = run_script(repo, ["--source-sha", head], path)
    assert dirty.returncode == 2 and "the tree is not clean" in dirty.stderr


def test_a_dirty_tree_without_source_sha_builds_and_says_so(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    (repo / "stray.txt").write_text("untracked\n", encoding="utf-8")
    result = run_script(repo, [], path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"commit {head}, tree dirty: yes" in result.stdout
    info = (repo / "dist" / "shell-macos" / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert "tree dirty: yes" in info and f"commit: {head}" in info
    assert "commit " + head + ", tree dirty: yes" in info


def test_without_a_tauri_cli_dir_a_fresh_temporary_one_is_used_and_removed(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    for _ in range(2):
        result = run_script(repo, [], path)
        assert result.returncode == 0, result.stdout + result.stderr
    roots = [Path(line) for line in (repo.parent / "fake.log.roots").read_text(encoding="utf-8").splitlines()]
    assert len(roots) == 2 and roots[0] != roots[1]
    for root in roots:
        assert root.parent == repo.parent / "tmp" and root.name.startswith("tauri-cli.")
        assert not root.exists()


def test_the_tauri_cli_dir_is_reused_only_at_exactly_the_pinned_version(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    cli = tmp_path / "cli"
    first = run_script(repo, ["--tauri-cli-dir", str(cli), "--out", str(tmp_path / "out1")], path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (cli / "bin" / "cargo-tauri").exists()
    installs = [line for line in fake_log(repo) if line.startswith("cargo install")]
    assert installs == [f"cargo install tauri-cli --version 2.12.2 --locked --root {cli}"]

    second = run_script(repo, ["--tauri-cli-dir", str(cli), "--out", str(tmp_path / "out2")], path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "reusing tauri-cli 2.12.2" in second.stdout
    assert len([line for line in fake_log(repo) if line.startswith("cargo install")]) == len(installs)

    (cli / "bin" / "cargo-tauri").write_text(f"#!{path[1] / 'bash'}\necho 'tauri-cli 2.12.20'\n", encoding="utf-8")
    third = run_script(repo, ["--tauri-cli-dir", str(cli), "--out", str(tmp_path / "out3")], path)
    assert third.returncode == 0, third.stdout + third.stderr
    assert len([line for line in fake_log(repo) if line.startswith("cargo install")]) == len(installs) + 1
    assert cli.exists()


def test_out_dir_rules(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("keep\n", encoding="utf-8")
    refused = run_script(repo, ["--out", str(full)], path)
    assert refused.returncode == 2 and "is not empty" in refused.stderr
    assert (full / "keep.txt").exists() and _classified(fake_log(repo)) == []

    explicit = tmp_path / "explicit" / "candidate"
    assert run_script(repo, ["--out", str(explicit)], path).returncode == 0
    assert "SHA256SUMS" in _names(explicit)

    default = repo / "dist" / "shell-macos"
    default.mkdir(parents=True)
    (default / "stale.txt").write_text("stale\n", encoding="utf-8")
    assert run_script(repo, [], path).returncode == 0
    assert "stale.txt" not in _names(default)


def test_an_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, ["--publish"], path)
    assert result.returncode == 2 and "unknown option: --publish" in result.stderr


def test_the_script_signs_nothing_and_reads_no_secret_even_when_some_are_set(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"APPLE_SIGNING_IDENTITY": "x", "GITHUB_TOKEN": "t", "APPLE_PASSWORD": "p"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [line for line in fake_log(repo) if "--sign" in line or "notarytool" in line]
    out = repo / "dist" / "shell-macos"
    for item in out.iterdir():
        assert "APPLE_" not in item.read_text(encoding="utf-8", errors="ignore")


@pytest.mark.parametrize("key", ["verify_staged", "build_app", "verify_bundle", "smoke_kill9", "tauri_cli", "wheel"])
def test_every_required_stage_failure_exits_non_zero(tmp_path: Path, key: str) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_FAIL": key})
    assert result.returncode == 1, result.stdout + result.stderr
    assert _summary(result.stdout)[key] == "FAILED"
    assert "result: FAILED" in result.stdout
