"""Installed-wheel acceptance helpers (A-05).

The heavy end-to-end proof runs ``scripts/verify/verify_installed_baseline.py``
against a real non-editable wheel, a separate interpreter and a fresh isolated
daemon.  These tests keep the artifact predicates honest without building or
installing a wheel in the default suite.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from scripts.verify.verify_installed_baseline import (
    EXPECTED_METHODS,
    EXPECTED_PUBLIC_IMPORTS,
    PUBLIC_PROBE,
    READY_PROBE,
    canonical_sha256,
    check_contract,
    compare_installed_bytes,
    default_console,
    forbidden_payload,
    inspect_wheel,
    main,
    probe_payload_problems,
    sidecar_name,
    start_daemon,
    task_environment,
)

COMMIT = "1234567" + "0" * 33
VERSION = "0.5.5+g1234567"


def _write_wheel(
    path: Path,
    *,
    sidecar: bool = True,
    executable: bool = True,
    entry_point: bool = True,
    gui_payload: bool = False,
    wheel_metadata: bool = True,
    duplicate_wheel: bool = False,
    version: str = VERSION,
) -> Path:
    dist = path.parent / f"hyprial-{version}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: hyprial\n"
        f"Version: {version}\n"
    )
    (dist / "METADATA").write_text(metadata, encoding="utf-8")
    if wheel_metadata:
        (dist / "WHEEL").write_text(
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: false\n"
            "Tag: py3-none-testplatform\n",
            encoding="utf-8",
        )
    (dist / "entry_points.txt").write_text(
        "[console_scripts]\n"
        + ("hyprial = hyprial.cli:main\n" if entry_point else "other = other:main\n"),
        encoding="utf-8",
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "hyprial/identity/impl/agents/migration/_bindings.py",
            "# ordinary source with the _bin substring\n",
        )
        archive.writestr(
            "hyprial/identity/impl/device/machine_binding.py",
            "# ordinary source with the _bin substring\n",
        )
        if sidecar:
            info = zipfile.ZipInfo(f"hyprial/_bin/{sidecar_name()}")
            info.create_system = 3
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            archive.writestr(info, b"\xcf\xfa\xed\xfe" + b"\0" * 60)
        if gui_payload:
            archive.writestr("hyprial/_gui/scripts/start-gui.sh", "#!/bin/sh\n")
            archive.writestr("hyprial/gui-source.json", "{}")
        for name in ("METADATA", "WHEEL", "entry_points.txt"):
            if (dist / name).is_file():
                archive.writestr(f"{dist.name}/{name}", (dist / name).read_bytes())
        if duplicate_wheel:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(f"{dist.name}/WHEEL", (dist / "WHEEL").read_bytes())
    return path


def test_sidecar_name_is_platform_specific() -> None:
    assert sidecar_name("win32") == "hyprial-tailcat.exe"
    assert sidecar_name("darwin") == "hyprial-tailcat"
    assert sidecar_name("linux") == "hyprial-tailcat"


def test_payload_predicate_matches_exact_paths_not_substrings() -> None:
    ordinary = [
        "hyprial/daemon/impl/application/messaging/identity_binding.py",
        "hyprial/identity/impl/agents/migration/_bindings.py",
        "hyprial/identity/impl/agents/registry/config_binding/__init__.py",
        "hyprial/identity/impl/device/machine_binding.py",
        "hyprial/daemon/impl/org/network/binding.py",
        "hyprial/shell/impl/login/binding.py",
    ]
    assert forbidden_payload(ordinary) == []
    assert forbidden_payload(
        [*ordinary, "hyprial/_bin/hyprial-tailcat", "hyprial/_gui/", "hyprial/gui-source.json"]
    ) == ["hyprial/_bin/hyprial-tailcat", "hyprial/_gui/", "hyprial/gui-source.json"]


def test_canonical_hash_ignores_key_order_and_separators() -> None:
    first = {"openapi": "3.1.0", "paths": {"/b": {}, "/a": {}}}
    second = {"paths": {"/a": {}, "/b": {}}, "openapi": "3.1.0"}
    assert canonical_sha256(first) == canonical_sha256(second)
    assert canonical_sha256(first) != canonical_sha256({"openapi": "3.1.0"})


def test_inspect_wheel_accepts_a_stamped_standard_wheel(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "hyprial.whl")
    summary, problems = inspect_wheel(wheel, COMMIT)
    assert problems == []
    assert summary["version"] == VERSION
    assert summary["sidecarMembers"] == [f"hyprial/_bin/{sidecar_name()}"]
    assert summary["retiredPayload"] == []
    assert summary["wheelTags"] == ["py3-none-testplatform"]
    # Ordinary binding modules must not look like payload.
    assert summary["members"] == 6


def test_inspect_wheel_requires_the_exact_sidecar_and_entry_point(tmp_path: Path) -> None:
    missing = _write_wheel(tmp_path / "missing.whl", sidecar=False)
    _, missing_problems = inspect_wheel(missing, COMMIT)
    assert any("exact sidecar" in problem for problem in missing_problems)

    no_entry = _write_wheel(tmp_path / "no-entry.whl", entry_point=False)
    _, entry_problems = inspect_wheel(no_entry, COMMIT)
    assert any("console script" in problem for problem in entry_problems)


def test_inspect_wheel_rejects_retired_gui_and_non_executable_payload(tmp_path: Path) -> None:
    gui = _write_wheel(tmp_path / "gui.whl", gui_payload=True)
    _, gui_problems = inspect_wheel(gui, COMMIT)
    assert any("retired GUI payload" in problem for problem in gui_problems)

    plain = _write_wheel(tmp_path / "plain.whl", executable=False)
    _, plain_problems = inspect_wheel(plain, COMMIT)
    assert any("not executable" in problem for problem in plain_problems)


def test_inspect_wheel_requires_exactly_one_wheel_metadata(tmp_path: Path) -> None:
    missing = _write_wheel(tmp_path / "missing-wheel.whl", wheel_metadata=False)
    _, missing_problems = inspect_wheel(missing, COMMIT)
    assert any("expected one dist-info/WHEEL" in problem for problem in missing_problems)

    duplicate = _write_wheel(
        tmp_path / "duplicate-wheel.whl", duplicate_wheel=True
    )
    _, duplicate_problems = inspect_wheel(duplicate, COMMIT)
    assert any("expected one dist-info/WHEEL" in problem for problem in duplicate_problems)


def test_installed_bytes_are_bound_to_the_supplied_wheel(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "bytes.whl")
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)

    checked, problems = compare_installed_bytes(wheel, site)
    assert problems == []
    assert checked == 3

    damaged = site / "hyprial/identity/impl/agents/migration/_bindings.py"
    original = damaged.read_bytes()
    damaged.write_bytes(b"X" + original[1:])
    _, problems = compare_installed_bytes(wheel, site)
    assert any("installed bytes differ" in problem for problem in problems)


def test_task_environment_uses_task_owned_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/production-home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/production-config")
    monkeypatch.setenv("PYTHONPATH", "/source/src")
    monkeypatch.setenv("GIT_DIR", "/checkout/.git")
    env = task_environment(tmp_path)
    assert env["HOME"] == str(tmp_path / "user")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "config")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "data")
    assert env["XDG_STATE_HOME"] == str(tmp_path / "xdg-state")
    assert env["HYPRIAL_HOME"] == str(tmp_path / "home")
    assert env["HARNESS_STATE_DIR"] == str(tmp_path / "state")
    assert "PYTHONPATH" not in env
    assert "GIT_DIR" not in env


def test_main_requires_contract_and_golden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["--wheel", str(tmp_path / "x.whl"), "--expected-commit", COMMIT])
    error = capsys.readouterr().err
    assert "--contract" in error or "required" in error


def test_main_rejects_a_non_hex_expected_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--wheel",
                str(tmp_path / "x.whl"),
                "--expected-commit",
                "zzzzzzz",
                "--contract",
                str(tmp_path / "contract.json"),
                "--golden",
                str(tmp_path / "golden.json"),
            ]
        )
    assert "lowercase hex" in capsys.readouterr().err


def test_inspect_wheel_rejects_a_different_commit_stamp(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "stamp.whl", version="0.5.5+g7654321")
    summary, problems = inspect_wheel(wheel, COMMIT)
    assert summary["version"] == "0.5.5+g7654321"
    assert any("does not end with" in problem for problem in problems)


def test_inspect_wheel_accepts_the_four_segment_release_stamp(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "four.whl", version="0.5.5.1+g1234567")
    summary, problems = inspect_wheel(wheel, COMMIT)
    assert problems == []
    assert summary["version"] == "0.5.5.1+g1234567"


@pytest.mark.parametrize(
    "version",
    (
        "0.5.5.1",
        "0.5.5.1+g123456",
        "0.5.5.1+g12345678",
        "0.5.5.1+gzzzzzzz",
        "0.5.5.1+g1234567.1",
        "v0.5.5.1+g1234567",
    ),
)
def test_inspect_wheel_rejects_a_corrupted_stamp(tmp_path: Path, version: str) -> None:
    wheel = _write_wheel(tmp_path / "bad-stamp.whl", version=version)
    _, problems = inspect_wheel(wheel, COMMIT)
    assert any("not a stamped version" in problem for problem in problems)


def test_check_contract_uses_the_installed_openapi_hash(tmp_path: Path) -> None:
    document = {"openapi": "3.1.0", "paths": {"/daemon/request/link.list": {}}}
    contract = tmp_path / "openapi.json"
    contract.write_text(json.dumps(document, indent=2), encoding="utf-8")
    payload = {
        "openapiCanonicalSha256": canonical_sha256(document),
        "openapiPaths": ["/daemon/request/link.list"],
    }
    digest, problems = check_contract(payload, contract)
    assert problems == []
    assert digest == canonical_sha256(document)

    payload["openapiCanonicalSha256"] = "0" * 64
    _, problems = check_contract(payload, contract)
    assert any("!= contract" in problem for problem in problems)


def test_all_thirteen_daemon_cases_use_agent_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.verify import verify_installed_baseline as baseline

    calls: list[list[str]] = []

    def record_cli(_hyprial: str, argv: list[str], **_kwargs: object) -> dict:
        calls.append(argv)
        return {
            "argv": argv, "exitCode": 0, "stdout": "{}", "stderr": "",
            "stdoutBytes": b"{}", "stderrBytes": b"",
        }

    monkeypatch.setattr(baseline, "run_cli", record_cli)
    results, _ = baseline.daemon_cases("unused", tmp_path, {}, None)
    assert len(results) == len(calls) == 13
    assert all(argv[:2] == ["agent", "link"] for argv in calls), calls


def test_readiness_probe_uses_the_public_client() -> None:
    assert "from hyprial.kernel import daemon_request" in READY_PROBE
    assert 'daemon_request("ping"' in READY_PROBE


def _valid_probe_payload() -> dict[str, object]:
    return {
        "module": "/isolated/site-packages/hyprial/__init__.py",
        "moduleInsidePrefix": True,
        "cwd": "/tmp/a05-probe",
        "methods": sorted(EXPECTED_METHODS),
        "openapi": "3.1.0",
        "resource": "/isolated/site-packages/hyprial/_bin/hyprial-tailcat",
        "resourceMagic": True,
        "resourceExecutable": True,
        "entryPoint": [["hyprial", "hyprial.cli:main"]],
        "publicImports": sorted(EXPECTED_PUBLIC_IMPORTS),
    }


def test_public_probe_reports_an_omitted_idempotency_spec() -> None:
    source_root = Path("/source-checkout")
    payload = _valid_probe_payload()
    assert probe_payload_problems(payload, source_root) == []
    omitted = {
        **payload,
        "publicImports": sorted(EXPECTED_PUBLIC_IMPORTS - {"IdempotencySpec"}),
    }
    problems = probe_payload_problems(omitted, source_root)
    assert any("missing=['IdempotencySpec']" in problem for problem in problems)
    assert "IdempotencySpec" in PUBLIC_PROBE


@pytest.mark.skipif(os.name == "nt", reason="POSIX console-script fixture")
def test_default_console_keeps_the_uv_venv_bin_directory(tmp_path: Path) -> None:
    shared_bin = tmp_path / "uv-python" / "bin"
    shared_bin.mkdir(parents=True)
    shared_python = shared_bin / "python3.12"
    shared_python.write_text("", encoding="utf-8")

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(shared_python)
    venv_console = venv_bin / "hyprial"
    venv_console.write_text("#!/bin/sh\n", encoding="utf-8")

    # A uv venv's python is a symlink to the shared interpreter.  Resolving it
    # would select <uv-interpreter>/bin/hyprial, which does not exist.
    assert default_console(venv_python) == venv_console


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group fixture")
def test_start_daemon_reports_an_immediate_exit_with_the_log_tail(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "hyprial"
    executable.write_text(
        "#!/bin/sh\n"
        "echo 'daemon exploded: missing sidecar' >&2\n"
        "exit 17\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    root = tmp_path / "daemon-root"
    root.mkdir()
    env = task_environment(root)

    process, log, problems = start_daemon(
        str(executable), sys.executable, root, env, ready_timeout=0.2
    )
    try:
        assert process.returncode == 17
        assert problems == [
            "daemon exited 17 before readiness: daemon exploded: missing sidecar\n"
        ]
        # The caller owns the write-only log until it stops/closes the daemon.
        assert log is not None and not log.closed
    finally:
        if log is not None:
            log.close()
    assert (root / "daemon.log").read_text(encoding="utf-8").endswith(
        "daemon exploded: missing sidecar\n"
    )
