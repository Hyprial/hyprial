"""P23-pi controlled loader proof against the installed Pi SDK.

The test uses P20's native-format fixtures and Pi's real ``DefaultResourceLoader``.
It never invokes ``pi --help`` or a TUI, and every HOME/native root is temporary.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import compose_worker_child_launch
from hyprial.identity.impl.agents.runtime import (
    resolve_agent_runtime_context,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.pi.loader import (
    PI_CONTROLLED_LOADER,
    PI_SUPPORTED_MAX_VERSION,
    pi_version_supported,
    PI_LOADER_RECEIPT_SCHEMA,
    PiLoaderReceipt,
    PiProjectTrustReceipt,
    pi_loader_probe_argv,
    pi_sdk_launch_from_runtime_context,
)
from hyprial.daemon.impl.harnesses.pi.rpc_client import PI_HARNESS_BRIDGE_EXTENSION, PiRpcClient
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.identity.impl.provider_auth import HelperOutcome, ProviderAuthCoordinator
from hyprial.identity import UserProfileStore
from hyprial.daemon.impl.transfer.archive.session_files import (
    locate_session_file,
    session_target_path,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

NODE = shutil.which("node")
PI = shutil.which("pi")


def _pi_package_root() -> Path | None:
    if PI is None:
        return None
    candidate = Path(PI).resolve()
    for parent in (candidate, *candidate.parents):
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if body.get("name") == "@earendil-works/pi-coding-agent":
            return parent
    return None


PI_ROOT = _pi_package_root()
FIXTURE = Path(__file__).resolve().parents[5] / 'tests/fixtures/agent_home_p20/pi'

if os.environ.get("HYPRIAL_REQUIRE_PI_SDK") and (NODE is None or PI_ROOT is None):
    # CI installs the pinned SDK; a missing one there is a broken guard, not a skip.
    raise RuntimeError("HYPRIAL_REQUIRE_PI_SDK is set but node or the pi SDK is missing")

pytestmark = pytest.mark.skipif(
    NODE is None or PI_ROOT is None,
    reason="the real node and installed Pi SDK are required",
)


def _write_skill(root: Path, name: str, sentinel: str) -> Path:
    skill = root / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: isolated test sentinel\n---\n\n{sentinel}\n",
        encoding="utf-8",
    )
    return skill


def _split_native_roots(fixture: Path) -> tuple[Path, Path]:
    source = fixture / "native-root"
    projection_root = fixture / "projection-root"
    shutil.copytree(source, projection_root)
    (projection_root / "auth.json").unlink()
    native_root = fixture / "native-auth-root"
    native_root.mkdir()
    shutil.copy2(source / "auth.json", native_root / "auth.json")
    return projection_root, native_root


def _probe(tmp_path: Path) -> tuple[PiLoaderReceipt, Path, Path, Path, Path]:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cwd = repository / "nested"
    (cwd / ".pi").mkdir()
    shutil.copy2(repository / ".pi" / "settings.json", cwd / ".pi" / "settings.json")
    projection_root, native_root = _split_native_roots(fixture)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    # Pi independently discovers ~/.agents/skills.  Even an isolated HOME is
    # not an approved C/project source, so the controlled loader must exclude it.
    _write_skill(
        isolated_home / ".agents" / "skills",
        "p23-pi-home-decoy",
        "P23_PI_HOME_GLOBAL_SKILL_MUST_NOT_LOAD",
    )
    command = pi_loader_probe_argv(
        node=NODE or "node",
        pi_package_root=PI_ROOT or Path("/missing"),
        cwd=cwd,
        repository_root=repository,
        projection_root=projection_root,
        native_root=native_root,
        home_root=isolated_home,
    )
    completed = subprocess.run(
        command,
        check=True,
        cwd=cwd,
        env={
            "HOME": str(isolated_home),
            "PATH": os.environ.get("PATH", ""),
            "PI_CODING_AGENT_DIR": str(native_root),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert "P20_SENTINEL_NOT_A_SECRET" not in completed.stdout
    assert "P20_SENTINEL_NOT_A_SECRET" not in completed.stderr
    return (
        PiLoaderReceipt.from_text(completed.stdout),
        fixture,
        repository,
        cwd,
        isolated_home,
    )


def test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary(
    tmp_path: Path,
) -> None:
    receipt, fixture, repository, cwd, isolated_home = _probe(tmp_path)

    assert pi_version_supported(receipt.pi_package_version), (
        f"pi {receipt.pi_package_version} is outside the proven range "
        f"(<= {PI_SUPPORTED_MAX_VERSION})"
    )
    assert receipt.cwd == str(cwd.resolve())
    assert receipt.repository_root == str(repository.resolve())
    assert receipt.projection_root == str((fixture / "projection-root").resolve())
    assert receipt.native_root == str((fixture / "native-auth-root").resolve())
    assert receipt.home_root == str(isolated_home.resolve())
    assert receipt.project_trusted is True

    paths = [Path(item.path) for item in receipt.context_files]
    assert paths == [
        (fixture / "projection-root" / "AGENTS.md").resolve(),
        (repository / "AGENTS.md").resolve(),
        (cwd / "AGENTS.md").resolve(),
    ]
    assert (fixture / "workspace" / "AGENTS.md").resolve() not in paths
    for item in receipt.context_files:
        contents = Path(item.path).read_bytes()
        assert item.digest == hashlib.sha256(contents).hexdigest()
        assert item.size == len(contents)
    assert receipt.session_proof.present_context_digests == tuple(
        item.digest for item in receipt.context_files
    )
    outside_digest = hashlib.sha256(
        (fixture / "workspace" / "AGENTS.md").read_bytes()
    ).hexdigest()
    assert outside_digest not in receipt.session_proof.present_context_digests

    assert {(skill.scope, skill.name) for skill in receipt.skills} == {
        ("native", "p20-pi-user"),
        ("project", "p20-pi-project"),
    }
    assert all("p23-pi-home-decoy" not in skill.path for skill in receipt.skills)
    assert receipt.effective_settings.default_provider == "openai"
    assert receipt.effective_settings.default_model == "gpt-4o-mini"
    assert receipt.effective_settings.default_thinking_level == "low"


def test_t04_non_git_boundary_is_rejected_instead_of_walking_to_root(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    projection_root, native_root = _split_native_roots(fixture)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    command = pi_loader_probe_argv(
        node=NODE or "node",
        pi_package_root=PI_ROOT or Path("/missing"),
        cwd=repository / "nested",
        repository_root=repository,
        projection_root=projection_root,
        native_root=native_root,
        home_root=isolated_home,
    )
    completed = subprocess.run(
        command,
        check=False,
        cwd=repository,
        env={"HOME": str(isolated_home), "PATH": os.environ.get("PATH", "")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "repositoryRoot has no .git boundary" in completed.stderr
    assert completed.stdout == ""


def test_t04_workspace_trust_scope_accepts_a_private_non_git_root(
    tmp_path: Path,
) -> None:
    """The agent's own 0700 workspace is trusted without a .git boundary.

    Defect B: the P2 default cwd is the agent-home workspace, which is not a
    Git repository by design.  ``trustScope: workspace`` is the one bounded
    exception; the loader must not fall through to the .git rejection.
    """

    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    workspace = tmp_path / "agents" / "be-dev-1" / "workspace"
    workspace.mkdir(parents=True)
    os.chmod(workspace, 0o700)
    projection_root, native_root = _split_native_roots(fixture)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    command = pi_loader_probe_argv(
        node=NODE or "node",
        pi_package_root=PI_ROOT or Path("/missing"),
        cwd=workspace,
        repository_root=workspace,
        projection_root=projection_root,
        native_root=native_root,
        home_root=isolated_home,
        trust_scope="workspace",
    )
    completed = subprocess.run(
        command,
        check=False,
        cwd=workspace,
        env={"HOME": str(isolated_home), "PATH": os.environ.get("PATH", "")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert "no .git boundary" not in completed.stderr
    assert completed.returncode == 0, completed.stderr


def test_t04_context_symlink_is_rejected_before_native_consumption(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    projection_root, native_root = _split_native_roots(fixture)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    context = repository / "nested" / "AGENTS.md"
    target = tmp_path / "outside-agents.md"
    target.write_text("P23_PI_OUTSIDE_SYMLINK\n", encoding="utf-8")
    context.unlink()
    context.symlink_to(target)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    command = pi_loader_probe_argv(
        node=NODE or "node",
        pi_package_root=PI_ROOT or Path("/missing"),
        cwd=repository / "nested",
        repository_root=repository,
        projection_root=projection_root,
        native_root=native_root,
        home_root=isolated_home,
    )
    completed = subprocess.run(
        command,
        check=False,
        cwd=repository,
        env={"HOME": str(isolated_home), "PATH": os.environ.get("PATH", "")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "context candidate is not a regular non-symlink file" in completed.stderr


def test_t06_dynamic_package_is_rejected_before_native_resolution(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    projection_root, native_root = _split_native_roots(fixture)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    settings_path = projection_root / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["packages"] = ["npm:not-installed@1.0.0"]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    command = pi_loader_probe_argv(
        node=NODE or "node",
        pi_package_root=PI_ROOT or Path("/missing"),
        cwd=repository / "nested",
        repository_root=repository,
        projection_root=projection_root,
        native_root=native_root,
        home_root=isolated_home,
    )
    completed = subprocess.run(
        command,
        check=False,
        cwd=repository,
        env={"HOME": str(isolated_home), "PATH": os.environ.get("PATH", "")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "dynamic resource settings require a frozen dependency closure: packages" in completed.stderr
    assert not (projection_root / "npm").exists()


def _run_sdk_rpc(
    payload: dict[str, object],
    cwd: Path,
    environment: dict[str, str],
    *,
    loader: Path = PI_CONTROLLED_LOADER,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            NODE or "node",
            str(loader),
            "run-rpc",
            json.dumps(payload, separators=(",", ":")),
        ],
        input=(
            '{"id":"state","type":"get_state"}\n'
            '{"id":"commands","type":"get_commands"}\n'
            '{"id":"name","type":"set_session_name","name":"P23 Pi RPC"}\n'
        ),
        check=True,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert "P20_SENTINEL_NOT_A_SECRET" not in completed.stdout
    assert "P20_SENTINEL_NOT_A_SECRET" not in completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    return {str(row.get("id")): row for row in rows if row.get("type") == "response"}


def test_t13_real_sdk_rpc_uses_explicit_session_root_and_cold_resume(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    projection_root, native_root = _split_native_roots(fixture)
    cwd = repository / "nested"
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    existing_session = session_root / "2026-09-23T00-00-00-000Z_p23-pi-rpc-session.jsonl"
    existing_session.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "p23-pi-rpc-session",
                "timestamp": "2026-09-23T00:00:00.000Z",
                "cwd": str(cwd),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    receipt_path = session_root / "receipts" / "rpc.json"
    payload: dict[str, object] = {
        "piPackageRoot": str(PI_ROOT),
        "cwd": str(cwd),
        "repositoryRoot": str(repository),
        "projectionRoot": str(projection_root),
        "nativeRoot": str(native_root),
        "homeRoot": str(isolated_home),
        "projectTrusted": True,
        "sessionRoot": str(session_root),
        "sessionId": "p23-pi-rpc-session",
        "sessionName": "P23 Pi RPC",
        "receiptPath": str(receipt_path),
    }
    environment = {
        "HOME": str(isolated_home),
        "PATH": os.environ.get("PATH", ""),
        "PI_CODING_AGENT_DIR": str(native_root),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }

    # Node canonicalizes a symlinked main-module path in ``import.meta.url``.
    # The loader's entry guard must canonicalize argv too; otherwise it returns
    # rc=0 without reading RPC stdin, which made /tmp -> /private/tmp launches
    # reconnect forever without reaching ready on macOS.
    loader_alias = tmp_path / "pi-controlled-loader.mjs"
    loader_alias.symlink_to(PI_CONTROLLED_LOADER)
    first = _run_sdk_rpc(payload, cwd, environment, loader=loader_alias)
    assert first["state"]["success"] is True
    assert first["state"]["data"]["sessionId"] == "p23-pi-rpc-session"
    assert first["state"]["data"]["model"]["provider"] == "openai"
    assert first["state"]["data"]["model"]["id"] == "gpt-4o-mini"
    first_session_file = Path(first["state"]["data"]["sessionFile"])
    assert first_session_file == existing_session
    assert first_session_file.is_relative_to(session_root)
    assert first["commands"]["success"] is True
    receipt = PiLoaderReceipt.from_text(receipt_path.read_text(encoding="utf-8"))
    assert receipt.projection_root == str(projection_root.resolve())
    assert receipt.native_root == str(native_root.resolve())

    second = _run_sdk_rpc(payload, cwd, environment)
    assert second["state"]["success"] is True
    assert second["state"]["data"]["sessionId"] == "p23-pi-rpc-session"
    assert Path(second["state"]["data"]["sessionFile"]) == first_session_file


def test_loader_receipt_rejects_unknown_or_secret_shaped_fields(tmp_path: Path) -> None:
    receipt, *_ = _probe(tmp_path)
    payload = {
        "schema": PI_LOADER_RECEIPT_SCHEMA,
        "piPackageVersion": receipt.pi_package_version,
        "cwd": receipt.cwd,
        "repositoryRoot": receipt.repository_root,
        "projectionRoot": receipt.projection_root,
        "nativeRoot": receipt.native_root,
        "homeRoot": receipt.home_root,
        "projectTrusted": True,
        "contextFiles": [],
        "skills": [],
        "effectiveSettings": {
            "defaultProvider": None,
            "defaultModel": None,
            "defaultThinkingLevel": None,
        },
        "sessionProof": {
            "systemPromptDigest": "0" * 64,
            "presentContextDigests": [],
        },
        "auth": {"apiKey": "must-never-be-here"},
    }
    with pytest.raises(ValueError, match=r"unknown=\['auth'\]"):
        PiLoaderReceipt.from_json(payload)


def _real_p22_context(tmp_path: Path, *, seeded_settings: bool = True):
    repository = tmp_path / "repo"
    cwd = repository / "nested"
    cwd.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = tmp_path / "personality"
    settings = config / "native" / "pi" / "settings.json"
    settings.parent.mkdir(parents=True)
    if seeded_settings:
        settings.write_text(
            '{"defaultProvider":"openai","defaultModel":"gpt-4o-mini"}\n',
            encoding="utf-8",
        )
    context_file = config / "native" / "pi" / "AGENTS.md"
    context_file.write_text("P23_PI_P22_CONTEXT\n", encoding="utf-8")
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir()
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="p23-owner",
        machine="p23-node",
        hyprial_home=hyprial_home,
    )
    registry.create(
        "worker",
        cwd=str(cwd),
        config={
            "sources": [{"path": str(config), "required": True}],
            "discovery": "explicit-only",
        },
    )
    registry.ensure_home("worker")
    context = resolve_agent_runtime_context(
        registry=registry,
        agent_name="worker",
        harness="pi",
        cwd=str(cwd),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    channel = build_worker_channel(
        actor=registry.require("worker").uri,
        session_ref="p23-channel",
        hyprial_home=hyprial_home,
        state_dir=tmp_path / "daemon-state",
        runtime_context=context,
    )
    launch = compose_worker_child_launch(
        registry=registry,
        hyprial_home=hyprial_home,
        channel=channel,
        environ={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/ambient"},
        agent_name="worker",
    )
    return registry, repository, cwd, context, channel, launch


@pytest.mark.parametrize("seeded_settings", [False, True])
def test_real_sdk_settings_setters_do_not_write_frozen_projection(
    tmp_path: Path, seeded_settings: bool,
) -> None:
    registry, repository, cwd, context, _channel, _complete = _real_p22_context(
        tmp_path, seeded_settings=seeded_settings,
    )
    try:
        root = context.roots.projection_root
        def snapshot():
            return {
                str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
                for path in root.rglob("*") if path.is_file()
            }
        before = snapshot()
        options = {
            "piPackageRoot": str(PI_ROOT), "cwd": str(cwd),
            "repositoryRoot": str(repository), "projectTrusted": True,
            "projectionRoot": str(root), "nativeRoot": str(context.roots.native_root),
            "homeRoot": str(context.roots.tool_home),
        }
        script = """
