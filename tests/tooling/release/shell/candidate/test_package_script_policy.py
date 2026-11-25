"""Policy of the package script: Python pin, signing variables, exit codes, independent smokes, temp parents.

Mac verification 2026-11-18 found a uv whose default interpreter is a
free-threaded 3.14 failing the wheel stage (the job environment of the old
workflow carried ``UV_PYTHON`` and the script did not), plus small defects:
temp parents left behind by a failed verify or smoke stage, "skipped" worded
like ``--skip-smoke``'s SKIPPED, a wrong cookie file name in the app-data list,
signing variables not cleared, a refused input exiting like a build failure,
and a failing ``smoke_sigterm`` stopping ``smoke_kill9``. Everything here runs
the script against fake host tools (see ``script_support``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.release.shell.candidate.script_support import call, fake_log, make_repo, node_dir, run_script, tools
from tests.tooling.release.shell.candidate.test_package_script_run import EXPECTED_ORDER, _classified, _names, _summary


def _setup(tmp_path: Path):
    repo, head = make_repo(tmp_path)
    path = [node_dir(tmp_path / "keg", "v24.22.3"), tools(tmp_path)]
    return repo, head, path


def _env_log(repo: Path) -> list[str]:
    return (repo.parent / "fake.log.env").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("caller", [None, "3.14t", "3.13"])
def test_the_python_pin_is_exported_before_every_uv_call(tmp_path: Path, caller: str | None) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {} if caller is None else {"UV_PYTHON": caller})
    assert result.returncode == 0, result.stdout + result.stderr
    seen = [line for line in _env_log(repo) if line.startswith(("uv ", "build-wheel.sh "))]
    assert len(seen) >= 6 and {line.split()[1] for line in seen} == {"UV_PYTHON=3.12"}, seen
    assert "python (UV_PYTHON=3.12) /fake/uv/python3.12 3.12.9 gil-disabled=0" in result.stdout
    info = (repo / "dist" / "shell-macos" / "BUILD-INFO.txt").read_text(encoding="utf-8")
    assert "python=/fake/uv/python3.12 3.12.9 gil-disabled=0" in info


@pytest.mark.parametrize("interpreter", ["/fake/uv/python3.12t 3.12.9 gil-disabled=1", "/fake/uv/python3.13 3.13.1 gil-disabled=0", "/fake/uv/python3.14t 3.14.0 gil-disabled=1"])
def test_an_interpreter_that_is_not_a_gil_python_3_12_fails_the_toolchain_stage(tmp_path: Path, interpreter: str) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_PYTHON": interpreter})
    assert result.returncode == 1, result.stdout
    assert "uv selects an unsupported interpreter" in result.stdout
    assert _summary(result.stdout)["toolchain"] == "FAILED" and _summary(result.stdout)["wheel"] == "NOT RUN"


def test_signing_variables_are_not_visible_to_cargo_or_any_other_tool(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    variables = {
        "APPLE_SIGNING_IDENTITY": "secret-identity",
        "APPLE_CERTIFICATE": "secret-certificate",
        "APPLE_ID": "secret-id",
        "APPLE_ANYTHING_ELSE": "secret-other",
        "TAURI_SIGNING_PRIVATE_KEY": "secret-key",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "secret-key-password",
    }
    result = run_script(repo, [], path, variables)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _env_log(repo)
    assert any(line.startswith("cargo ") for line in lines)
    assert all(line.endswith("visible=") for line in lines), lines
    for name in variables:
        assert name in result.stdout
    for value in variables.values():
        assert value not in result.stdout + result.stderr
        for item in (repo / "dist" / "shell-macos").iterdir():
            assert value not in item.read_text(encoding="utf-8", errors="ignore")


def test_without_signing_variables_nothing_is_announced(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path)
    assert result.returncode == 0 and "signing variables cleared" not in result.stdout


def test_exit_status_is_0_for_success_1_for_a_failed_stage_and_2_for_usage_or_refused_input(tmp_path: Path) -> None:
    repo, head, path = _setup(tmp_path)
    stale = repo / "dist" / "shell-macos"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("stale\n", encoding="utf-8")
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("keep\n", encoding="utf-8")

    for args in (["--publish"], ["--source-sha"], ["--source-sha", "0" * 40], ["--source-sha", "xyz"], ["--source-sha", head.upper()], ["--out", str(full)]):
        refused = run_script(repo, args, path)
        assert refused.returncode == 2, (args, refused.stdout, refused.stderr)
        assert "==> source" not in refused.stdout and _classified(fake_log(repo)) == []
    assert (stale / "stale.txt").exists() and (full / "keep.txt").exists()

    (repo / "stray.txt").write_text("untracked\n", encoding="utf-8")
    assert run_script(repo, ["--source-sha", head], path).returncode == 2
    (repo / "stray.txt").unlink()

    for key in ("wheel", "smoke_kill9", "verify_staged"):
        assert run_script(repo, [], path, {"FAKE_FAIL": key}).returncode == 1, key
    assert run_script(repo, [], path).returncode == 0


def test_the_help_documents_every_exit_code(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    text = run_script(repo, ["--help"], [tools(tmp_path)]).stdout
    section = text.split("Exit status:", 1)[1]
    assert "0  every required stage passed" in section and "1  a build or verification stage failed" in section
    assert "2  a usage error or a refused input" in section and "non-empty --out" in section


def test_both_smokes_run_when_the_first_fails_and_collect_still_runs(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    for failing, other in (("smoke_sigterm", "smoke_kill9"), ("smoke_kill9", "smoke_sigterm")):
        result = run_script(repo, ["--out", str(tmp_path / f"out-{failing}")], path, {"FAKE_FAIL": failing})
        assert result.returncode == 1, result.stdout + result.stderr
        assert _classified(fake_log(repo))[-2:] == EXPECTED_ORDER[-2:]
        rows = _summary(result.stdout)
        assert rows[failing] == "FAILED" and rows[other] == "PASSED" and rows["collect"] == "PASSED"
        assert "result: FAILED" in result.stdout
        assert "dmg" in " ".join(_names(tmp_path / f"out-{failing}"))
        (repo.parent / "fake.log").write_text("", encoding="utf-8")


def test_both_smokes_are_not_run_when_the_bundle_check_failed(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    result = run_script(repo, [], path, {"FAKE_FAIL": "verify_bundle"})
    assert result.returncode == 1
    rows = _summary(result.stdout)
    assert rows["smoke_sigterm"] == "NOT RUN" and rows["smoke_kill9"] == "NOT RUN"
    assert [line for line in _classified(fake_log(repo)) if line.startswith("smoke_bundle")] == []


def test_the_stage_line_says_not_run_or_skipped_like_the_summary(tmp_path: Path) -> None:
    repo, _, path = _setup(tmp_path)
    failed = run_script(repo, [], path, {"FAKE_FAIL": "stage"})
    assert "==> verify_staged (NOT RUN: an earlier stage failed)" in failed.stdout
    assert "skipped" not in failed.stdout
    skipped = run_script(repo, ["--skip-smoke"], path)
    assert "==> smoke_sigterm (SKIPPED: --skip-smoke)" in skipped.stdout
    assert "NOT RUN" not in skipped.stdout


@pytest.mark.parametrize("failing", ["verify_staged", "verify_bundle", "smoke_sigterm", "smoke_kill9", None])
def test_the_temp_parents_of_the_verify_and_smoke_stages_are_removed_on_every_path(tmp_path: Path, failing: str | None) -> None:
    repo, _, path = _setup(tmp_path)
    short = tmp_path / "short"
    short.mkdir()
    env = {"SHORT_TMP": str(short)}
    if failing is not None:
        env["FAKE_FAIL"] = failing
    result = run_script(repo, [], path, env)
    assert result.returncode == (1 if failing else 0), result.stdout + result.stderr
    parents = (repo.parent / "fake.log.parents").read_text(encoding="utf-8").split()
    assert parents and all(Path(item).parent == short for item in parents) and {Path(item).name[:3] for item in parents} <= {"hsv", "hsm"}
    assert list(short.iterdir()) == []


def test_the_app_data_list_names_the_real_cookie_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    library = home / "Library"
    for entry in ("WebKit/com.hyprial.hyprialos", "Caches/com.hyprial.hyprialos", "Logs/com.hyprial.hyprialos", "Application Support/com.hyprial.hyprialos"):
        (library / entry).mkdir(parents=True)
    (library / "HTTPStorages").mkdir()
    (library / "HTTPStorages" / "com.hyprial.hyprialos.binarycookies").write_text("cookies", encoding="utf-8")
    (library / "HTTPStorages" / "com.hyprial.hyprialos").mkdir()
    result = call("list_app_data", tmp_path / "work", [tools(tmp_path)], {"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    listed = [line.split()[-1] for line in result.stdout.splitlines()]
    assert f"{library}/HTTPStorages/com.hyprial.hyprialos.binarycookies" in listed
    assert f"{library}/HTTPStorages/com.hyprial.hyprialos" not in listed
    assert len(listed) == 5
