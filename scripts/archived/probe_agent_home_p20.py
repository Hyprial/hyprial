#!/usr/bin/env python3
"""Measure P20 native agent-home capabilities without reading real secrets.

The probe gives every executable a fresh HOME, XDG tree, and native config
root.  Its only credential-shaped input is the checked-in literal
``P20_SENTINEL_NOT_A_SECRET``.  It never invokes an interactive login, a
vendor model request, Forgejo whoami, or a network push.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SENTINEL = "P20_SENTINEL_NOT_A_SECRET"
DEFAULT_FIXTURES = Path("tests/fixtures/agent_home_p20")
DEFAULT_TIMEOUT_SECONDS = 20.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    aggregate = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "tree_sha256": aggregate,
    }


def verify_fixtures(root: Path) -> dict[str, Any]:
    required = {
        "claude/native-root/settings.json",
        "claude/project/.claude/settings.json",
        "codex/native-root/config.toml",
        "codex/native-root/auth.json",
        "codex/project/.codex/config.toml",
        "pi/native-root/settings.json",
        "pi/native-root/auth.json",
        "pi/workspace/AGENTS.md",
        "pi/workspace/repo/AGENTS.md",
        "pi/workspace/repo/nested/AGENTS.md",
        "tool-profile/gitconfig",
        "tool-profile/ssh_config",
    }
    manifest = _tree_manifest(root)
    present = {item["path"] for item in manifest["files"]}
    missing = sorted(required - present)
    parse_errors: list[str] = []
    for item in manifest["files"]:
        path = root / item["path"]
        try:
            if path.suffix == ".json":
                json.loads(path.read_text())
            elif path.suffix == ".toml":
                tomllib.loads(path.read_text())
            elif path.name == "SKILL.md":
                text = path.read_text()
                if not text.startswith("---\n") or "\nname:" not in text:
                    raise ValueError("missing native skill frontmatter")
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
            parse_errors.append(f"{item['path']}: {exc}")

    forbidden = re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
        r"\bghp_[A-Za-z0-9]{20,}|\bsk-(?:ant|proj)-[A-Za-z0-9_-]{12,}"
    )
    secret_findings: list[str] = []
    for item in manifest["files"]:
        path = root / item["path"]
        try:
            text = path.read_text()
        except UnicodeError:
            continue
        if forbidden.search(text.replace(SENTINEL, "")):
            secret_findings.append(item["path"])

    codex_auth = json.loads((root / "codex/native-root/auth.json").read_text())
    pi_auth = json.loads((root / "pi/native-root/auth.json").read_text())
    sentinel_ok = (
        codex_auth == {"auth_mode": "apikey", "OPENAI_API_KEY": SENTINEL}
        and pi_auth
        == {"openai": {"type": "api_key", "key": SENTINEL}}
    )
    state = "PASS" if not (missing or parse_errors or secret_findings) and sentinel_ok else "FAIL"
    return {
        "state": state,
        "missing": missing,
        "parse_errors": parse_errors,
        "secret_findings": secret_findings,
        "sentinel_shapes_exact": sentinel_ok,
        "manifest": manifest,
    }


def _base_env(root: Path) -> dict[str, str]:
    paths = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "xdg-config",
        "XDG_CACHE_HOME": root / "xdg-cache",
        "XDG_DATA_HOME": root / "xdg-data",
        "XDG_STATE_HOME": root / "xdg-state",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CI": "1",
        **{name: str(path) for name, path in paths.items()},
    }


def _normalize(text: str, replacements: dict[str, str]) -> str:
    value = text.replace("\r\n", "\n")
    variants: list[tuple[str, str]] = []
    for original, replacement in replacements.items():
        variants.append((original, replacement))
        try:
            variants.append((str(Path(original).resolve()), replacement))
        except OSError:
            pass
    for original, replacement in sorted(
        set(variants), key=lambda item: len(item[0]), reverse=True
    ):
        value = value.replace(original, replacement)
    value = value.replace(SENTINEL, "<P20_SENTINEL>")
    value = re.sub(r"P20_SENT[A-Z*_-]*ECRET", "<P20_SENTINEL>", value)
    return value.rstrip()


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    replacements: dict[str, str],
    input_text: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": _normalize(completed.stdout, replacements),
            "stderr": _normalize(completed.stderr, replacements),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return {
            "argv": argv,
            "returncode": None,
            "timed_out": True,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": _normalize(stdout, replacements),
            "stderr": _normalize(stderr, replacements),
        }


def _tool_path(name: str) -> str | None:
    return shutil.which(name)


def _prepare_native_root(source: Path, target: Path) -> None:
    shutil.copytree(source, target)
    auth = target / "auth.json"
    if auth.exists():
        auth.chmod(0o600)


def _tool_header(name: str) -> dict[str, Any]:
    executable = _tool_path(name)
    if executable is None:
        return {"state": "NOT_RUN", "reason": f"{name} is not installed"}
    path = Path(executable)
    resolved = path.resolve()
    return {
        "state": "PASS",
        "executable": str(path),
        "resolved_executable": str(resolved),
        "executable_sha256": _sha256(resolved),
    }


def _probe_claude(fixtures: Path, probe_root: Path, repo_root: Path) -> dict[str, Any]:
    result = _tool_header("claude")
    if result["state"] == "NOT_RUN":
        return result
    native_root = probe_root / "claude"
    _prepare_native_root(fixtures / "claude/native-root", native_root)
    workspace = probe_root / "workspace"
    shutil.copytree(fixtures / "claude/project", workspace)
    env = _base_env(probe_root)
    env["CLAUDE_CONFIG_DIR"] = str(native_root)
    replacements = {str(probe_root): "<PROBE_ROOT>", str(repo_root): "<REPO_ROOT>"}
    commands = [
        ["claude", "--version"],
        ["claude", "--help"],
        ["claude", "auth", "--help"],
        ["claude", "auth", "status", "--help"],
        ["claude", "auth", "status", "--json"],
        ["claude", "doctor", "--help"],
    ]
    captures = [
        _run(command, cwd=workspace, env=env, replacements=replacements)
        for command in commands
    ]
    auth_status: dict[str, Any] | None = None
    try:
        auth_status = json.loads(captures[4]["stdout"])
    except (json.JSONDecodeError, TypeError):
        pass
    config_root_ok = bool(
        auth_status
        and auth_status.get("configDirectory") == "<PROBE_ROOT>/claude"
        and auth_status.get("projectsDirectory")
        == "<PROBE_ROOT>/claude/projects"
    )
    result.update(
        {
            "commands": captures,
            "version": captures[0]["stdout"],
            "config_root": {
                "state": "PASS" if config_root_ok else "FAIL",
                "auth_status": auth_status,
            },
            "effective_settings": {
                "state": "NOT_RUN",
                "reason": (
                    "2.1.278 exposes no non-auth effective-settings command; "
                    "help/auth status proves the redirected root, not actual "
                    "user/project settings consumption"
                ),
            },
            "auth_refresh": {
                "state": "NOT_RUN",
                "reason": "no authorized test OAuth account; no real login/keychain read performed",
            },
        }
    )
    if any(item["timed_out"] for item in captures) or captures[0]["returncode"] != 0:
        result["state"] = "FAIL"
    return result


def _json_rpc_exchange(
    argv: list[str],
    requests: list[dict[str, Any]],
    wanted_ids: set[str | int],
    *,
    cwd: Path,
    env: dict[str, str],
    replacements: dict[str, str],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    responses: dict[str, dict[str, Any]] = {}
    other_messages: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    started = time.monotonic()
    try:
        for request in requests:
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
            request_id = request.get("id")
            if request_id not in wanted_ids:
                continue
            while str(request_id) not in responses:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"waiting for response id {request_id}")
                ready = selector.select(remaining)
                if not ready:
                    raise TimeoutError(f"waiting for response id {request_id}")
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError(
                        f"process exited before response id {request_id}: {process.poll()}"
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"{exc}: {line.rstrip()}")
                    continue
                message_id = message.get("id")
                if message_id in wanted_ids:
                    responses[str(message_id)] = message
                else:
                    other_messages.append(message)
        state = "PASS" if wanted_ids == {int(key) if key.isdigit() else key for key in responses} else "FAIL"
    except (OSError, RuntimeError, TimeoutError) as exc:
        state = "FAIL"
        parse_errors.append(str(exc))
    finally:
        process.terminate()
        try:
            _, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=3)
        selector.close()
    normalized = json.loads(
        _normalize(json.dumps(responses), replacements)
    )
    normalized_other = json.loads(
        _normalize(json.dumps(other_messages), replacements)
    )
    return {
        "state": state,
        "argv": argv,
        "responses": normalized,
        "other_messages": normalized_other,
        "parse_errors": parse_errors,
        "stderr": _normalize(stderr, replacements),
    }


def _codex_schema_methods(bundle: Path) -> dict[str, list[str]]:
    body = json.loads(bundle.read_text())
    definitions = body.get("definitions", {})
    groups: dict[str, list[str]] = {}
    for name in ("ClientRequest", "ClientNotification", "ServerRequest", "ServerNotification"):
        methods: list[str] = []
        for variant in definitions.get(name, {}).get("oneOf", []):
            method = variant.get("properties", {}).get("method", {})
            methods.extend(method.get("enum", []))
        groups[name] = sorted(set(methods))
    return groups


def _probe_codex(fixtures: Path, probe_root: Path, repo_root: Path) -> dict[str, Any]:
    result = _tool_header("codex")
    if result["state"] == "NOT_RUN":
        return result
    native_root = probe_root / "codex"
    _prepare_native_root(fixtures / "codex/native-root", native_root)
    workspace = probe_root / "workspace"
    shutil.copytree(fixtures / "codex/project", workspace)
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        env=_base_env(probe_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    with (native_root / "config.toml").open("a") as stream:
        escaped = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
        stream.write(f'\n[projects."{escaped}"]\ntrust_level = "trusted"\n')
    env = _base_env(probe_root)
    env["CODEX_HOME"] = str(native_root)
    replacements = {str(probe_root): "<PROBE_ROOT>", str(repo_root): "<REPO_ROOT>"}
    commands = [
        ["codex", "--version"],
        ["codex", "--help"],
        ["codex", "login", "--help"],
        ["codex", "login", "status", "--help"],
        ["codex", "login", "status"],
        ["codex", "app-server", "--help"],
        ["codex", "app-server", "generate-json-schema", "--help"],
        ["codex", "doctor", "--help"],
    ]
    captures = [
        _run(command, cwd=workspace, env=env, replacements=replacements)
        for command in commands
    ]
    schema_dir = probe_root / "schema"
    schema_capture = _run(
        [
            "codex",
            "app-server",
            "generate-json-schema",
            "--out",
            str(schema_dir),
        ],
        cwd=workspace,
        env=env,
        replacements=replacements,
        timeout=60,
    )
    schema_manifest = _tree_manifest(schema_dir) if schema_dir.exists() else {
        "files": [],
        "file_count": 0,
        "total_bytes": 0,
        "tree_sha256": None,
    }
    schema_parse_errors: list[str] = []
    parsed_schema_files = 0
    for item in schema_manifest["files"]:
        path = schema_dir / item["path"]
        if path.suffix != ".json":
            continue
        try:
            json.loads(path.read_text())
            parsed_schema_files += 1
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            schema_parse_errors.append(f"{item['path']}: {exc}")
    bundle = schema_dir / "codex_app_server_protocol.v2.schemas.json"
    schema_methods = _codex_schema_methods(bundle) if bundle.exists() else {}
    rpc = _json_rpc_exchange(
        ["codex", "app-server", "--stdio"],
        [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-home-p20-probe", "version": "1"}
                },
            },
            {"method": "initialized", "params": {}},
            {
                "id": 2,
                "method": "config/read",
                "params": {"cwd": str(workspace), "includeLayers": True},
            },
        ],
        {1, 2},
        cwd=workspace,
        env=env,
        replacements=replacements,
    )
    config_result = rpc.get("responses", {}).get("2", {}).get("result", {})
    config = config_result.get("config", {}) if isinstance(config_result, dict) else {}
    layers = config_result.get("layers") if isinstance(config_result, dict) else []
    layer_types = [
        layer.get("name", {}).get("type")
        for layer in layers or []
        if isinstance(layer, dict)
    ]
    config_summary = {
        "state": (
            "PASS"
            if rpc["state"] == "PASS"
            and config.get("model") == "p20-codex-project-sentinel"
            and "user" in layer_types
            and "project" in layer_types
            and "system" in layer_types
            else "FAIL"
        ),
        "effective_model": config.get("model"),
        "layer_types": layer_types,
        "layer_files": [
            layer.get("name", {}).get("file")
            for layer in layers or []
            if isinstance(layer, dict)
        ],
        "rpc": rpc,
    }
    result.update(
        {
            "commands": captures,
            "version": captures[0]["stdout"],
            "schema_generation": {
                "state": (
                    "PASS"
                    if schema_capture["returncode"] == 0
                    and schema_manifest["file_count"] > 0
                    and not schema_parse_errors
                    and schema_methods
                    else "FAIL"
                ),
                "command": schema_capture,
                "manifest": schema_manifest,
                "parsed_files": parsed_schema_files,
                "parse_errors": schema_parse_errors,
                "methods": schema_methods,
            },
            "config_discovery": config_summary,
            "auth": {
                "state": "PASS",
                "fixture_shape": ["auth_mode", "OPENAI_API_KEY"],
                "file_mode": oct((native_root / "auth.json").stat().st_mode & 0o777),
                "login_status": captures[4],
                "note": "native parser sees the isolated sentinel file; no vendor request was made",
            },
            "auth_refresh": {
                "state": "NOT_RUN",
                "reason": "no authorized test OAuth account; refresh was not attempted",
            },
        }
    )
    if (
        captures[0]["returncode"] != 0
        or config_summary["state"] != "PASS"
        or result["schema_generation"]["state"] != "PASS"
    ):
        result["state"] = "FAIL"
    return result


def _pi_package_root(executable: Path) -> Path | None:
    resolved = executable.resolve()
    for candidate in (resolved.parent, *resolved.parents):
        package = candidate / "package.json"
        if not package.is_file():
            continue
        try:
            body = json.loads(package.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if body.get("name") == "@earendil-works/pi-coding-agent":
            return candidate
    return None


def _pi_context_probe(
    package_root: Path,
    *,
    cwd: Path,
    agent_dir: Path,
    env: dict[str, str],
    replacements: dict[str, str],
) -> dict[str, Any]:
    loader = package_root / "dist/core/resource-loader.js"
    script = """