import { pathToFileURL } from 'node:url';
const loaderPath = process.argv[1];
process.argv[1] = 'isolated-sdk-test';
const { createControlledPiResourceLoader } = await import(pathToFileURL(loaderPath));
const controlled = await createControlledPiResourceLoader(JSON.parse(process.argv[2]));
const settings = controlled.settingsManager;
const { session } = await controlled.sdk.createAgentSession({
  cwd: controlled.options.cwd, agentDir: controlled.options.nativeRoot,
  settingsManager: settings, resourceLoader: controlled.loader,
  sessionManager: controlled.sdk.SessionManager.inMemory(controlled.options.cwd),
});
try {
  session.setThinkingLevel('low', { persist: true });
  settings.setDefaultModelAndProvider('openai', 'gpt-4o-mini');
  await settings.flush();
} finally {
  session.dispose();
}
if (settings.drainErrors().length) throw new Error('settings persistence failed');
if (settings.getDefaultThinkingLevel() !== 'low') throw new Error('setter ineffective');
console.log(JSON.stringify({thinking: settings.getDefaultThinkingLevel()}));
await controlled.loader.reload();
if (settings.getDefaultThinkingLevel() !== undefined) throw new Error('reload did not restore C');
"""
        completed = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(PI_CONTROLLED_LOADER), json.dumps(options)],
            cwd=cwd, env={
                "HOME": str(context.roots.tool_home), "PATH": os.environ.get("PATH", ""),
                "PI_CODING_AGENT_DIR": str(context.roots.native_root),
                "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
            }, capture_output=True, text=True, timeout=30, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["thinking"] == "low"
        assert snapshot() == before, "Pi setters must not persist into frozen C projection"
        assert resolve_agent_runtime_context(
            registry=registry, agent_name="worker", harness="pi", cwd=str(cwd),
            tool_profile=TEST_TOOL_PROFILE,
        ) == context
    finally:
        registry.close()


@pytest.mark.skipif(os.name == "nt", reason="legacy 0644 reproduction requires POSIX modes")
@pytest.mark.parametrize("chmod_before_rebind", [False, True])
def test_operator_rebind_recovers_real_pi_corruption_without_erasing_evidence(
    tmp_path: Path, chmod_before_rebind: bool,
):
    from hyprial.identity.impl.agents import AgentConfigError
    from hyprial.identity.impl.agents.config_rebind.service import run_config_rebind

    registry, _repository, cwd, context, _channel, _complete = _real_p22_context(
        tmp_path, seeded_settings=False,
    )
    class QuiescentOwner:
        # No process or daemon is started in this test. Only the custody port is
        # supplied; the rebind saga, filesystem, registry and binding CAS are real.
        require = registry.require
        home_receipt = registry.home_receipt

        def quiescence(self, _actor):
            return {"quiescent": True}

        def bind_config_quiescent(self, actor, *, config, expected_current_config_digest):
            bound = registry.bind_agent_config(
                actor, config=config, expected_entity_token=registry.require(actor).entity_token,
                expected_current_config_digest=expected_current_config_digest,
            )
            return bound, self.quiescence(actor)

    try:
        source = Path(registry.require("worker").config.source)
        for path in (source, *source.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        # Exercise the actual legacy SDK write, not a fabricated manifest mode.
        script = """
