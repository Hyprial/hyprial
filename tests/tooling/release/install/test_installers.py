"""Public installer checks: loopback HTTP, fake wheels, never a real install."""
from __future__ import annotations

from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[4]
NEXT_STEP_HINT = 'Next: run "hyprial init", then "hyprial login".'
PLATFORMS = [
    ("Darwin", "arm64", "macosx_13_0_arm64"),
    ("Darwin", "x86_64", "macosx_13_0_x86_64"),
    ("Linux", "x86_64", "manylinux_2_17_x86_64.manylinux2014_x86_64"),
    ("Linux", "aarch64", "manylinux_2_17_aarch64.manylinux2014_aarch64"),
]


@pytest.fixture
def sandbox(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(web))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home), "TMPDIR": str(tmp_path),
        "UV_TOOL_DIR": str(home / "tools"), "UV_CACHE_DIR": str(home / "cache"),
        "UV_TOOL_BIN_DIR": str(home / "bin"),
        "HYPRIAL_DL_BASE": f"http://127.0.0.1:{server.server_port}",
        "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
        "RECORD": str(tmp_path / "argv"),
        "TEST_OS": "Linux", "TEST_ARCH": "x86_64",
    }
    for name, body in {
        "uname": 'case "$1" in -s) echo "$TEST_OS";; -m) echo "$TEST_ARCH";; esac',
        "uv": ('printf "%s\\n" "$@" >> "$RECORD"; '
               'if [ "$1 $2" = "tool list" ]; then '
               'echo "hyprial v${TEST_INSTALLED_VERSION:-0.0.1+gabcdef0}"; fi; '
               'exit "${UV_EXIT:-0}"'),
    }.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    yield web, env
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def wheel(web, tag=PLATFORMS[2][2], version="0.0.1+gabcdef0", track="stable", abi="py3-none"):
    directory = web / "wheel" / track
    directory.mkdir(parents=True, exist_ok=True)
    name = f"hyprial-{version}-{abi}-{tag}.whl"
    body = b"fake wheel, must only reach stub uv"
    (directory / name).write_bytes(body)
    return name, f"{sha256(body).hexdigest()}  {name}\n"


def run(env):
    return subprocess.run(["sh", str(ROOT / "install.sh")], env=env,
                          capture_output=True, text=True, timeout=15)


def sums(web, text, track="stable"):
    (web / "wheel" / track / "SHA256SUMS").write_bytes(text.encode())


@pytest.mark.parametrize("system,arch,tag", PLATFORMS)
def test_native_installer_selects_wheel_and_records_version(sandbox, system, arch, tag):
    web, env = sandbox
    env.update(TEST_OS=system, TEST_ARCH=arch)
    name, line = wheel(web, tag, abi="cp312-abi3")
    sums(web, line)
    result = run(env)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(env["HOME"]) / ".config/hyprial/install.json").read_text())
    assert receipt["install"]["selected_version"] == "0.0.1+gabcdef0"
    assert receipt["install"]["wheel_asset_name"] == name
    args = Path(env["RECORD"]).read_text().splitlines()
    assert "--python" in args and "3.12" in args
    assert "cpython>=3.12" not in args


@pytest.mark.parametrize("problem", ["ambiguous", "unsupported-abi", "unsafe-name", "hash"])
def test_native_installer_refuses_unsafe_selection(sandbox, problem):
    web, env = sandbox
    _, line = wheel(web, abi="cp312-abi3")
    if problem == "ambiguous":
        _, old = wheel(web)
        line += old
    elif problem == "unsupported-abi":
        line = line.replace("cp312-abi3", "cp313-abi3")
    elif problem == "unsafe-name":
        line = line.replace("  hyprial-", "  ../hyprial-")
    else:
        line = "0" * 64 + line[64:]
    sums(web, line)
    result = run(env)
    assert result.returncode != 0
    assert not Path(env["RECORD"]).exists()


