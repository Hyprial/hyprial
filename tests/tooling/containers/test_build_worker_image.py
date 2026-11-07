"""Run the actual build script with fake uv/docker; never build an image.

The regression input http://127.0.0.1:7890 is from card c8e58c90. Other URL
shapes are explicit synthetic edge cases, not claimed live observations.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/build/docker/build-worker-image.sh'
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _run_script(
    tmp_path: Path,
    proxies: dict[str, str],
    version: str = "0.4.4",
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    """Run the real script against fake uv/docker tools; never build."""
    repo = tmp_path / "repo"
    script = repo / "scripts" / "build" / "docker" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    (repo / "src" / "hyprial").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    fake = f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["BUILD_TEST_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "args": args, "cwd": os.getcwd()}) + "\\n")
if name == "uv":
    if args == ["build", "--wheel"]:
        pass
    elif args == ["run", "python", "-c", "from hyprial import __version__; print(__version__)"]:
        print(os.environ["BUILD_TEST_VERSION"])
    else:
        raise SystemExit("unexpected uv argv")
elif name != "docker" or not args or args[0] != "build":
    raise SystemExit("unexpected build tool argv")
'''
    for name in ("uv", "docker"):
        tool = bin_dir / name
        tool.write_text(fake)
        tool.chmod(0o755)
    # A minimal environment prevents production proxy/credential/config reads.
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path),
        "BUILD_TEST_LOG": str(log),
        "BUILD_TEST_VERSION": version,
        **proxies,
    }
    result = subprocess.run(
        ["/bin/sh", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _build_args(tmp_path: Path, proxies: dict[str, str], version: str = "0.4.4") -> list[str]:
    result, calls = _run_script(tmp_path, proxies, version)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"built hyprial-worker:{version.replace('+', '-')}\n"
    assert result.stderr == ""
    repo = tmp_path / "repo"
    # uv build -> uv version -> docker build.  The retired GUI payload had its
    # own provenance stamp; the worker image no longer stages it.
    assert [call["name"] for call in calls] == ["uv", "uv", "docker"]
    assert all(call["cwd"] == str(repo) for call in calls)
    args = calls[-1]["args"]
    assert args[0] == "build"
    assert args[-5:] == ["-f", "scripts/build/docker/Dockerfile.hyprial-worker", "-t", f"hyprial-worker:{version.replace('+', '-')}", "."]
    return args[1:-5]


@pytest.mark.parametrize("var", PROXY_VARS)
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_proxy_build_args_are_rewritten(tmp_path: Path, var: str, host: str) -> None:
    args = _build_args(tmp_path, {var: f"http://{host}:7890"})
    assert args == ["--build-arg", f"{var}=http://host.docker.internal:7890"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost", "http://host.docker.internal"),
        ("localhost:7890", "host.docker.internal:7890"),
        ("socks5h://127.0.0.1:7890", "socks5h://host.docker.internal:7890"),
        ("http://fixture:secret@localhost:7890/a?x=1#part", "http://fixture:secret@host.docker.internal:7890/a?x=1#part"),
        ("http://[::1]:7890", "http://host.docker.internal:7890"),
        ("http://proxy.example:7890", "http://proxy.example:7890"),
        ("http://localhost.example:7890", "http://localhost.example:7890"),
        ("http://127.0.0.1.example:7890", "http://127.0.0.1.example:7890"),
        ("http://localhost@proxy.example:7890/127.0.0.1", "http://localhost@proxy.example:7890/127.0.0.1"),
        ("http://proxy.example:7890/a?arg=two words&glob=*", "http://proxy.example:7890/a?arg=two words&glob=*"),
    ],
)
def test_only_loopback_authority_is_rewritten(tmp_path: Path, value: str, expected: str) -> None:
    assert _build_args(tmp_path, {"http_proxy": value}) == ["--build-arg", f"http_proxy={expected}"]


def test_proxy_variables_are_independent_and_no_proxy_is_unchanged(tmp_path: Path) -> None:
    proxies = {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "http_proxy": "http://localhost:7891",
        "HTTPS_PROXY": "https://proxy.example:443",
        "https_proxy": "",
        "NO_PROXY": "localhost,127.0.0.1, .example, [::1]",
        "no_proxy": "localhost,127.0.0.1, .internal",
    }
    args = _build_args(tmp_path, proxies)
    assert args[::2] == ["--build-arg"] * 5
    assert set(args[1::2]) == {
        "HTTP_PROXY=http://host.docker.internal:7890",
        "http_proxy=http://host.docker.internal:7891",
        "HTTPS_PROXY=https://proxy.example:443",
        f"NO_PROXY={proxies['NO_PROXY']}",
        f"no_proxy={proxies['no_proxy']}",
    }


@pytest.mark.parametrize("var", ["NO_PROXY", "no_proxy"])
@pytest.mark.parametrize("value", ["localhost", "127.0.0.1"])
def test_single_loopback_bypass_entry_is_not_rewritten(
    tmp_path: Path, var: str, value: str
) -> None:
    assert _build_args(tmp_path, {var: value}) == ["--build-arg", f"{var}={value}"]


def test_no_proxy_environment_and_local_version_tag(tmp_path: Path) -> None:
    assert _build_args(tmp_path, {}, version="0.4.4+test") == []