import { pathToFileURL } from 'node:url';
const { SettingsManager } = await import(pathToFileURL(process.argv[1] + '/dist/index.js'));
process.umask(0o022);
const settings = SettingsManager.create(process.argv[2], process.argv[3], {projectTrusted:true});
settings.setDefaultThinkingLevel('low');
await settings.flush();
if (settings.drainErrors().length) throw Error('legacy write failed');
"""
        subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(PI_ROOT), str(cwd),
             str(context.roots.projection_root)], check=True, capture_output=True, timeout=30,
            env={"HOME": str(context.roots.tool_home), "PATH": os.environ.get("PATH", ""),
                 "PI_OFFLINE": "1", "PI_TELEMETRY": "0"},
        )
        def resolve():
            return resolve_agent_runtime_context(
                registry=registry, agent_name="worker", harness="pi", cwd=str(cwd),
                tool_profile=TEST_TOOL_PROFILE,
            )
        path = context.roots.projection_root / "settings.json"
        with pytest.raises(AgentConfigError, match="settings.json mode must be 0600"):
            resolve()
        if chmod_before_rebind:
            path.chmod(0o600)
            with pytest.raises(AgentConfigError, match="drifted after publication: settings.json"):
                resolve()
        corrupt_bytes = path.read_bytes()
        corrupt_mode = path.stat().st_mode
        original = registry.require("worker")
        preview = run_config_rebind(
            QuiescentOwner(), "worker", source=str(source), rollback_receipt_id=None, dry_run=True,
        )
        assert preview["projectionChanges"] == [{
            "harness": "pi", "path": "settings.json",
            "changes": ["unexpected"] if chmod_before_rebind else ["unexpected", "mode"],
            "action": "omit-from-new-projection",
        }]
        assert registry.require("worker") == original
        result = run_config_rebind(
            QuiescentOwner(), "worker", source=str(source), rollback_receipt_id=None, dry_run=False,
        )
        assert result["projectionChanges"] == preview["projectionChanges"]
        recovered = resolve()
        assert recovered.roots.projection_root != context.roots.projection_root
        assert not (recovered.roots.projection_root / "settings.json").exists()
        assert path.read_bytes() == corrupt_bytes  # old evidence is not repaired in place
        assert path.stat().st_mode == corrupt_mode
        assert registry.require("worker").entity_token == original.entity_token
    finally:
        registry.close()


def test_p22_context_consumer_uses_all_three_roots_without_derivation(
    tmp_path: Path,
) -> None:
    registry, repository, cwd, context, _channel, _complete = _real_p22_context(
        tmp_path
    )
    try:
        launch = pi_sdk_launch_from_runtime_context(
            context,
            trust=PiProjectTrustReceipt(cwd, repository, True),
            mode="rpc",
            session_id="session:one",
            pi_package_root=PI_ROOT or Path("/missing"),
        )
        payload = json.loads(launch.argv[3])
        assert payload["projectionRoot"] == str(context.roots.projection_root)
        assert payload["nativeRoot"] == str(context.roots.native_root)
        assert payload["sessionRoot"] == str(context.roots.session_root)
        assert payload["homeRoot"] == str(context.roots.tool_home)
        assert payload["cwd"] == str(cwd)
        assert payload["repositoryRoot"] == str(repository)
        assert launch.environment == context.environment()
        assert launch.receipt_path.is_relative_to(context.roots.session_root)
    finally:
        registry.close()


def test_p22_context_consumer_rejects_native_root_environment_drift(
    tmp_path: Path,
) -> None:
    registry, repository, cwd, context, _channel, _complete = _real_p22_context(
        tmp_path
    )
    try:
        environment = context.environment()
        environment["PI_CODING_AGENT_DIR"] = str(tmp_path / "wrong-native")
        drifted = replace(context, environment_items=tuple(sorted(environment.items())))
        with pytest.raises(ValueError, match="does not bind Pi native root"):
            pi_sdk_launch_from_runtime_context(
                drifted,
                trust=PiProjectTrustReceipt(cwd, repository, True),
                mode="rpc",
                session_id="session-one",
                pi_package_root=PI_ROOT or Path("/missing"),
            )
    finally:
        registry.close()


def test_headless_pi_client_uses_p22_context_and_sdk_bridge(tmp_path: Path) -> None:
    registry, repository, cwd, context, channel, complete = _real_p22_context(
        tmp_path
    )
    try:
        spec = HarnessLaunchSpec(
            "pi",
            "worker",
            True,
            args=("--approve",),
            cwd=str(cwd),
            model_provider="openai",
            model="gpt-4o-mini",
        )
        client = PiRpcClient(
            spec,
            "p23:session",
            command=(PI or "pi",),
            worker_channel=channel,
            complete_launch=complete,
        )
        assert client.command[0] == "node"
        assert client.command[1] == str(PI_CONTROLLED_LOADER)
        assert client.command[2] == "run-rpc"
        payload = json.loads(client.command[3])
        assert payload["projectionRoot"] == str(context.roots.projection_root)
        assert payload["nativeRoot"] == str(context.roots.native_root)
        assert payload["sessionRoot"] == str(context.roots.session_root)
        assert payload["repositoryRoot"] == str(repository)
        assert payload["sessionId"] == "p23.session"
        assert payload["additionalExtensionPaths"] == [
            str(PI_HARNESS_BRIDGE_EXTENSION)
        ]
        assert client._env == complete.environment.for_exec()
    finally:
        registry.close()


def test_p2_headless_pi_refuses_missing_project_approval(tmp_path: Path) -> None:
    registry, _repository, cwd, _context, channel, complete = _real_p22_context(
        tmp_path
    )
    try:
        spec = HarnessLaunchSpec(
            "pi",
            "worker",
            True,
            cwd=str(cwd),
            model_provider="openai",
            model="gpt-4o-mini",
        )
        with pytest.raises(ValueError, match="requires explicit --approve"):
            PiRpcClient(
                spec,
                "p23-session",
                command=(PI or "pi",),
                worker_channel=channel,
                complete_launch=complete,
            )
    finally:
        registry.close()


def test_t13_transfer_locator_consumes_p22_session_root(tmp_path: Path) -> None:
    registry, _repository, cwd, context, _channel, _complete = _real_p22_context(
        tmp_path
    )
    session_ref = "p23-transfer"
    session_dir = context.roots.session_root
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"2026-09-23T00-00-00-000Z_{session_ref}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_ref,
                "timestamp": "2026-09-23T00:00:00.000Z",
                "cwd": str(cwd),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        assert locate_session_file(
            "pi",
            str(cwd),
            session_ref,
            home=tmp_path / "ambient-home",
            runtime_context=context,
        ) == session_file
        target = session_target_path(
            "pi",
            str(cwd),
            Path("/source") / session_file.name,
            session_ref,
            home=tmp_path / "ambient-home",
            runtime_context=context,
        )
        assert target == session_file
    finally:
        registry.close()


class _RouteRunner:
    def __init__(self, environment: dict[str, str]) -> None:
        self.environment = environment
        self.calls: list[str] = []

    def __call__(self, provider: str, _on_device_code, *, stop=None):
        self.calls.append(provider)
        return HelperOutcome.FAILED


def test_t14_provider_auth_routes_two_agents_to_their_own_native_roots(
    tmp_path: Path,
) -> None:
    registry_a, *_rest_a, context_a, _channel_a, _launch_a = _real_p22_context(
        tmp_path / "a"
    )
    registry_b, *_rest_b, context_b, _channel_b, _launch_b = _real_p22_context(
        tmp_path / "b"
    )
    store = UserProfileStore(tmp_path / "users.json")
    store.ensure(
        owner="p23-owner",
        owner_key="owner-key",
        login_name="unit",
        machine="p23-node",
        machine_key="machine-key",
    )
    legacy = _RouteRunner({})
    routes: dict[str, _RouteRunner] = {}
    log_events: list[str] = []

    def factory(context):
        environment = context.environment()
        runner = _RouteRunner(environment)
        routes[context.entity_token] = runner
        return runner

    valid_tokens = {context_a.entity_token, context_b.entity_token}
    coordinator = ProviderAuthCoordinator(
        profile_store=store,
        owner_key="owner-key",
        notifier=lambda _text, *, idempotency_key: idempotency_key,
        helper_runner=legacy,
        auth_path=tmp_path / "legacy-auth.json",
        host="test-host",
        stop=threading.Event(),
        runtime_helper_factory=factory,
        runtime_context_validator=lambda context: context.entity_token
        in valid_tokens,
        logger=lambda event, **_fields: log_events.append(event),
    )
    failure = (
        "OAuth refresh failed for openai-codex: OpenAI Codex token refresh "
        "unauthorized (status 400): The provided authorization grant is invalid"
    )
    try:
        for worker, context in (("worker-a", context_a), ("worker-b", context_b)):
            coordinator.handle_turn_failure(
                failure,
                harness="pi",
                provider="openai-codex",
                model="gpt-5.1-codex",
                worker=worker,
                runtime_context=context,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            not routes.get(context.entity_token)
            or not routes[context.entity_token].calls
            for context in (context_a, context_b)
        ):
            time.sleep(0.01)
        assert legacy.calls == []
        assert routes[context_a.entity_token].calls == ["openai-codex"]
        assert routes[context_b.entity_token].calls == ["openai-codex"]
        assert routes[context_a.entity_token].environment[
            "PI_CODING_AGENT_DIR"
        ] == str(context_a.roots.native_root)
        assert routes[context_b.entity_token].environment[
            "PI_CODING_AGENT_DIR"
        ] == str(context_b.roots.native_root)
        profile = store.get("owner-key")
        assert profile is not None
        assert profile.runtime_capabilities == ()

        stale = replace(context_a, entity_token="stale-incarnation")
        coordinator.handle_turn_failure(
            failure,
            harness="pi",
            provider="openai-codex",
            model="gpt-5.1-codex",
            worker="stale",
            runtime_context=stale,
        )
        assert "stale-incarnation" not in routes
        assert "provider.auth.runtime-context.rejected" in log_events
    finally:
        registry_a.close()
        registry_b.close()


def test_shipped_controlled_loader_is_package_data() -> None:
    assert PI_CONTROLLED_LOADER.is_file()
    assert PI_CONTROLLED_LOADER.name == "pi_controlled_loader.mjs"
