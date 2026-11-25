#!/usr/bin/env python3
"""Build and exercise the installed wheel GUI in one isolated acceptance run.

The run builds the wheel with ``scripts/build/build-wheel.sh`` from a clean
checkout, installs that exact file into a fresh GIL CPython 3.12 virtualenv,
starts a disposable logged-in daemon, launches ``hyprial gui``, and drives its
private launch URL with Python Playwright and Chromium. The browser must see
the Start page, a successful ``/daemon/request/*`` response, and no
``not implemented: hyprial backend not wired`` response. Closing the GUI must
leave the daemon alive until this script explicitly stops it.

With ``--wheel <file>`` the build is skipped and that exact file is installed
instead; this is the path for a wheel built by CI, because the native engine
must be built in a manylinux2014 image and most developer machines cannot do
that (decision 2026-11-15: acceptance uses a CI-built wheel).

This file installs only the wheel under test. It does not call install.sh,
the wheel validator, a publication script, or PR #2730. After installation,
the product process runs outside the checkout with no source import path and
no Node, npm, or Git dependency. Source mode, ``gui dev``, external accounts,
real Lark/provider traffic, native shell packaging, and publication remain
outside this acceptance. Missing build, GIL CPython 3.12, Playwright, or
Chromium prerequisites are BLOCKED and never reported as PASS.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

PASS = 0
FAIL = 1
BLOCKED = 2
NOT_WIRED = "not implemented: hyprial backend not wired"


@dataclass(frozen=True)
class Step:
    name: str
    status: str
    detail: str


def verdict(steps: list[Step]) -> tuple[str, int]:
    failures = [step for step in steps if step.status == "FAIL"]
    if failures:
        step = failures[0]
        return f"FAIL {step.name} {step.detail}", FAIL
    blocked = [step for step in steps if step.status == "BLOCKED"]
    if blocked:
        step = blocked[0]
        return f"BLOCKED {step.name} {step.detail}", BLOCKED
    return "PASS", PASS


def command(argv: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=None if cwd is None else str(cwd), env=None if env is None else dict(env), capture_output=True, text=True, timeout=timeout, check=False)


def probe_interpreter(path: str) -> tuple[bool, str]:
    found = shutil.which(path) if Path(path).parent == Path(".") else path
    if found is None or not Path(found).is_file():
        return False, f"interpreter not found: {path}"
    result = command([found, "-I", "-c", "import json,sys,sysconfig; print(json.dumps({'v': list(sys.version_info[:3]), 'i': sys.implementation.name, 'g': sysconfig.get_config_var('Py_GIL_DISABLED')}))"])
    if result.returncode != 0:
        return False, result.stderr.strip() or "interpreter probe failed"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, f"interpreter probe was not JSON: {result.stdout!r}"
    version = payload.get("v")
    if not isinstance(version, list) or len(version) < 3:
        return False, f"interpreter probe returned invalid version: {payload}"
    if version[:2] != [3, 12] or payload.get("i") != "cpython":
        return False, f"requires GIL CPython 3.12, got {payload}"
    if payload.get("g") in {1, "1", True}:
        return False, f"requires GIL CPython 3.12, got free-threaded build: {payload}"
    return True, f"CPython {version[0]}.{version[1]}.{version[2]} (GIL)"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def isolated_environment(root: Path, venv_bin: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("HYPRIAL_") and not key.startswith("HARNESS_")}
    environment.update({
        "HOME": str(root / "user"), "HYPRIAL_HOME": str(root / "home"), "HARNESS_STATE_DIR": str(root / "state"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"), "XDG_STATE_HOME": str(root / "xdg-state"), "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_DATA_HOME": str(root / "xdg-data"), "XDG_RUNTIME_DIR": str(root / "runtime"), "TMPDIR": str(root / "tmp"),
        "TMP": str(root / "tmp"), "TEMP": str(root / "tmp"), "PATH": str(venv_bin), "PYTHONNOUSERSITE": "1",
        "HYPRIAL_NETWORK_ISOLATED": "1", "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "", "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_NODE_ID": "wheel-acceptance-node", "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
        "UV_PYTHON_DOWNLOADS": "never",
    })
    return environment


def prepare_home(root: Path) -> None:
    for name in ("home", "state", "user", "runtime", "tmp", "xdg-config", "xdg-state", "xdg-cache", "xdg-data"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "home" / "settings.json").write_text(json.dumps({"version": 1, "owner": "wheel-acceptance-owner"}), encoding="utf-8")


def wait_for_path(path: Path, process: subprocess.Popen[str], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def wait_for_json(process: subprocess.Popen[str], timeout: float) -> tuple[dict[str, Any] | None, str]:
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    output: queue.Queue[str] = queue.Queue()

    def read() -> None:
        if process.stdout is not None:
            output.put(process.stdout.readline())

    threading.Thread(target=read, daemon=True).start()
    while time.monotonic() < deadline:
        try:
            line = output.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if not line:
            break
        lines.append(line.strip())
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "ok" in payload:
            return payload, ""
    return None, "GUI did not emit one startup JSON object: " + " | ".join(lines[-3:])


def stop_process(process: subprocess.Popen[str] | None, timeout: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def browser_check(launch_url: str, timeout: float) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        return False, f"Playwright is unavailable: {error}"
    executable = os.environ.get("CHROMIUM_EXECUTABLE")
    try:
        with sync_playwright() as playwright:
            browser_args: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
            if executable:
                browser_args["executable_path"] = executable
            browser = playwright.chromium.launch(**browser_args)
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            responses: list[tuple[str, int, str]] = []
            def observe(response: Any) -> None:
                try:
                    body = response.text()
                except Exception:
                    body = ""
                responses.append((response.url, response.status, body))
            page.on("response", observe)
            page.goto(launch_url, wait_until="networkidle", timeout=int(timeout * 1000))
            page.locator("main#main-content").wait_for(timeout=int(timeout * 1000))
            page.locator('[data-page="start"]').wait_for(timeout=int(timeout * 1000))
            page.wait_for_timeout(1000)
            browser.close()
    except Exception as error:
        return False, f"Chromium/Playwright failed: {error}"
    daemon_responses = [item for item in responses if "/daemon/request/" in item[0]]
    bad = [item for item in responses if NOT_WIRED in item[2]]
    if bad:
        return False, f"not-wired response observed at {bad[0][0]} status={bad[0][1]}"
    if not any(status == 200 for _, status, _ in daemon_responses):
        return False, f"no successful /daemon/request/* response; observed={daemon_responses}"
    return True, f"Start page and {len(daemon_responses)} daemon requests observed"


def obtain_wheel(args: argparse.Namespace, root: Path, steps: list[Step], evidence: dict[str, Any]) -> list[Path]:
    """Return the one wheel under test: the given file, or a fresh local build."""
    if args.wheel is not None:
        given = Path(args.wheel)
        usable = given.is_file() and given.suffix == ".whl"
        steps.append(Step("given-wheel", "PASS" if usable else "BLOCKED", str(given) if usable else f"not a wheel file: {given}"))
        evidence["wheel"] = {"source": "given", "name": given.name}
        return [given.resolve()] if usable else []
    checkout = Path(__file__).resolve().parents[2]
    status = command(["git", "-C", str(checkout), "status", "--porcelain"])
    clean = status.returncode == 0 and not status.stdout.strip()
    steps.append(Step("clean-checkout", "PASS" if clean else "BLOCKED", "clean" if clean else status.stdout.strip() or status.stderr.strip()))
    if not clean:
        return []
    outdir = root / "dist"
    build = command(["bash", str(checkout / "scripts/build/build-wheel.sh"), str(outdir)], cwd=checkout, timeout=args.build_timeout)
    wheels = sorted(outdir.glob("*.whl")) if outdir.exists() else []
    built = build.returncode == 0 and len(wheels) == 1
    steps.append(Step("build-wheel", "PASS" if built else "BLOCKED", build.stdout.strip()[-1000:] or build.stderr.strip()[-1000:] or "no wheel"))
    evidence["build"] = {"returncode": build.returncode, "stdout": build.stdout[-4000:], "stderr": build.stderr[-4000:]}
    evidence["wheel"] = {"source": "built", "name": wheels[0].name if built else None}
    return wheels if built else []


def finish(steps: list[Step], evidence: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    line, code = verdict(steps)
    evidence["steps"] = [step.__dict__ for step in steps]
    evidence["verdict"] = line
    return code, line, evidence


def run(args: argparse.Namespace) -> tuple[int, str, dict[str, Any]]:
    root = Path(tempfile.mkdtemp(prefix="hyprial-gui-wheel-"))
    steps: list[Step] = []
    evidence: dict[str, Any] = {"root": str(root)}
    daemon: subprocess.Popen[str] | None = None
    gui: subprocess.Popen[str] | None = None
    try:
        valid, detail = probe_interpreter(args.python)
        steps.append(Step("python", "PASS" if valid else "BLOCKED", detail))
        uv = shutil.which("uv")
        steps.append(Step("uv", "PASS" if uv else "BLOCKED", "uv available" if uv else "uv is missing"))
        if not valid or uv is None:
            return finish(steps, evidence)
        wheels = obtain_wheel(args, root, steps, evidence)
        if not wheels:
            return finish(steps, evidence)
        venv = root / "venv"
        create = command([args.python, "-m", "venv", str(venv)])
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = command([uv, "pip", "install", "--python", str(venv_python), str(wheels[0])], timeout=args.install_timeout) if create.returncode == 0 else create
        installed = create.returncode == 0 and install.returncode == 0
        steps.append(Step("install-wheel", "PASS" if installed else "BLOCKED", install.stdout.strip()[-1000:] or install.stderr.strip()[-1000:]))
        if not installed:
            return finish(steps, evidence)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        console = bin_dir / ("hyprial.exe" if os.name == "nt" else "hyprial")
        environment = isolated_environment(root, bin_dir)
        prepare_home(root)
        (root / "outside").mkdir()
        daemon = subprocess.Popen([str(console), "daemon", "run"], cwd=str(root / "outside"), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = wait_for_path(root / "state" / "daemon.sock", daemon, args.service_timeout)
        steps.append(Step("daemon", "PASS" if ready else "FAIL", "isolated daemon ready" if ready else "daemon did not create its socket"))
        if not ready:
            return finish(steps, evidence)
        port = args.port or free_port()
        gui = subprocess.Popen([str(console), "gui", "--no-open", "--json", "--port", str(port)], cwd=str(root / "outside"), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        startup, startup_error = wait_for_json(gui, args.service_timeout)
        launch_path = Path(str(startup.get("handoff_path"))) if startup and startup.get("handoff_path") else None
        started = startup is not None and startup.get("ok") is True and launch_path is not None and launch_path.is_file()
        steps.append(Step("gui-start", "PASS" if started else "FAIL", "startup JSON and launch.url ready" if started else startup_error or "startup payload missing launch.url"))
        if not started:
            return finish(steps, evidence)
        browser_ok, browser_detail = browser_check(launch_path.read_text(encoding="utf-8").strip(), args.browser_timeout)
        browser_status = "PASS" if browser_ok else "BLOCKED" if "unavailable" in browser_detail.lower() else "FAIL"
        steps.append(Step("browser", browser_status, browser_detail))
        stop_process(gui)
        steps.append(Step("lifecycle", "PASS" if gui.poll() is not None and daemon.poll() is None else "FAIL", f"gui_exit={gui.poll()} daemon_alive={daemon.poll() is None}"))
        return finish(steps, evidence)
    except (OSError, subprocess.SubprocessError) as error:
        steps.append(Step("runner", "BLOCKED", str(error)))
        return finish(steps, evidence)
    finally:
        stop_process(gui)
        stop_process(daemon)
        if args.keep:
            evidence["kept"] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--wheel", type=Path, help="install this wheel file instead of building one")
    parser.add_argument("--port", type=int)
    parser.add_argument("--build-timeout", type=float, default=900)
    parser.add_argument("--install-timeout", type=float, default=300)
    parser.add_argument("--service-timeout", type=float, default=60)
    parser.add_argument("--browser-timeout", type=float, default=60)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    code, line, evidence = run(args)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
