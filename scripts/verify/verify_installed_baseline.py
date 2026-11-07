#!/usr/bin/env python3
"""Accept an installed hyprial wheel outside every source checkout.

This is the A-05 reproduction gate for the public baseline B/C consume.  It
never imports ``hyprial`` in this process: the artifact under test is a
non-editable wheel installed into a separate interpreter, and every product
import runs there with ``python -I`` (no cwd, no ``PYTHONPATH``, no user site).

What is checked, in order:

1. wheel bytes and SHA-256, version stamped with the expected commit, console
   entry point, the standard ``hyprial/_bin`` sidecar payload, and absence of
   retired GUI payload names;
2. the installed distribution's import path, public kernel client/declaration
   surface, daemon catalogue/OpenAPI projection, and the sidecar package
   resource;
3. the installed ``hyprial`` console script (help/version) and a fresh
   isolated short-path daemon driven through the real public client;
4. the accepted link CLI bytes, including error codes, stderr and exit status.

The JSON summary on stdout is the machine-readable result.  A non-zero exit
means at least one check failed; ``--json-out`` and ``--artifacts-dir`` make
the run reproducible without hiding command output.

Usage example (run outside the checkout):

    uv run --no-project --python 3.12 python \
      scripts/verify/verify_installed_baseline.py \
      --wheel /tmp/a05-dist/hyprial-*.whl \
      --expected-commit <full sha> \
      --python /tmp/a05-installed-venv/bin/python \
      --hyprial /tmp/a05-installed-venv/bin/hyprial \
      --contract contract/daemon-request/openapi.json \
      --golden tests/integration/cli/link/golden.json \
      --artifacts-dir /tmp/a05-artifacts --json-out /tmp/a05-artifacts/result.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

EXPECTED_METHODS = frozenset({"link.add", "link.drop", "link.list", "link.stats"})
STAMPED_VERSION_RE = re.compile(r"\d+(?:\.\d+)*\+g[0-9a-f]{7}")
EXPECTED_PUBLIC_IMPORTS = frozenset({
    "DaemonRequestError", "IdempotencySpec", "MethodError", "MethodSpec",
    "PreviewSpec", "RequestPortError", "daemon_request", "daemon_request_once",
    "daemon_socket_path", "export_catalog", "export_openapi", "ipc_errors",
    "validate_method_specs",
})
OWNER = "a03-owner"
NODE = "a03-node"
ALICE = f"agent:{OWNER}:{NODE}:alice"
BOB = f"agent:{OWNER}:{NODE}:bob"
SUCCESS_CASES = (
    "list_text",
    "list_json",
    "stats_text",
    "stats_json",
    "add_text",
    "add_json",
    "drop_text",
    "drop_json",
)
ERROR_CASES = {
    "invalid_from": (["agent", "link", "list", "--from", "", "--json"], "INVALID_ARGUMENT"),
    "operator_without_agent": (
        ["agent", "link", "add", BOB, "--from", f"user:{OWNER}", "--json"],
        "CALLER_NOT_AUTHORIZED",
    ),
    "bare_agent_without_session": (
        ["agent", "link", "add", BOB, "--from", ALICE, "--json"],
        "SENDER_UNRESOLVED",
    ),
}
EMPTY_PEER_JSON = (
    ["agent", "link", "add", "", "--from", f"user:{OWNER}", "--on-behalf-of", ALICE, "--json"],
    b'{"ok":false,"code":"INVALID_ARGUMENT","error":"peer must be a non-empty string"}\n',
    b"",
)
EMPTY_PEER_TEXT = (
    ["agent", "link", "add", "", "--from", f"user:{OWNER}", "--on-behalf-of", ALICE],
    b"",
    b"hyprial: peer must be a non-empty string\n",
)

PUBLIC_PROBE = r'''
import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import sys
from pathlib import Path

import hyprial
from hyprial.kernel import (
    DaemonRequestError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    PreviewSpec,
    RequestPortError,
    daemon_request,
    daemon_request_once,
    daemon_socket_path,
    export_catalog,
    export_openapi,
    ipc_errors,
    validate_method_specs,
)
from hyprial.daemon import daemon_catalog, daemon_openapi

sidecar = sys.argv[1]
module = Path(hyprial.__file__).resolve()
prefix = Path(sys.prefix).resolve()
module_ok = module.is_relative_to(prefix) and "site-packages" in module.parts
cwd = Path(os.getcwd()).resolve()
direct = {}
dist = importlib.metadata.distribution("hyprial")
try:
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
except (FileNotFoundError, json.JSONDecodeError):
    direct = {}

catalog = daemon_catalog()
openapi = daemon_openapi()
methods = sorted(item["name"] for item in catalog["methods"])
canonical = json.dumps(
    openapi, sort_keys=True, separators=(",", ":"), ensure_ascii=False
).encode("utf-8")
catalog_canonical = json.dumps(
    catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=False
).encode("utf-8")

resource = importlib.resources.files("hyprial").joinpath("_bin", sidecar)
data = resource.read_bytes()
if sys.platform == "darwin":
    magic = data[:4] in {
        b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
        b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    }
elif sys.platform.startswith("linux"):
    magic = data[:4] == b"\x7fELF"
elif sys.platform.startswith("win"):
    magic = data[:2] == b"MZ"
else:
    magic = False

print(json.dumps({
    "module": str(module),
    "moduleInsidePrefix": module_ok,
    "prefix": str(prefix),
    "python": str(Path(sys.executable).resolve()),
    "sitePackages": str(Path(dist.locate_file("")).resolve()),
    "cwd": str(cwd),
    "version": dist.version,
    "directUrl": direct.get("url"),
    "entryPoint": [
        (point.name, point.value)
        for point in dist.entry_points
        if point.group == "console_scripts" and point.name == "hyprial"
    ],
    "methods": methods,
    "catalogCanonicalSha256": hashlib.sha256(catalog_canonical).hexdigest(),
    "openapi": openapi["openapi"],
    "openapiPaths": sorted(openapi["paths"]),
    "openapiCanonicalSha256": hashlib.sha256(canonical).hexdigest(),
    "resource": str(resource),
    "resourceBytes": len(data),
    "resourceExecutable": os.access(resource, os.X_OK),
    "resourceMagic": magic,
    "publicImports": sorted([
        "DaemonRequestError", "IdempotencySpec", "MethodError", "MethodSpec",
        "PreviewSpec", "RequestPortError", "daemon_request", "daemon_request_once",
        "daemon_socket_path", "export_catalog", "export_openapi", "ipc_errors",
        "validate_method_specs",
    ]),
}))
'''

READY_PROBE = r'''
import json
import os
import sys
import time
from pathlib import Path

from hyprial.kernel import daemon_request, ipc_errors

socket_path = Path(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
while time.monotonic() < deadline:
    try:
        result = daemon_request("ping", {}, socket_path=socket_path, timeout=5, restore_wait=5)
    except ipc_errors.DaemonUnavailableError:
        time.sleep(0.1)
        continue
    except ipc_errors.IpcTimeoutError:
        time.sleep(0.1)
        continue
    if result.get("running") is True and result.get("restorePending") is not True:
        print(json.dumps(result))
        raise SystemExit(0)
    time.sleep(0.1)
print("daemon did not become ready", file=sys.stderr)
raise SystemExit(1)
'''


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    body = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256_bytes(body)


def sidecar_name(platform: str | None = None) -> str:
    active = sys.platform if platform is None else platform
    return "hyprial-tailcat.exe" if active.startswith("win") else "hyprial-tailcat"


def default_console(python: str | Path) -> Path:
    """Return the console script next to the *venv* interpreter.

    A uv venv's ``bin/python`` is a symlink to the shared uv-managed
    interpreter.  ``Path(...).resolve()`` would therefore look in the shared
    interpreter's ``bin`` directory, where the venv's console script does not
    exist.  Keep the caller-visible interpreter path (making it absolute is
    fine) and select its sibling ``hyprial``.
    """
    interpreter = Path(python)
    if interpreter.parent == Path("."):
        located = shutil.which(str(python))
        if located is not None:
            interpreter = Path(located)
    return interpreter.absolute().parent / "hyprial"


def forbidden_payload(names: list[str]) -> list[str]:
    """Return payload paths that must not be in the installed baseline wheel.

    Ordinary modules such as ``identity_binding.py`` or ``_bindings.py`` are
    source, not payload; only the exact payload directories count.
    """
    return sorted(
        name
        for name in names
        if name.startswith("hyprial/_bin/")
        or name.startswith("hyprial/_gui/")
        or name == "hyprial/gui-source.json"
    )


def inspect_wheel(wheel: Path, expected_commit: str) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    if not wheel.is_file():
        return {}, [f"wheel is not a file: {wheel}"]
    expected_short = expected_commit[:7]
    expected_version = f"+g{expected_short}"
    expected_sidecar = f"hyprial/_bin/{sidecar_name()}"
    summary: dict[str, Any] = {
        "path": str(wheel),
        "sha256": sha256_file(wheel),
        "expectedCommit": expected_commit,
        "expectedSidecar": expected_sidecar,
    }
    try:
        archive = zipfile.ZipFile(wheel)
    except zipfile.BadZipFile as error:
        return summary, [f"wheel is not a zip archive: {error}"]

    with archive:
        names = archive.namelist()
        summary["members"] = len(names)
        retired_payload = sorted(
            name
            for name in names
            if name.startswith("hyprial/_gui/") or name == "hyprial/gui-source.json"
        )
        summary["retiredPayload"] = retired_payload
        if retired_payload:
            problems.append(f"wheel carries retired GUI payload paths: {retired_payload}")

        metadata_names = sorted(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(metadata_names) != 1:
            problems.append(f"expected one dist-info/METADATA, got {metadata_names}")
        else:
            metadata = archive.read(metadata_names[0]).decode("utf-8", "replace")
            match = re.search(r"^Version:\s*(\S+)\s*$", metadata, re.MULTILINE)
            version = match.group(1) if match else ""
            summary["version"] = version
            if not version.endswith(expected_version):
                problems.append(
                    f"wheel version {version!r} does not end with {expected_version!r}"
                )
            if not STAMPED_VERSION_RE.fullmatch(version):
                problems.append(f"wheel version {version!r} is not a stamped version")

        entry_names = sorted(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entry_text = ""
        if len(entry_names) != 1:
            problems.append(f"expected one entry_points.txt, got {entry_names}")
        else:
            entry_text = archive.read(entry_names[0]).decode("utf-8", "replace")
            summary["entryPoints"] = entry_text.strip()
            if "hyprial = hyprial.cli:main" not in entry_text.splitlines():
                problems.append("console script hyprial=hyprial.cli:main is absent")

        sidecar_members = sorted(
            name for name in names if name.startswith("hyprial/_bin/")
        )
        summary["sidecarMembers"] = sidecar_members
        if sidecar_members != [expected_sidecar]:
            problems.append(
                f"expected exact sidecar {expected_sidecar!r}, got {sidecar_members}"
            )
        elif archive.getinfo(expected_sidecar).file_size <= 0:
            problems.append(f"sidecar {expected_sidecar!r} is empty")
        else:
            mode = archive.getinfo(expected_sidecar).external_attr >> 16
            summary["sidecarMode"] = oct(mode & 0o777)
            if not mode & 0o111:
                problems.append(f"sidecar {expected_sidecar!r} is not executable")

        wheel_names = sorted(name for name in names if name.endswith(".dist-info/WHEEL"))
        if len(wheel_names) != 1:
            problems.append(f"expected one dist-info/WHEEL, got {wheel_names}")
        else:
            wheel_text = archive.read(wheel_names[0]).decode("utf-8", "replace")
            tags = re.findall(r"^Tag:\s*(\S+)\s*$", wheel_text, re.MULTILINE)
            summary["wheelTags"] = tags
            if not tags:
                problems.append("wheel metadata declares no Tag")
            elif all(tag.endswith("-none-any") for tag in tags):
                problems.append("native wheel is tagged as pure-Python none-any")

    if wheel.name.endswith(".whl"):
        summary["filename"] = wheel.name
    return summary, problems


def clean_environment() -> dict[str, str]:
    """A child environment with no inherited product/source/Git injection."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HYPRIAL_", "HARNESS_", "GIT_"))
    }
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
    ):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def task_environment(workdir: Path) -> dict[str, str]:
    """Task-owned HOME/XDG/HYPRIAL roots; never the user's real state."""
    roots = {
        "user": workdir / "user",
        "config": workdir / "config",
        "cache": workdir / "cache",
        "data": workdir / "data",
        "xdg-state": workdir / "xdg-state",
        "home": workdir / "home",
        "state": workdir / "state",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = clean_environment()
    env.update(
        {
            "HOME": str(roots["user"]),
            "XDG_CONFIG_HOME": str(roots["config"]),
            "XDG_CACHE_HOME": str(roots["cache"]),
            "XDG_DATA_HOME": str(roots["data"]),
            "XDG_STATE_HOME": str(roots["xdg-state"]),
            "HYPRIAL_HOME": str(roots["home"]),
            "HARNESS_STATE_DIR": str(roots["state"]),
        }
    )
    return env


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if stdin is None:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=stdin.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def public_probe(
    python: str,
    workdir: Path,
    expected_sidecar: str,
    source_root: Path,
    env: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    completed = run(
        [python, "-I", "-", expected_sidecar],
        cwd=workdir,
        env=env,
        timeout=120,
        stdin=PUBLIC_PROBE,
    )
    if completed.returncode != 0:
        return {}, [
            "public import probe failed with exit "
            f"{completed.returncode}: {completed.stderr.decode('utf-8', 'replace')[-2000:]}"
        ]
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except json.JSONDecodeError as error:
        return {}, [f"public import probe returned invalid JSON: {error}"]
    return payload, probe_payload_problems(payload, source_root)


def probe_payload_problems(payload: dict[str, Any], source_root: Path) -> list[str]:
    """Validate one installed public-probe payload against the documented surface."""
    problems: list[str] = []
    module = Path(str(payload.get("module", ""))).resolve()
    probe_cwd = Path(str(payload.get("cwd", ""))).resolve()
    if payload.get("moduleInsidePrefix") is not True:
        problems.append(
            f"hyprial imported outside the isolated prefix: {payload.get('module')}"
        )
    if module.is_relative_to(source_root):
        problems.append(f"hyprial imported from the source checkout: {module}")
    if probe_cwd.is_relative_to(source_root):
        problems.append(f"probe cwd is inside the source checkout: {probe_cwd}")
    public_imports = set(payload.get("publicImports", []))
    missing_imports = EXPECTED_PUBLIC_IMPORTS - public_imports
    extra_imports = public_imports - EXPECTED_PUBLIC_IMPORTS
    if missing_imports or extra_imports:
        problems.append(
            "public import probe does not match the documented surface: "
            f"missing={sorted(missing_imports)} extra={sorted(extra_imports)}"
        )
    declared = set(payload.get("methods", []))
    if not EXPECTED_METHODS.issubset(declared):
        problems.append(
            f"declared methods {sorted(declared)} miss {sorted(EXPECTED_METHODS - declared)}"
        )
    if payload.get("openapi") != "3.1.0":
        problems.append(f"OpenAPI version {payload.get('openapi')!r} != '3.1.0'")
    if payload.get("resourceMagic") is not True:
        problems.append(f"sidecar resource is not a native binary: {payload.get('resource')}")
    if payload.get("resourceExecutable") is not True:
        problems.append(f"sidecar resource is not executable: {payload.get('resource')}")
    if not payload.get("entryPoint"):
        problems.append("installed distribution has no hyprial console entry point")
    return problems


def check_contract(payload: dict[str, Any], contract: Path) -> tuple[int | str, list[str]]:
    if not contract.is_file():
        return 0, [f"contract snapshot is missing: {contract}"]
    document = json.loads(contract.read_text(encoding="utf-8"))
    expected = canonical_sha256(document)
    actual = payload.get("openapiCanonicalSha256")
    problems: list[str] = []
    if actual != expected:
        problems.append(
            f"installed OpenAPI canonical SHA-256 {actual} != contract {expected}"
        )
    if sorted(document.get("paths", {})) != sorted(payload.get("openapiPaths", [])):
        problems.append("installed OpenAPI path set does not match the contract snapshot")
    return expected, problems


def compare_installed_bytes(
    wheel: Path, site_packages: Path
) -> tuple[int, list[str]]:
    """Bind every installed ``hyprial/`` file to the supplied wheel bytes."""
    problems: list[str] = []
    checked = 0
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name.startswith("hyprial/") or name.endswith("/"):
                continue
            installed = site_packages / name
            if not installed.is_file():
                problems.append(f"installed artifact is missing {name}")
                continue
            if installed.stat().st_size != info.file_size:
                problems.append(
                    f"installed size mismatch for {name}: "
                    f"{installed.stat().st_size} != {info.file_size}"
                )
                continue
            if sha256_bytes(archive.read(info)) != sha256_file(installed):
                problems.append(f"installed bytes differ from the wheel for {name}")
                continue
            checked += 1
    if checked == 0:
        problems.append("no installed hyprial files were compared to the wheel")
    return checked, problems


def provenance_problems(
    payload: dict[str, Any], wheel: Path, hyprial: str
) -> list[str]:
    problems: list[str] = []
    prefix_value = payload.get("prefix")
    site_value = payload.get("sitePackages")
    if not prefix_value or not site_value:
        return ["installed probe did not report its prefix/site-packages"]
    prefix = Path(str(prefix_value)).resolve()
    site = Path(str(site_value)).resolve()
    if not site.is_relative_to(prefix):
        problems.append(f"site-packages {site} is outside prefix {prefix}")
    direct = str(payload.get("directUrl") or "")
    parsed = urlparse(direct)
    if parsed.scheme != "file":
        problems.append(f"installed direct URL is not a wheel file URL: {direct!r}")
    else:
        direct_path = Path(unquote(parsed.path)).resolve()
        if direct_path != wheel.resolve():
            problems.append(
                f"installed wheel {direct_path} is not the supplied wheel {wheel.resolve()}"
            )
    console = Path(hyprial).resolve()
    if not console.is_file():
        problems.append(f"console script is missing: {console}")
    elif not console.is_relative_to(prefix):
        problems.append(f"console script {console} is outside prefix {prefix}")
    elif os.name != "nt":
        first = console.open("rb").readline().decode("utf-8", "replace")
        shebang = Path(first[2:].strip()) if first.startswith("#!") else None
        # Resolve the venv's bin directory, not the interpreter symlink: a
        # uv venv's python points at the shared uv-managed interpreter.
        bin_dir = shebang.parent.resolve() if shebang is not None else None
        if (
            shebang is None
            or "python" not in shebang.name
            or bin_dir is None
            or bin_dir.parent != prefix
        ):
            problems.append(
                f"console script shebang does not name the isolated prefix: {first!r}"
            )
    return problems


def _log_tail(path: Path, limit: int = 2000) -> str:
    """Read the daemon log without using the write-only stdout handle."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError as error:
        return f"<cannot read daemon log: {error}>"


def _environment(root: Path) -> dict[str, str]:
    env = task_environment(root)
    (root / "home" / "settings.json").write_text(
        json.dumps({"version": 1, "owner": OWNER}), encoding="utf-8"
    )
    env.update(
        {
            "HOME": str(root / "user"),
            "HYPRIAL_HOME": str(root / "home"),
            "HARNESS_STATE_DIR": str(root / "state"),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": OWNER,
            "HYPRIAL_NODE_ID": NODE,
            "HYPRIAL_LINKS_BYPASS": "0",
        }
    )
    env.pop("HARNESS_SOCKET_PATH", None)
    return env


def start_daemon(
    hyprial: str,
    python: str,
    root: Path,
    env: dict[str, str],
    ready_timeout: float = 60.0,
) -> tuple[subprocess.Popen[bytes], Any, list[str]]:
    log_path = root / "daemon.log"
    log = log_path.open("wb")
    try:
        process = subprocess.Popen(
            [hyprial, "daemon", "run"],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        log.close()
        raise
    socket_path = Path(env["HARNESS_STATE_DIR"]) / "daemon.sock"
    early = None
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    else:
        early = process.returncode
    if early is not None:
        return process, log, [
            f"daemon exited {early} before readiness: {_log_tail(log_path)}"
        ]
    try:
        ready = run(
            [python, "-I", "-", str(socket_path), str(ready_timeout)],
            cwd=root,
            env=env,
            timeout=ready_timeout + 15,
            stdin=READY_PROBE,
        )
    except subprocess.TimeoutExpired as error:
        stop_daemon(process)
        log.close()
        return process, None, [f"daemon readiness probe timed out: {error}"]
    except BaseException:
        stop_daemon(process)
        log.close()
        raise
    if ready.returncode != 0 and process.poll() is None:
        # Give an exiting child a moment to be reaped so its exact code and
        # log tail replace the probe's generic failure below.
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is not None:
        return process, log, [
            f"daemon exited {process.returncode} before readiness: "
            f"{_log_tail(log_path)}"
        ]
    problems: list[str] = []
    if ready.returncode != 0:
        problems.append(
            "daemon readiness probe failed: "
            + ready.stderr.decode("utf-8", "replace")[-2000:]
        )
    return process, log, problems


def stop_daemon(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is not None:
        return process.returncode
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return process.returncode if process.returncode is not None else 0
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=30)
    return process.returncode if process.returncode is not None else 1


def run_cli(
    hyprial: str, argv: list[str], *, cwd: Path, env: dict[str, str]
) -> dict[str, Any]:
    completed = run([hyprial, *argv], cwd=cwd, env=env, timeout=120)
    return {
        "argv": argv,
        "exitCode": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", "replace"),
        "stderr": completed.stderr.decode("utf-8", "replace"),
        "stdoutBytes": completed.stdout,
        "stderrBytes": completed.stderr,
    }


def daemon_cases(
    hyprial: str,
    root: Path,
    env: dict[str, str],
    golden: Path | None,
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    results: dict[str, Any] = {}
    cases = {
        "list_text": ["agent", "link", "list", "--from", ALICE],
        "list_json": ["agent", "link", "list", "--from", ALICE, "--json"],
        "stats_text": ["agent", "link", "stats", "--from", ALICE],
        "stats_json": ["agent", "link", "stats", "--from", ALICE, "--json"],
        "add_text": ["agent", "link", "add", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE],
        "add_json": [
            "agent", "link", "add", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE, "--json",
        ],
        "drop_text": ["agent", "link", "drop", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE],
        "drop_json": [
            "agent", "link", "drop", BOB, "--from", f"user:{OWNER}", "--on-behalf-of", ALICE, "--json",
        ],
    }
    expected = json.loads(golden.read_text(encoding="utf-8")) if golden else {}
    if golden and set(expected) != set(SUCCESS_CASES):
        problems.append(f"golden case set {sorted(expected)} != {sorted(SUCCESS_CASES)}")
    for name in SUCCESS_CASES:
        actual = run_cli(hyprial, cases[name], cwd=root, env=env)
        results[name] = {
            "argv": actual["argv"],
            "exitCode": actual["exitCode"],
            "stdoutSha256": sha256_bytes(actual["stdoutBytes"]),
            "stderrSha256": sha256_bytes(actual["stderrBytes"]),
        }
        if actual["exitCode"] != 0:
            problems.append(f"{name}: exit {actual['exitCode']} != 0")
        if golden and name in expected:
            want = expected[name]
            if actual["exitCode"] != want["exitCode"]:
                problems.append(
                    f"{name}: exit {actual['exitCode']} != {want['exitCode']}"
                )
            if actual["stdoutBytes"] != want["stdout"].encode("utf-8"):
                problems.append(f"{name}: stdout bytes differ from golden")
            if actual["stderrBytes"] != want["stderr"].encode("utf-8"):
                problems.append(f"{name}: stderr bytes differ from golden")
        results[name]["stdout"] = actual["stdout"]
        results[name]["stderr"] = actual["stderr"]

    for name, (argv, code) in ERROR_CASES.items():
        actual = run_cli(hyprial, argv, cwd=root, env=env)
        results[name] = {
            "argv": argv,
            "exitCode": actual["exitCode"],
            "stdoutSha256": sha256_bytes(actual["stdoutBytes"]),
            "stderr": actual["stderr"],
            "stdout": actual["stdout"],
        }
        if actual["exitCode"] != 1:
            problems.append(f"{name}: exit {actual['exitCode']} != 1")
        if actual["stderr"] != "":
            problems.append(f"{name}: stderr is not empty: {actual['stderr']!r}")
        try:
            payload = json.loads(actual["stdout"])
        except json.JSONDecodeError:
            problems.append(f"{name}: stdout is not JSON: {actual['stdout']!r}")
            continue
        if payload.get("ok") is not False or payload.get("code") != code:
            problems.append(f"{name}: expected ok:false code {code}, got {payload}")
        if not isinstance(payload.get("error"), str) or not payload["error"]:
            problems.append(f"{name}: error is not a non-empty string")

    for name, (argv, stdout, stderr) in (
        ("empty_peer_json", EMPTY_PEER_JSON),
        ("empty_peer_text", EMPTY_PEER_TEXT),
    ):
        actual = run_cli(hyprial, argv, cwd=root, env=env)
        results[name] = {
            "argv": argv,
            "exitCode": actual["exitCode"],
            "stdout": actual["stdout"],
            "stderr": actual["stderr"],
        }
        if actual["exitCode"] != 1:
            problems.append(f"{name}: exit {actual['exitCode']} != 1")
        if actual["stdoutBytes"] != stdout:
            problems.append(f"{name}: stdout bytes differ from the pinned baseline")
        if actual["stderrBytes"] != stderr:
            problems.append(f"{name}: stderr bytes differ from the pinned baseline")
    return results, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--hyprial", default=None)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--artifacts-dir", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    if re.fullmatch(r"[0-9a-f]{7,40}", args.expected_commit) is None:
        parser.error("--expected-commit must be 7-40 lowercase hex characters")
    if not args.contract.is_file():
        parser.error(f"--contract is not a file: {args.contract}")
    if not args.golden.is_file():
        parser.error(f"--golden is not a file: {args.golden}")
    hyprial = args.hyprial or str(default_console(args.python))
    if not Path(hyprial).is_file():
        parser.error(f"hyprial console script is missing: {hyprial}")

    problems: list[str] = []
    summary: dict[str, Any] = {
        "ok": False,
        "wheel": {},
        "installed": {},
        "contract": {},
        "daemon": {},
        "cases": {},
        "problems": problems,
    }

    wheel_summary, wheel_problems = inspect_wheel(args.wheel, args.expected_commit)
    summary["wheel"] = wheel_summary
    problems.extend(wheel_problems)

    tmp_parent = args.work_dir
    if tmp_parent is None and os.name == "posix":
        tmp_parent = Path("/tmp")
    if tmp_parent is not None:
        tmp_parent.mkdir(parents=True, exist_ok=True)
    workspace = tempfile.mkdtemp(
        prefix="a05-installed-",
        dir=None if tmp_parent is None else str(tmp_parent),
    )
    workdir = Path(workspace)
    daemon_process: subprocess.Popen[bytes] | None = None
    daemon_log = None
    source_root = Path(__file__).resolve().parents[2]
    try:
        env = task_environment(workdir)
        payload, probe_problems = public_probe(
            args.python, workdir, sidecar_name(), source_root, env
        )
        summary["installed"] = payload
        problems.extend(probe_problems)
        if payload.get("version") != wheel_summary.get("version"):
            problems.append(
                f"installed version {payload.get('version')!r} != wheel {wheel_summary.get('version')!r}"
            )
        if payload.get("sitePackages"):
            checked, byte_problems = compare_installed_bytes(
                args.wheel, Path(str(payload["sitePackages"]))
            )
            summary["provenance"] = {"checkedMembers": checked}
            problems.extend(byte_problems)
        problems.extend(provenance_problems(payload, args.wheel, hyprial))
        contract_sha, contract_problems = check_contract(payload, args.contract)
        summary["contract"] = {
            "path": str(args.contract),
            "canonicalSha256": contract_sha,
        }
        problems.extend(contract_problems)

        help_run = run_cli(hyprial, ["help", "--json"], cwd=workdir, env=env)
        summary["console"] = {
            "helpExitCode": help_run["exitCode"],
            "helpStdoutSha256": sha256_bytes(help_run["stdoutBytes"]),
            "helpStderr": help_run["stderr"],
        }
        if help_run["exitCode"] != 0:
            problems.append(f"console help failed with exit {help_run['exitCode']}")
        else:
            try:
                help_payload = json.loads(help_run["stdout"])
            except json.JSONDecodeError:
                problems.append("console help --json did not return JSON")
            else:
                if help_payload.get("ok") is not True:
                    problems.append("console help --json did not report ok:true")
                if "  hyprial agent" not in help_payload.get("usage", "").splitlines():
                    problems.append("console help --json omits the agent group for agent link")

        version_run = run_cli(hyprial, ["version"], cwd=workdir, env=env)
        expected_version = re.compile(
            r"^packageVersion:\s*" + re.escape(str(wheel_summary.get("version"))) + r"\s*$",
            re.MULTILINE,
        )
        if version_run["exitCode"] != 0:
            problems.append(f"console version failed with exit {version_run['exitCode']}")
        elif not expected_version.search(version_run["stdout"]):
            problems.append(
                "console version does not report the installed version: "
                f"{version_run['stdout'][:500]!r}"
            )

        root = workdir / "daemon-root"
        root.mkdir()
        env = _environment(root)
        daemon_process, daemon_log, ready_problems = start_daemon(
            hyprial, args.python, root, env
        )
        problems.extend(ready_problems)
        summary["daemon"] = {
            "socket": str(root / "state" / "daemon.sock"),
            "logPath": str(root / "daemon.log"),
        }
        if not ready_problems:
            cases, case_problems = daemon_cases(hyprial, root, env, args.golden)
            summary["cases"] = cases
            problems.extend(case_problems)
        summary["daemon"]["returnCode"] = stop_daemon(daemon_process)
        daemon_process = None
        if daemon_log is not None:
            daemon_log.close()
            daemon_log = None
        if summary["daemon"].get("returnCode") not in (0, -15):
            problems.append(
                f"daemon exited with {summary['daemon'].get('returnCode')}"
            )

        if args.artifacts_dir is not None:
            args.artifacts_dir.mkdir(parents=True, exist_ok=True)
            if (root / "daemon.log").is_file():
                shutil.copyfile(root / "daemon.log", args.artifacts_dir / "daemon.log")
            if summary["cases"]:
                (args.artifacts_dir / "daemon-cases.json").write_text(
                    json.dumps(summary["cases"], indent=2, sort_keys=True),
                    encoding="utf-8",
                )
    finally:
        if daemon_process is not None:
            stop_daemon(daemon_process)
        if daemon_log is not None:
            daemon_log.close()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    summary["ok"] = not problems
    summary["workDir"] = str(workdir) if args.keep else None
    text = json.dumps(summary, indent=2, sort_keys=True)
    print(text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