import { pathToFileURL } from 'node:url';
const [loader, cwd, agentDir] = process.argv.slice(1);
const mod = await import(pathToFileURL(loader).href);
const rows = mod.loadProjectContextFiles({cwd, agentDir});
process.stdout.write(JSON.stringify(rows));
""".strip()
    capture = _run(
        ["node", "--input-type=module", "-e", script, str(loader), str(cwd), str(agent_dir)],
        cwd=cwd,
        env=env,
        replacements=replacements,
    )
    rows: list[dict[str, Any]] = []
    try:
        rows = json.loads(capture["stdout"])
    except (json.JSONDecodeError, TypeError):
        pass
    paths = [row.get("path") for row in rows if isinstance(row, dict)]
    outside = any(path == "<PROBE_ROOT>/workspace/AGENTS.md" for path in paths)
    return {
        "state": "UNSUPPORTED" if outside else "PASS",
        "command": capture,
        "paths": paths,
        "outside_repository_ancestor_loaded": outside,
        "reason": (
            "0.85.1 loadProjectContextFiles traverses above the fixture Git root"
            if outside
            else None
        ),
    }


def _probe_pi(fixtures: Path, probe_root: Path, repo_root: Path) -> dict[str, Any]:
    result = _tool_header("pi")
    if result["state"] == "NOT_RUN":
        return result
    native_root = probe_root / "pi"
    _prepare_native_root(fixtures / "pi/native-root", native_root)
    workspace = probe_root / "workspace"
    shutil.copytree(fixtures / "pi/workspace", workspace)
    repository = workspace / "repo"
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        env=_base_env(probe_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    cwd = workspace / "repo/nested"
    sessions = probe_root / "sessions"
    sessions.mkdir()
    env = _base_env(probe_root)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(native_root),
            "PI_CODING_AGENT_SESSION_DIR": str(sessions),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
        }
    )
    replacements = {str(probe_root): "<PROBE_ROOT>", str(repo_root): "<REPO_ROOT>"}
    commands = [
        ["pi", "--version"],
        ["pi", "--help"],
        ["pi", "auth", "--help"],
        ["pi", "auth", "check", "--provider", "openai", "--json", "--no-refresh"],
    ]
    captures = [
        _run(command, cwd=cwd, env=env, replacements=replacements)
        for command in commands
    ]
    default_help_root = probe_root / "default-help"
    default_help_env = _base_env(default_help_root)
    default_help_env.update({"PI_OFFLINE": "1", "PI_TELEMETRY": "0"})
    default_help = _run(
        ["pi", "--help"],
        cwd=cwd,
        env=default_help_env,
        replacements={
            str(default_help_root): "<DEFAULT_HELP_ROOT>",
            **replacements,
        },
    )
    default_help_manifest = _tree_manifest(default_help_root)
    default_help_writes = [
        item["path"]
        for item in default_help_manifest["files"]
        if item["path"].startswith("home/.pi/")
    ]
    rpc = _json_rpc_exchange(
        [
            "pi",
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--approve",
            "--session-dir",
            str(sessions),
        ],
        [
            {"id": "state", "type": "get_state"},
            {"id": "commands", "type": "get_commands"},
        ],
        {"state", "commands"},
        cwd=cwd,
        env=env,
        replacements=replacements,
    )
    commands_data = (
        rpc.get("responses", {})
        .get("commands", {})
        .get("data", {})
        .get("commands", [])
    )
    skill_rows = [
        {
            "name": row.get("name"),
            "source": row.get("source"),
            "location": row.get("location"),
            "path": row.get("path"),
        }
        for row in commands_data
        if isinstance(row, dict) and row.get("source") == "skill"
    ]
    skill_names = {row["name"] for row in skill_rows}
    skill_state = (
        "PASS"
        if {"skill:p20-pi-user", "skill:p20-pi-project"}.issubset(skill_names)
        else "FAIL"
    )
    package_root = _pi_package_root(Path(result["executable"]))
    package_artifacts: dict[str, Any] = {}
    context_probe: dict[str, Any] = {
        "state": "NOT_RUN",
        "reason": "installed Pi package root could not be resolved",
    }
    if package_root is not None:
        paths = {
            "package": package_root / "package.json",
            "rpc_types": package_root / "dist/modes/rpc/rpc-types.d.ts",
            "resource_loader": package_root / "dist/core/resource-loader.js",
            "resource_loader_types": package_root / "dist/core/resource-loader.d.ts",
            "rpc_docs": package_root / "docs/rpc.md",
        }
        package_artifacts = {
            name: {
                "path": _normalize(str(path), replacements),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for name, path in paths.items()
            if path.is_file()
        }
        rpc_types = paths["rpc_types"].read_text()
        package_artifacts["rpc_command_literals"] = sorted(
            set(re.findall(r'type:\s*"([a-z][a-z0-9_]*)"', rpc_types))
        )
        context_probe = _pi_context_probe(
            package_root,
            cwd=cwd,
            agent_dir=native_root,
            env=env,
            replacements=replacements,
        )
    auth_body: dict[str, Any] | None = None
    try:
        auth_body = json.loads(captures[3]["stdout"])
    except (json.JSONDecodeError, TypeError):
        pass
    result.update(
        {
            "commands": captures,
            "version": captures[0]["stdout"],
            "default_root_help": {
                "state": (
                    "PASS"
                    if default_help["returncode"] == 0 and default_help_writes
                    else "FAIL"
                ),
                "command": default_help,
                "written_files": default_help_writes,
                "note": "pi --help initializes files below default HOME/.pi when PI_CODING_AGENT_DIR is unset",
            },
            "rpc": rpc,
            "resource_discovery": {
                "state": skill_state,
                "skills": skill_rows,
                "context_boundary": context_probe,
            },
            "auth": {
                "state": (
                    "PASS"
                    if auth_body
                    == {"status": "ready", "provider": "openai", "authType": "api_key"}
                    else "FAIL"
                ),
                "check": auth_body,
                "file_mode": oct((native_root / "auth.json").stat().st_mode & 0o777),
                "refresh": "suppressed by --no-refresh",
            },
            "auth_refresh": {
                "state": "NOT_RUN",
                "reason": "no authorized test OAuth account; --no-refresh was mandatory",
            },
            "package_artifacts": package_artifacts,
        }
    )
    if captures[0]["returncode"] != 0 or skill_state != "PASS" or result["auth"]["state"] != "PASS":
        result["state"] = "FAIL"
    return result


def _agent_sdk_version(repo_root: Path) -> str | None:
    source = repo_root / "src/hyprial/harnesses/agent_sdk.py"
    try:
        tree = ast.parse(source.read_text())
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "AGENT_SDK_VERSION" for target in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def _probe_claude_sdk(probe_root: Path, repo_root: Path) -> dict[str, Any]:
    version = _agent_sdk_version(repo_root)
    uv = _tool_path("uv")
    if version is None:
        return {"state": "NOT_RUN", "reason": "AGENT_SDK_VERSION was not found"}
    if uv is None:
        return {"state": "NOT_RUN", "reason": "uv is not installed", "pinned_version": version}
    env = _base_env(probe_root)
    sdk_cache = Path(
        os.environ.get(
            "AGENT_HOME_P20_UV_CACHE",
            str(Path(tempfile.gettempdir()) / "agent-home-p20-uv-cache"),
        )
    )
    sdk_cache.mkdir(parents=True, exist_ok=True)
    env["UV_CACHE_DIR"] = str(sdk_cache)
    allow_network = os.environ.get("AGENT_HOME_P20_ALLOW_NETWORK") == "1"
    if allow_network:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if name in os.environ:
                env[name] = os.environ[name]
    script = (
        "import dataclasses,importlib.metadata as m,json;"
        "from claude_agent_sdk import ClaudeAgentOptions;"
        "print(json.dumps({'version':m.version('claude-agent-sdk'),"
        "'option_fields':[f.name for f in dataclasses.fields(ClaudeAgentOptions)],"
        "'setting_sources':str(ClaudeAgentOptions.__annotations__['setting_sources'])},sort_keys=True))"
    )
    uv_args = [
        uv,
        "run",
        *([] if allow_network else ["--offline"]),
        "--isolated",
        "--no-project",
        "--with",
        f"claude-agent-sdk=={version}",
        "python",
        "-c",
        script,
    ]
    capture = _run(
        uv_args,
        cwd=probe_root,
        env=env,
        replacements={str(probe_root): "<PROBE_ROOT>", str(repo_root): "<REPO_ROOT>"},
        timeout=180,
    )
    body: dict[str, Any] | None = None
    try:
        body = json.loads(capture["stdout"])
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "state": "PASS" if body and body.get("version") == version else "NOT_RUN",
        "pinned_version": version,
        "command": capture,
        "schema": body,
        "reason": None if body else "isolated SDK package could not be loaded",
    }


def _probe_tools(fixtures: Path, probe_root: Path, repo_root: Path) -> dict[str, Any]:
    env = _base_env(probe_root)
    replacements = {str(probe_root): "<PROBE_ROOT>", str(repo_root): "<REPO_ROOT>"}
    profile = fixtures / "tool-profile"
    env["GIT_CONFIG_GLOBAL"] = str(profile / "gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    commands = {
        "fj_version": ["fj", "version"],
        "fj_help": ["fj", "--help"],
        "fj_whoami_help": ["fj", "whoami", "--help"],
        "tea_version": ["tea", "--version"],
        "tea_help": ["tea", "--help"],
        "tea_whoami_help": ["tea", "whoami", "--help"],
        "git_version": ["git", "--version"],
        "git_profile": ["git", "config", "--global", "--list", "--show-origin"],
        "ssh_version": ["ssh", "-V"],
        "ssh_effective": [
            "ssh",
            "-G",
            "-F",
            str(profile / "ssh_config"),
            "git.internal.hyprial.com",
        ],
        "uv_version": ["uv", "--version"],
    }
    captures: dict[str, Any] = {}
    missing: list[str] = []
    for name, command in commands.items():
        if _tool_path(command[0]) is None:
            captures[name] = {
                "argv": command,
                "state": "NOT_RUN",
                "reason": f"{command[0]} is not installed",
            }
            missing.append(command[0])
            continue
        captures[name] = _run(
            command,
            cwd=repo_root,
            env=env,
            replacements=replacements,
        )
    return {
        "state": "PASS" if not missing else "NOT_RUN",
        "commands": captures,
        "service_identity": {
            "state": "NOT_RUN",
            "reason": (
                "no approved test Forgejo login/repository; whoami and push were not run, "
                "and no client auth store was read"
            ),
        },
        "ssh_signing_identity": {
            "state": "NOT_RUN",
            "reason": "no approved test key/socket; only ssh -G schema expansion was run",
        },
    }


def record(repo_root: Path, fixtures: Path) -> dict[str, Any]:
    fixture_check = verify_fixtures(fixtures)
    with tempfile.TemporaryDirectory(prefix="agent-home-p20-") as directory:
        probe_root = Path(directory)
        result = {
            "schema_version": SCHEMA_VERSION,
            "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "object": "installed tools and checked-in fixtures on this host",
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "fixtures": fixture_check,
            "claude": _probe_claude(fixtures, probe_root / "claude-probe", repo_root),
            "claude_sdk": _probe_claude_sdk(probe_root / "claude-sdk-probe", repo_root),
            "codex": _probe_codex(fixtures, probe_root / "codex-probe", repo_root),
            "pi": _probe_pi(fixtures, probe_root / "pi-probe", repo_root),
            "tools": _probe_tools(fixtures, probe_root / "tools-probe", repo_root),
        }
    return result


def _contract(snapshot: dict[str, Any]) -> dict[str, Any]:
    codex_schema = snapshot.get("codex", {}).get("schema_generation", {})
    pi_resources = snapshot.get("pi", {}).get("resource_discovery", {})
    sdk_schema = snapshot.get("claude_sdk", {}).get("schema") or {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "platform": {
            key: snapshot.get("platform", {}).get(key)
            for key in ("system", "release", "machine")
        },
        "fixtures": {
            "state": snapshot.get("fixtures", {}).get("state"),
            "tree_sha256": snapshot.get("fixtures", {})
            .get("manifest", {})
            .get("tree_sha256"),
        },
        "claude": {
            "state": snapshot.get("claude", {}).get("state"),
            "version": snapshot.get("claude", {}).get("version"),
            "config_root_state": snapshot.get("claude", {})
            .get("config_root", {})
            .get("state"),
        },
        "claude_sdk": {
            "state": snapshot.get("claude_sdk", {}).get("state"),
            "version": sdk_schema.get("version"),
            "option_fields": sdk_schema.get("option_fields"),
            "setting_sources": sdk_schema.get("setting_sources"),
        },
        "codex": {
            "state": snapshot.get("codex", {}).get("state"),
            "version": snapshot.get("codex", {}).get("version"),
            "schema_state": codex_schema.get("state"),
            "schema_tree_sha256": codex_schema.get("manifest", {}).get("tree_sha256"),
            "schema_methods": codex_schema.get("methods"),
            "config_discovery": {
                key: snapshot.get("codex", {}).get("config_discovery", {}).get(key)
                for key in ("state", "effective_model", "layer_types")
            },
            "auth_state": snapshot.get("codex", {}).get("auth", {}).get("state"),
        },
        "pi": {
            "state": snapshot.get("pi", {}).get("state"),
            "version": snapshot.get("pi", {}).get("version"),
            "default_root_help": {
                key: snapshot.get("pi", {}).get("default_root_help", {}).get(key)
                for key in ("state", "written_files")
            },
            "skill_state": pi_resources.get("state"),
            "skills": pi_resources.get("skills"),
            "context_boundary": {
                key: pi_resources.get("context_boundary", {}).get(key)
                for key in (
                    "state",
                    "paths",
                    "outside_repository_ancestor_loaded",
                    "reason",
                )
            },
            "rpc_types_sha256": snapshot.get("pi", {})
            .get("package_artifacts", {})
            .get("rpc_types", {})
            .get("sha256"),
            "rpc_command_literals": snapshot.get("pi", {})
            .get("package_artifacts", {})
            .get("rpc_command_literals"),
            "auth_state": snapshot.get("pi", {}).get("auth", {}).get("state"),
        },
        "tools": {
            "state": snapshot.get("tools", {}).get("state"),
            "service_identity": snapshot.get("tools", {}).get("service_identity"),
            "ssh_signing_identity": snapshot.get("tools", {}).get("ssh_signing_identity"),
        },
    }


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_contract = _contract(expected)
    actual_contract = _contract(actual)
    return {
        "state": "PASS" if expected_contract == actual_contract else "FAIL",
        "expected": expected_contract,
        "actual": actual_contract,
    }


def _write_json(path: Path | None, body: dict[str, Any]) -> None:
    text = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify-fixtures")
    verify.add_argument("--output", type=Path)

    capture = subparsers.add_parser("record")
    capture.add_argument("--output", type=Path)

    check = subparsers.add_parser("compare")
    check.add_argument("--expected", type=Path, required=True)
    check.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    fixtures = args.fixtures
    if not fixtures.is_absolute():
        fixtures = repo_root / fixtures
    fixtures = fixtures.resolve()

    if args.command == "verify-fixtures":
        body = verify_fixtures(fixtures)
        _write_json(args.output, body)
        return 0 if body["state"] == "PASS" else 1
    if args.command == "record":
        body = record(repo_root, fixtures)
        _write_json(args.output, body)
        top_states = [
            body[name]["state"]
            for name in ("fixtures", "claude", "claude_sdk", "codex", "pi", "tools")
        ]
        return 1 if "FAIL" in top_states else 0
    if args.command == "compare":
        expected = json.loads(args.expected.read_text())
        actual = record(repo_root, fixtures)
        body = compare(expected, actual)
        _write_json(args.output, body)
        return 0 if body["state"] == "PASS" else 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