@pytest.mark.parametrize("system,arch,tag", PLATFORMS)
@pytest.mark.parametrize("track", ["stable", "nightly", "internal"])
def test_verified_install_and_rerun(sandbox, system, arch, tag, track):
    web, env = sandbox
    env.update(TEST_OS=system, TEST_ARCH=arch)
    if track != "stable":
        env["HYPRIAL_TRACK"] = track
    name, line = wheel(web, tag, track=track)
    # A neighbouring platform must not match. CRLF is accepted.
    _, other = wheel(web, "win_amd64", track=track)
    sums(web, (line + other).replace("\n", "\r\n"), track)
    for _ in range(2):
        result = run(env)
        assert result.returncode == 0, result.stderr
        assert "0.0.1+gabcdef0" in result.stdout
        assert result.stdout.rstrip("\n").endswith(NEXT_STEP_HINT)
    args = Path(env["RECORD"]).read_text().splitlines()
    assert args[:3] == ["tool", "install", "--force"]
    assert Path(args[3]).name == name
    assert args[4:6] == ["tool", "list"]
    assert args[6:9] == args[:3]
    assert not Path(args[3]).exists(), "temporary wheel must be removed"
    receipt = json.loads((Path(env["HOME"]) / ".config/hyprial/install.json").read_text())
    assert receipt["schema"] == 1
    assert receipt["install"]["track"] == track
    assert receipt["install"]["installed_version"] == "0.0.1+gabcdef0"
    assert receipt["install"]["digest_verification"] == "pass"
    assert receipt["install"]["method"] == "wheel"
    assert receipt["install"]["wheel_asset_name"] == name


def test_download_encodes_local_version_plus(sandbox):
    web, env = sandbox
    _, line = wheel(web)
    sums(web, line)
    record = Path(env["TMPDIR"]) / "curl-argv"
    env["CURL_RECORD"] = str(record)
    stub = Path(env["PATH"].split(":")[0]) / "curl"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$CURL_RECORD"\nexec /usr/bin/curl "$@"\n')
    stub.chmod(0o755)
    result = run(env)
    assert result.returncode == 0, result.stderr
    urls = [arg for arg in record.read_text().splitlines() if arg.startswith("http:")]
    assert "%2B" in urls[-1] and "+" not in urls[-1]
    assert "[Uri]::EscapeDataString($wheel)" in (ROOT / "install.ps1").read_text()


@pytest.mark.parametrize("scenario,message", [
    ("missing", "SHA256SUMS missing for track stable (HTTP 404)"),
    ("hash", "SHA-256 mismatch"),
    ("two", "Expected exactly one wheel"),
    ("none", "Expected exactly one wheel"),
    ("unknown", "Unsupported platform"),
    ("bad-track", "Invalid HYPRIAL_TRACK"),
    ("missing-wheel", "Wheel download failed"),
    ("install-fails", "uv tool install failed"),
])
def test_fail_closed(sandbox, scenario, message):
    web, env = sandbox
    name, line = wheel(web)
    if scenario == "hash":
        line = "0" * 64 + line[64:]
    if scenario == "two":
        _, extra = wheel(web, version="0.0.2+gabcdef0")
        line += extra
    if scenario == "none":
        _, line = wheel(web, "win_amd64")
    if scenario == "unknown":
        env["TEST_ARCH"] = "riscv64"
    if scenario == "bad-track":
        env["HYPRIAL_TRACK"] = "stabel"
    if scenario == "missing-wheel":
        (web / "wheel/stable" / name).unlink()
    if scenario == "install-fails":
        env["UV_EXIT"] = "7"
    if scenario != "missing":
        sums(web, line)
    result = run(env)
    assert result.returncode != 0
    assert message in result.stderr
    if scenario != "install-fails":
        assert not Path(env["RECORD"]).exists()


