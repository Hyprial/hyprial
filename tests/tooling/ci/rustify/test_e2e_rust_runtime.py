"""Isolated E2E Rust preparation rejects unverified or incompatible tools."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/ci-ensure-rust.sh"
HOST = "x86_64-unknown-linux-gnu"


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)


@pytest.fixture
def tools(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    commands = tmp_path / "commands"
    executable(commands / "uname", '#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo x86_64; fi\n')
    seed = tmp_path / "seed"
    executable(seed / "bin/rustc", f'#!/bin/sh\nprintf "rustc 1.93.1 (fixture)\\nhost: {HOST}\\n"\n')
    executable(seed / "bin/cargo", '#!/bin/sh\necho "cargo 1.93.1 (fixture)"\n')
    return {
        "PATH": f"{commands}:/usr/bin:/bin",
        "HOME": str(home),
        "HYPRIAL_E2E_ROOT": str(tmp_path / "run"),
        "HYPRIAL_E2E_RUST_CACHE": str(tmp_path / "cache"),
        "HYPRIAL_E2E_RUST_SEED": str(seed),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "CARGO_HOME": str(tmp_path / "ambient-cargo"),
        "RUSTUP_HOME": str(tmp_path / "ambient-rustup"),
    }


def run(tools: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=tools, cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )


def published_environment(tools: dict[str, str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in Path(tools["GITHUB_ENV"]).read_text().splitlines())


def test_seed_supplies_real_child_paths_without_global_home_mutation(tools: dict[str, str]) -> None:
    for key in ("CARGO_HOME", "RUSTUP_HOME"):
        path = Path(tools[key])
        path.mkdir(mode=0o700)
        (path / "sentinel").write_text("leave unchanged")
    result = run(tools)
    assert result.returncode == 0, result.stderr
    published = published_environment(tools)
    for key in ("CARGO_HOME", "RUSTUP_HOME", "HYPRIAL_CARGO"):
        assert Path(published[key]).is_relative_to(Path(tools["HYPRIAL_E2E_ROOT"]))
    assert published["RUSTUP_TOOLCHAIN"] == "1.93.1"
    path = Path(tools["GITHUB_PATH"]).read_text().strip()
    child = subprocess.run(
        [published["HYPRIAL_CARGO"], "--version"],
        env={**tools, **published, "PATH": path + os.pathsep + tools["PATH"]},
        capture_output=True, text=True, timeout=5,
    )
    assert child.returncode == 0 and child.stdout.startswith("cargo 1.93.1 ")
    for key in ("CARGO_HOME", "RUSTUP_HOME"):
        assert list(Path(tools[key]).iterdir()) == [Path(tools[key]) / "sentinel"]
    assert not (Path(tools["HOME"]) / ".cargo").exists()
    assert not (Path(tools["HOME"]) / ".rustup").exists()


@pytest.mark.parametrize("case", ["missing-root", "global-cargo", "root-directory", "newline", "foreign-version", "native-skipped"])
def test_invalid_configuration_fails_before_announcing_tools(tools: dict[str, str], case: str) -> None:
    if case == "missing-root":
        tools.pop("HYPRIAL_E2E_ROOT")
    elif case == "global-cargo":
        tools["HYPRIAL_E2E_ROOT"] = str(Path(tools["HOME"]) / ".cargo")
    elif case == "root-directory":
        tools["HYPRIAL_E2E_ROOT"] = "/"
    elif case == "newline":
        tools["HYPRIAL_E2E_ROOT"] += "\nOTHER=value"
    elif case == "foreign-version":
        tools["RUST_VERSION"] = "stable"
    else:
        tools["HYPRIAL_BUILD_SKIP_NATIVE"] = "1"
    result = run(tools)
    assert result.returncode != 0
    assert "e2e-rust:" in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


@pytest.mark.parametrize("part", ["rustc-version", "cargo-version", "host"])
def test_seed_version_or_host_mismatch_is_refused(tools: dict[str, str], part: str) -> None:
    seed = Path(tools["HYPRIAL_E2E_RUST_SEED"])
    if part == "cargo-version":
        executable(seed / "bin/cargo", '#!/bin/sh\necho "cargo 1.92.0 (fixture)"\n')
    else:
        version = "1.92.0" if part == "rustc-version" else "1.93.1"
        host = "aarch64-apple-darwin" if part == "host" else HOST
        executable(seed / "bin/rustc", f'#!/bin/sh\nprintf "rustc {version} (fixture)\\nhost: {host}\\n"\n')
    result = run(tools)
    assert result.returncode != 0
    assert "e2e-rust:" in result.stderr
    assert part.split("-", 1)[0] in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_unknown_arguments_are_not_silently_accepted(tools: dict[str, str]) -> None:
    result = run(tools, "--skip-native")
    assert result.returncode != 0
    assert "takes no arguments" in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_bad_bootstrap_checksum_never_executes_or_publishes(tools: dict[str, str], tmp_path: Path) -> None:
    tools.pop("HYPRIAL_E2E_RUST_SEED")
    marker = tmp_path / "bootstrap-executed"
    payload = tmp_path / "untrusted-bootstrap"
    payload.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    commands = Path(tools["PATH"].split(":", 1)[0])
    executable(commands / "curl", f'''#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in --output|-o) out="$2"; shift 2 ;; *) shift ;; esac
done
cp {shlex.quote(str(payload))} "$out"
''')
    result = run(tools)
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()
    assert not marker.exists()
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_download_failure_is_not_a_ready_toolchain(tools: dict[str, str]) -> None:
    tools.pop("HYPRIAL_E2E_RUST_SEED")
    commands = Path(tools["PATH"].split(":", 1)[0])
    executable(commands / "curl", '#!/bin/sh\necho "fixture download refused" >&2\nexit 22\n')
    result = run(tools)
    assert result.returncode != 0
    assert "fixture download refused" in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_existing_manager_receives_only_run_owned_homes(tools: dict[str, str], tmp_path: Path) -> None:
    seed = Path(tools.pop("HYPRIAL_E2E_RUST_SEED"))
    manager = tmp_path / "manager"
    record = tmp_path / "manager-call"
    prefetch = tmp_path / "prefetch-call"
    commands = Path(tools["PATH"].split(":", 1)[0])
    executable(commands / "uv", f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(str(prefetch))}\n')
    executable(manager, f'''#!/bin/sh
if [ "$1" = --version ]; then
  test "${{RUSTUP_AUTO_INSTALL:-}}" = 0 && test -z "${{RUSTUP_TOOLCHAIN:-}}" || exit 7
  echo "rustup 1.28.2 (fixture)"; exit 0
fi
test -s {shlex.quote(str(prefetch))} || exit 8
printf '%s\\n' "$CARGO_HOME" "$RUSTUP_HOME" "$RUSTUP_TOOLCHAIN" "$*" > {shlex.quote(str(record))}
test "$1" = toolchain && test "$2" = install && test "$3" = 1.93.1 || exit 9
bin="$RUSTUP_HOME/toolchains/1.93.1-{HOST}/bin"
mkdir -p "$bin"
cp {shlex.quote(str(seed / 'bin/rustc'))} "$bin/rustc"
cp {shlex.quote(str(seed / 'bin/cargo'))} "$bin/cargo"
''')
    tools["HYPRIAL_RUSTUP"] = str(manager)
    result = run(tools)
    assert result.returncode == 0, result.stderr
    cargo, rustup, version, command = record.read_text().splitlines()
    assert Path(cargo).is_relative_to(Path(tools["HYPRIAL_E2E_ROOT"]))
    assert Path(rustup).is_relative_to(Path(tools["HYPRIAL_E2E_ROOT"]))
    assert version == "1.93.1" and command == "toolchain install 1.93.1 --profile minimal --no-self-update"
    assert not Path(tools["CARGO_HOME"]).exists()
    assert not Path(tools["RUSTUP_HOME"]).exists()
    prefetch_arguments = prefetch.read_text()
    assert "ci-rust-dist.py" in prefetch_arguments
    assert str(Path(rustup) / "downloads") in prefetch_arguments


def test_global_home_symlink_is_refused_without_writing_through(tools: dict[str, str], tmp_path: Path) -> None:
    global_home = Path(tools["HOME"]) / ".rustup"
    global_home.mkdir(mode=0o700)
    sentinel = global_home / "sentinel"
    sentinel.write_text("leave unchanged")
    link = tmp_path / "aliased-root"
    link.symlink_to(global_home, target_is_directory=True)
    tools["HYPRIAL_E2E_ROOT"] = str(link)
    result = run(tools)
    assert result.returncode != 0
    assert "global user home" in result.stderr
    assert list(global_home.iterdir()) == [sentinel]
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_nested_toolchain_symlink_cannot_create_directories_in_global_home(tools: dict[str, str]) -> None:
    global_home = Path(tools["HOME"]) / ".rustup"
    global_home.mkdir(mode=0o700)
    sentinel = global_home / "sentinel"
    sentinel.write_text("leave unchanged")
    root = Path(tools["HYPRIAL_E2E_ROOT"])
    root.mkdir()
    (root / "toolchain").symlink_to(global_home, target_is_directory=True)
    result = run(tools)
    assert result.returncode != 0
    assert "global user home" in result.stderr
    assert list(global_home.iterdir()) == [sentinel]
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_corrupt_cached_bootstrap_is_not_executed_or_replaced(tools: dict[str, str], tmp_path: Path) -> None:
    tools.pop("HYPRIAL_E2E_RUST_SEED")
    marker = tmp_path / "bootstrap-executed"
    archive = Path(tools["HYPRIAL_E2E_RUST_CACHE"]) / "bootstrap" / f"rustup-init-1.28.2-{HOST}"
    executable(archive, f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    commands = Path(tools["PATH"].split(":", 1)[0])
    download_marker = tmp_path / "unexpected-download"
    executable(commands / "curl", f"#!/bin/sh\ntouch {shlex.quote(str(download_marker))}\nexit 22\n")
    result = run(tools)
    assert result.returncode != 0
    assert "cached rustup-init checksum mismatch" in result.stderr
    assert not marker.exists() and not download_marker.exists()
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_nested_rustup_toolchains_symlink_cannot_escape_the_run(tools: dict[str, str], tmp_path: Path) -> None:
    external = tmp_path / "external-toolchains"
    external.mkdir()
    rustup = Path(tools["HYPRIAL_E2E_ROOT"]) / "toolchain/rust/rustup"
    rustup.mkdir(parents=True)
    (rustup / "toolchains").symlink_to(external, target_is_directory=True)
    result = run(tools)
    assert result.returncode != 0
    assert "inside this E2E root" in result.stderr
    assert list(external.iterdir()) == []
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_macos_workflow_prepares_tools_before_the_gate() -> None:
    workflow = yaml.safe_load((ROOT / ".forgejo/workflows/e2e-macos-isolated.yml").read_text())
    job = workflow["jobs"]["isolated-macos"]
    steps = job["steps"]
    rust = [i for i, step in enumerate(steps) if "scripts/ci/ci-ensure-rust.sh" in str(step.get("run", ""))]
    assert len(rust) == 1
    gate = next(i for i, step in enumerate(steps) if "run-isolated-gate.py" in str(step.get("run", "")))
    prepare = next(i for i, step in enumerate(steps) if "uv sync --locked" in str(step.get("run", "")))
    assert prepare < rust[0] < gate
    assert not steps[rust[0]].get("continue-on-error", False)
    assert "HYPRIAL_BUILD_SKIP_NATIVE" not in steps[rust[0]].get("env", {})
    assert job["timeout-minutes"] == 90
    assert steps[rust[0]]["timeout-minutes"] == 20


def test_wrong_manager_version_is_refused_before_install(tools: dict[str, str], tmp_path: Path) -> None:
    tools.pop("HYPRIAL_E2E_RUST_SEED")
    manager = tmp_path / "wrong-rustup"
    executable(manager, '#!/bin/sh\necho "rustup 1.29.0 (fixture)"\n')
    tools["HYPRIAL_RUSTUP"] = str(manager)
    result = run(tools)
    assert result.returncode != 0
    assert "rustup version must be exactly 1.28.2" in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_bootstrap_archive_symlink_is_refused(tools: dict[str, str], tmp_path: Path) -> None:
    tools.pop("HYPRIAL_E2E_RUST_SEED")
    target = tmp_path / "external-bootstrap"
    executable(target, '#!/bin/sh\nexit 0\n')
    archive = Path(tools["HYPRIAL_E2E_RUST_CACHE"]) / "bootstrap" / f"rustup-init-1.28.2-{HOST}"
    archive.parent.mkdir(parents=True)
    archive.symlink_to(target)
    result = run(tools)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


TOOLCHAIN_FIXTURE = f"1.93.1-{HOST}"


def retry_tools(tools: dict[str, str], tmp_path: Path, *, install_failures: int = 0, stage_failures: int = 0,
                curl_failures: int = 0) -> dict[str, Path]:
    tools.pop("HYPRIAL_E2E_RUST_SEED")
    tools["CI_RETRY_BACKOFF"] = "0 0"
    commands = Path(tools["PATH"].split(":", 1)[0])
    logs = {name: tmp_path / f"{name}.log" for name in ("rustup", "uv", "curl")}
    toolchain = Path(tools["HYPRIAL_E2E_ROOT"]) / "toolchain/rust/rustup/toolchains" / TOOLCHAIN_FIXTURE / "bin"
    executable(commands / "rustup", f'''#!/bin/sh
if [ "$1" = --version ]; then echo "rustup 1.28.2 (fixture)"; exit 0; fi
echo "$*" >> {shlex.quote(str(logs["rustup"]))}
count=$(wc -l < {shlex.quote(str(logs["rustup"]))})
if [ "$count" -le {install_failures} ]; then
  echo "error: could not download file from 'https://static.rust-lang.org/dist/channel-rust-1.93.1.toml.sha256'" >&2
  exit 1
fi
mkdir -p {shlex.quote(str(toolchain))}
printf '#!/bin/sh\\nprintf "rustc 1.93.1 (fixture)\\\\nhost: {HOST}\\\\n"\\n' > {shlex.quote(str(toolchain / "rustc"))}
printf '#!/bin/sh\\necho "cargo 1.93.1 (fixture)"\\n' > {shlex.quote(str(toolchain / "cargo"))}
chmod 700 {shlex.quote(str(toolchain / "rustc"))} {shlex.quote(str(toolchain / "cargo"))}
''')
    executable(commands / "uv", f'''#!/bin/sh
echo "$*" >> {shlex.quote(str(logs["uv"]))}
count=$(wc -l < {shlex.quote(str(logs["uv"]))})
if [ "$count" -le {stage_failures} ]; then echo "e2e-rust-dist: FAIL manifest-sha: official download failed (curl exit 35)" >&2; exit 1; fi
exit 0
''')
    executable(commands / "curl", f'''#!/bin/sh
printf '%s\\n' "$*" >> {shlex.quote(str(logs["curl"]))}
count=$(wc -l < {shlex.quote(str(logs["curl"]))})
if [ "$count" -le {curl_failures} ]; then exit 35; fi
while [ "$#" -gt 0 ]; do
  case "$1" in --output|-o) out="$2"; shift 2 ;; *) shift ;; esac
done
echo untrusted > "$out"
''')
    tools["HYPRIAL_RUSTUP"] = str(commands / "rustup")
    return logs


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_rustup_toolchain_install_retries_the_network_fetch_then_succeeds(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path, install_failures=2)
    result = run(tools)
    assert result.returncode == 0, result.stderr
    assert lines(logs["rustup"]) == ["toolchain install 1.93.1 --profile minimal --no-self-update"] * 3
    assert result.stderr.count("CI_RETRY label=rustup-toolchain-install attempt=1/3 status=failed rc=1") == 1
    assert result.stderr.count("CI_RETRY label=rustup-toolchain-install attempt=2/3 status=failed rc=1") == 1
    assert "CI_RETRY label=rustup-toolchain-install attempt=3/3 status=ok" in result.stderr
    assert "e2e-rust: ready version=1.93.1" in result.stdout


def test_rustup_toolchain_install_gives_up_after_three_attempts(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path, install_failures=99)
    result = run(tools)
    assert result.returncode != 0
    assert len(lines(logs["rustup"])) == 3
    assert "CI_RETRY label=rustup-toolchain-install exhausted attempts=3 rc=1" in result.stderr
    assert "e2e-rust: rustup-toolchain-install failed after 3 attempts" in result.stderr
    assert not Path(tools["GITHUB_ENV"]).exists()


def test_distribution_staging_is_retried_before_rustup_is_asked_to_install(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path, stage_failures=1)
    result = run(tools)
    assert result.returncode == 0, result.stderr
    assert len(lines(logs["uv"])) == 2
    assert all("ci-rust-dist.py" in line and "--version 1.93.1" in line for line in lines(logs["uv"]))
    assert "CI_RETRY label=rust-dist-staging attempt=1/3 status=failed rc=1" in result.stderr
    assert len(lines(logs["rustup"])) == 1


def test_exhausted_distribution_staging_never_reaches_rustup(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path, stage_failures=99)
    result = run(tools)
    assert result.returncode != 0
    assert len(lines(logs["uv"])) == 3
    assert lines(logs["rustup"]) == []
    assert "e2e-rust: rust-dist-staging failed after 3 attempts" in result.stderr


def test_bootstrap_download_is_retried_but_a_bad_checksum_is_not(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path, curl_failures=2)
    tools.pop("HYPRIAL_RUSTUP")
    (Path(tools["PATH"].split(":", 1)[0]) / "rustup").unlink()
    result = run(tools)
    assert result.returncode != 0
    assert len(lines(logs["curl"])) == 3
    assert "CI_RETRY label=rustup-init-download attempt=2/3 status=failed rc=35" in result.stderr
    assert "checksum mismatch; refusing execution" in result.stderr
    assert "exhausted" not in result.stderr


def test_bootstrap_download_gives_up_after_three_attempts(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path, curl_failures=99)
    tools.pop("HYPRIAL_RUSTUP")
    (Path(tools["PATH"].split(":", 1)[0]) / "rustup").unlink()
    result = run(tools)
    assert result.returncode != 0
    assert len(lines(logs["curl"])) == 3
    assert "official rustup-init download failed" in result.stderr


def test_a_wrong_manager_version_is_not_retried(tools: dict[str, str], tmp_path: Path) -> None:
    logs = retry_tools(tools, tmp_path)
    manager = tmp_path / "wrong-rustup"
    executable(manager, '#!/bin/sh\necho "rustup 1.29.0 (fixture)"\n')
    tools["HYPRIAL_RUSTUP"] = str(manager)
    result = run(tools)
    assert result.returncode != 0
    assert "CI_RETRY" not in result.stderr
    assert lines(logs["uv"]) == []
