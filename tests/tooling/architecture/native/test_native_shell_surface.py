"""Keep the native shell as a capability-free webview wrapper."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
NATIVE = ROOT / "clients/native"


def test_tauri_surface_has_no_page_capability_or_plugin() -> None:
    config = json.loads((NATIVE / "src-tauri/tauri.conf.json").read_text())
    assert config["identifier"] == "com.hyprial.hyprialos"
    assert config["productName"] == "HyprialOS"
    assert config["app"]["withGlobalTauri"] is False
    assert config["app"]["windows"] == []
    assert "plugins" not in config
    assert "ipc:" not in config["app"]["security"]["csp"]
    assert not (NATIVE / "src-tauri/capabilities").exists()


def test_tauri_wiring_has_no_ipc_command_or_network_fetch() -> None:
    main = (NATIVE / "src-tauri/src/main.rs").read_text()
    page = (NATIVE / "src-tauri/ui/index.html").read_text()
    assert "invoke_handler" not in main
    assert "tauri::command" not in main
    assert "remote" not in main
    assert "fetch(" not in page
    assert "<script src=" not in page


def test_src_tauri_dependencies_are_minimal() -> None:
    manifest = (NATIVE / "src-tauri/Cargo.toml").read_text()
    allowed = {"hyprial-shell-launcher", "open", "tauri", "tauri-build", "tauri-plugin-single-instance"}
    dependencies = set()
    in_dependencies = False
    for line in manifest.splitlines():
        if line.startswith("["):
            in_dependencies = line == "[dependencies]" or line == "[build-dependencies]"
        elif in_dependencies and "=" in line and not line.startswith("#"):
            dependencies.add(line.split("=", 1)[0].strip())
    assert dependencies <= allowed


def test_single_instance_plugin_does_not_pull_in_deep_link() -> None:
    import tomllib

    manifest = tomllib.loads((NATIVE / "src-tauri/Cargo.toml").read_text())
    plugin = manifest["dependencies"]["tauri-plugin-single-instance"]
    features = plugin.get("features", []) if isinstance(plugin, dict) else []
    assert "deep-link" not in features
    lock = tomllib.loads((NATIVE / "src-tauri/Cargo.lock").read_text())
    names = {package["name"] for package in lock["package"]}
    assert "tauri-plugin-single-instance" in names
    assert not {name for name in names if "deep-link" in name or "deep_link" in name}


TAURI_FLOOR = (2, 11, 1)
TAURI_PINNED_AT_LEAST = (2, 12, 3)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def tauri_requirement_problems(manifest: dict, lock: dict) -> list[str]:
    problems = []
    tauri = manifest["dependencies"]["tauri"]
    requirement = tauri["version"] if isinstance(tauri, dict) else tauri
    features = tauri.get("features", []) if isinstance(tauri, dict) else []
    if not requirement.startswith("=") or _version(requirement[1:]) < TAURI_PINNED_AT_LEAST:
        problems.append(f"tauri requirement {requirement} is not an exact pin at or above 2.12.3")
    if "devtools" in features:
        problems.append("tauri devtools feature enables devtools in release builds")
    locked = [package["version"] for package in lock["package"] if package["name"] == "tauri"]
    if len(locked) != 1 or _version(locked[0]) < TAURI_FLOOR:
        problems.append(f"locked tauri {locked} is missing or below 2.11.1")
    elif requirement.startswith("=") and locked[0] != requirement[1:]:
        problems.append(f"locked tauri {locked[0]} differs from the pin {requirement}")
    return problems


def devtools_problems(main: str, config: dict) -> list[str]:
    problems = []
    if "devtools" in main:
        problems.append("main.rs opens or enables devtools")
    if "devtools" in json.dumps(config):
        problems.append("tauri.conf.json mentions devtools")
    return problems


def test_tauri_version_floor_and_release_devtools_are_pinned() -> None:
    import tomllib

    manifest = tomllib.loads((NATIVE / "src-tauri/Cargo.toml").read_text())
    lock = tomllib.loads((NATIVE / "src-tauri/Cargo.lock").read_text())
    config = json.loads((NATIVE / "src-tauri/tauri.conf.json").read_text())
    main = (NATIVE / "src-tauri/src/main.rs").read_text()
    assert tauri_requirement_problems(manifest, lock) == []
    assert devtools_problems(main, config) == []


def test_version_and_devtools_checks_reject_bad_inputs() -> None:
    lock = {"package": [{"name": "tauri", "version": "2.12.3"}]}
    good = {"dependencies": {"tauri": {"version": "=2.12.3", "features": []}}}
    assert tauri_requirement_problems(good, lock) == []
    for requirement in ("^2.12.3", "2.12.3", "=2.12.2", "=2.11.0"):
        manifest = {"dependencies": {"tauri": {"version": requirement, "features": []}}}
        assert tauri_requirement_problems(manifest, lock), requirement
    devtools = {"dependencies": {"tauri": {"version": "=2.12.3", "features": ["devtools"]}}}
    assert tauri_requirement_problems(devtools, lock)
    assert tauri_requirement_problems(good, {"package": [{"name": "tauri", "version": "2.11.0"}]})
    assert tauri_requirement_problems(good, {"package": [{"name": "tauri", "version": "2.12.4"}]})
    assert devtools_problems("window.open_devtools();", {})
    assert devtools_problems("", {"app": {"windows": [{"devtools": True}]}})