@pytest.mark.parametrize("bootstrap_fails", [False, True])
def test_missing_uv_bootstraps_official_installer_with_notice(sandbox, bootstrap_fails):
    web, env = sandbox
    _, line = wheel(web)
    sums(web, line)
    bin_dir = Path(env["PATH"].split(":")[0])
    uv_stub = bin_dir / "uv-stub"
    (bin_dir / "uv").rename(uv_stub)
    installer = bin_dir / "fake-installer.sh"
    installer.write_text('#!/bin/sh\nexit 9\n' if bootstrap_fails else
                         '#!/bin/sh\nmkdir -p "$UV_UNMANAGED_INSTALL"\n'
                         'cp "$BOOTSTRAP_UV" "$UV_UNMANAGED_INSTALL/uv"\n')
    curl = bin_dir / "curl"
    real_curl = shutil.which("curl", path="/usr/bin:/bin")
    assert real_curl
    curl.write_text('#!/bin/sh\noutput=""; prev=""; url=""\n'
                    'for arg do [ "$prev" != --output ] || output=$arg; prev=$arg; url=$arg; done\n'
                    'if [ "$url" = https://astral.sh/uv/install.sh ]; then\n'
                    '  cp "$BOOTSTRAP_SCRIPT" "$output"\n'
                    f'else exec "{real_curl}" "$@"; fi\n')
    curl.chmod(0o755)
    env.update(BOOTSTRAP_SCRIPT=str(installer), BOOTSTRAP_UV=str(uv_stub))
    result = run(env)
    assert "Installing uv from https://astral.sh/uv/install.sh" in result.stdout
    assert str(Path(env["HOME"]) / ".local/bin") in result.stdout
    if bootstrap_fails:
        assert result.returncode != 0
        assert "uv bootstrap failed" in result.stderr
        assert not Path(env["RECORD"]).exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (Path(env["HOME"]) / ".local/bin/uv").exists()


def test_installed_version_must_match_verified_selection(sandbox):
    web, env = sandbox
    _, line = wheel(web)
    sums(web, line)
    env["TEST_INSTALLED_VERSION"] = "0.0.9"
    result = run(env)
    assert result.returncode != 0
    assert "Installed version mismatch" in result.stderr
    assert "Installed hyprial" not in result.stdout


def test_installers_print_next_step_hint_without_running_it():
    for name in ("install.sh", "install.ps1"):
        text = (ROOT / name).read_text()
        assert NEXT_STEP_HINT in text
        for line in text.splitlines():
            if NEXT_STEP_HINT in line:
                continue
            assert not re.search(r"hyprial(\.exe)?['\"]?\s+init\b", line), (name, line)


def test_posix_syntax():
    subprocess.run(["sh", "-n", str(ROOT / "install.sh")], check=True)


def test_powershell_static_contract():
    text = (ROOT / "install.ps1").read_text()
    for token in ["HYPRIAL_TRACK", "HYPRIAL_DL_BASE", "SHA256SUMS", "win_amd64",
                  "Get-FileHash", "SHA256", "tool install --force", "finally",
                  "SHA-256 mismatch", "Expected exactly one wheel", "$LASTEXITCODE",
                  "tool list", "ConvertTo-Json", "install.json", "installed_version",
                  "https://astral.sh/uv/install.ps1", "UV_UNMANAGED_INSTALL"]:
        assert token in text
    assert "Invoke-Expression" not in text
    assert "https://dl.hyprial.com" in text


def test_powershell_parse_when_available():
    # Availability is reported separately in evidence; not a skipped runtime claim.
    executable = shutil.which("pwsh")
    if executable is None:
        assert (ROOT / "install.ps1").exists()
        return
    subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command",
                    "$tokens=$null; $errors=$null; "
                    "[System.Management.Automation.Language.Parser]::ParseFile("
                    f"'{ROOT / 'install.ps1'}',[ref]$tokens,[ref]$errors) > $null; "
                    "if ($errors.Count) { $errors; exit 1 }"],
                   env={**os.environ, "HOME": "/tmp/hr-install-pwsh-parse"}, check=True)
