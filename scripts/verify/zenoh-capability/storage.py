#!/usr/bin/env python3
"""Assess direct Python storage support and validate the zenohd fallback config."""

from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import zenoh

from _common import emit
from _common import isolated_config, reserve_loopback_port


ROOT = Path(__file__).resolve().parent


def render_config(
    listen_endpoint: str,
    storage_manager_plugin: Path,
    backend_search_dir: Path,
) -> str:
    template = (ROOT / "zenohd-filesystem.json5").read_text(encoding="utf-8")
    return (
        template
        .replace("__HYPRIAL_LISTEN_ENDPOINT__", json.dumps(listen_endpoint))
        .replace("__HYPRIAL_STORAGE_MANAGER_PLUGIN__", json.dumps(str(storage_manager_plugin)))
        .replace("__HYPRIAL_BACKEND_SEARCH_DIR__", json.dumps(str(backend_search_dir)))
    )


def wait_for_router(process: subprocess.Popen[str], port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise AssertionError(f"zenohd exited {process.returncode}: {output[-2000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("zenohd did not open its loopback listener")


def start_router(
    zenohd: Path,
    config_path: Path,
    storage_root: Path,
    port: int,
) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["ZENOH_BACKEND_FS_ROOT"] = str(storage_root)
    env["RUST_LOG"] = "warn"
    process = subprocess.Popen(
        [str(zenohd), "--config", str(config_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait_for_router(process, port)
    time.sleep(0.5)  # storage-manager and filesystem backend finish declaration
    return process


def stop_router(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def query_value(endpoint: str, key: str) -> bytes:
    session = zenoh.open(isolated_config(listen=[], connect=[endpoint]))
    try:
        replies = session.get(key, target=zenoh.QueryTarget.ALL, timeout=3.0)
        reply = replies.recv()
        sample = reply.ok
        if sample is None:
            raise AssertionError(f"storage query returned error: {reply.err}")
        return sample.payload.to_bytes()
    finally:
        session.close()


def verify_persistence(
    zenohd: Path,
    storage_manager_plugin: Path,
    backend: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-zenoh-storage-runtime-") as temporary:
        root = Path(temporary)
        storage_root = root / "data"
        port = reserve_loopback_port()
        endpoint = f"tcp/127.0.0.1:{port}"
        config_path = root / "zenohd.json5"
        config_path.write_text(
            render_config(endpoint, storage_manager_plugin, backend.parent),
            encoding="utf-8",
        )
        key = "hyprial/v1/inbox/agent-spike/message-1"
        expected = b"filesystem-persistence-spike"

        first = start_router(zenohd, config_path, storage_root, port)
        try:
            session = zenoh.open(isolated_config(listen=[], connect=[endpoint]))
            try:
                session.put(key, expected, encoding="application/octet-stream")
                time.sleep(0.5)
            finally:
                session.close()
            if query_value(endpoint, key) != expected:
                raise AssertionError("filesystem storage did not return the published value")
        finally:
            stop_router(first)

        second = start_router(zenohd, config_path, storage_root, port)
        try:
            if query_value(endpoint, key) != expected:
                raise AssertionError("filesystem value did not survive zenohd restart")
        finally:
            stop_router(second)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zenohd", help="optional zenohd path for later runtime integration")
    parser.add_argument("--filesystem-backend", help="optional filesystem backend library path")
    args = parser.parse_args()

    direct_api = any(
        hasattr(zenoh.Session, name)
        for name in ("declare_storage", "storage", "declare_backend")
    )
    if direct_api:
        raise AssertionError("unexpected direct storage API appeared; update the spike")

    zenohd = Path(args.zenohd) if args.zenohd else None
    if zenohd is None:
        discovered = shutil.which("zenohd")
        zenohd = Path(discovered) if discovered else None
    backend = Path(args.filesystem_backend) if args.filesystem_backend else None
    storage_manager_plugin = (
        zenohd.parent / "libzenoh_plugin_storage_manager.dylib"
        if zenohd is not None else None
    )
    with tempfile.TemporaryDirectory(prefix="hyprial-zenoh-storage-spike-") as temporary:
        temporary_root = Path(temporary)
        rendered = render_config(
            "tcp/127.0.0.1:0",
            storage_manager_plugin or temporary_root / "libzenoh_plugin_storage_manager.dylib",
            backend.parent if backend else temporary_root,
        )
        parsed = zenoh.Config.from_json5(rendered)
        plugin_config_roundtrip = "storage_manager" in parsed.get_json("plugins")

    runtime_ready = (
        zenohd is not None and zenohd.is_file()
        and backend is not None and backend.is_file()
        and storage_manager_plugin is not None and storage_manager_plugin.is_file()
    )
    if runtime_ready:
        verify_persistence(zenohd, storage_manager_plugin, backend)
    emit({
        "capability": "filesystem-storage",
        "direct_python_api": direct_api,
        "eclipse_zenoh": version("eclipse-zenoh"),
        "filesystem_backend": str(backend) if backend else None,
        "plugin_config_roundtrip": plugin_config_roundtrip,
        "persistence_across_restart": "PASS" if runtime_ready else "NOT_RUN",
        "status": "FALLBACK_VERIFIED" if runtime_ready else "FALLBACK_REQUIRED",
        "storage_manager_plugin": str(storage_manager_plugin) if storage_manager_plugin else None,
        "zenohd": str(zenohd) if zenohd else None,
        "zenohd_runtime_ready": runtime_ready,
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit({"capability": "filesystem-storage", "status": "FAIL", "error": str(error)})
        sys.exit(1)
