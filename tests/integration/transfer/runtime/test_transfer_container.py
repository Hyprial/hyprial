"""Container-mode transfer: runtime argv, credential pipeline, spec fields.

Covers ``src/hyprial/transfer/container.py`` and the containerized paths of
the harness clients (design
``docs/design/design-transfer-container.md``).  The security invariants under
test: credentials never enter argv/env/image, the container sees no host
HOME and no API-key environment (the no-fallback mutation criterion), and
retirement removes the volume/container/home (D-A).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Sequence

import pytest

from hyprial.kernel import DesiredStateError

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.transfer.execution.container import (
    CONTAINER_HOME,
    CONTAINER_PI_EXTENSION,
    CONTAINER_SDK_WORKER,
    ContainerError,
    DockerRunner,
    credential_bundle,
    docker_run_argv,
    prepare_worker,
    prune_worker,
    session_mount,
    staging_dir,
    volume_name,
    worker_home,
    wrap_worker_launch)

REPO_ROOT = Path(__file__).resolve().parents[4]


# -- credential bundle -------------------------------------------------------


def _make_home(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def test_credential_bundle_claude(tmp_path: Path) -> None:
    home = _make_home(tmp_path, {".claude/.credentials.json": "{}"})
    bundle = credential_bundle("claude", home)
    assert bundle == {".claude/.credentials.json": home / ".claude/.credentials.json"}


def test_credential_bundle_codex_optional_file(tmp_path: Path) -> None:
    home = _make_home(tmp_path, {".codex/auth.json": "{}"})
    assert set(credential_bundle("codex", home)) == {".codex/auth.json"}
    _make_home(tmp_path, {".codex/.credentials.json": "{}"})
    assert set(credential_bundle("codex", home)) == {
        ".codex/auth.json",
        ".codex/.credentials.json",
    }


def test_credential_bundle_missing_required_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ContainerError) as caught:
        credential_bundle("claude", tmp_path)
    assert caught.value.code == "TRANSFER_CREDENTIALS"
    assert "Keychain" in str(caught.value)  # macOS export guidance


def test_credential_bundle_unknown_harness(tmp_path: Path) -> None:
    with pytest.raises(ContainerError):
        credential_bundle("dsh", tmp_path)


# -- path derivation ----------------------------------------------------------


def test_path_derivation(tmp_path: Path) -> None:
    assert volume_name("wk").startswith("hyprial-xfer-cred-")
    assert volume_name("wk") == volume_name("wk")
    assert volume_name("wk") != volume_name("wk2")
    staging = staging_dir(tmp_path, "wk")
    assert staging.parent == tmp_path / "xfer-cred"
    home = worker_home(tmp_path, "claude", "wk")
    assert home == tmp_path / "container-workers" / "claude-wk" / "home"
    host, container_path = session_mount("claude", home)
    assert host == home / ".claude" / "projects"
    assert container_path == f"{CONTAINER_HOME}/.claude/projects"
    with pytest.raises(ContainerError):
        session_mount("dsh", home)


# -- docker_run_argv ------------------------------------------------------------


def _argv(**overrides: Any) -> tuple[str, ...]:
    params: dict[str, Any] = {
        "harness": "claude",
        "name": "wk",
        "image": "hyprial-worker:test",
        "inner_argv": ("claude-worker", "--flag"),
        "env_delta": {"HYPRIAL_WORKER_ACTOR": "agent:o:m:wk", "HYPRIAL_HOME": "/h"},
        "state_dir": Path("/state"),
        "cwd": "/work/proj",
        "home": Path("/state/container-workers/claude-wk/home"),
        "cred_volume": "hyprial-xfer-cred-abc",
        "uid": 501,
        "gid": 20,
        "environ": {
            "HTTP_PROXY": "http://proxy:7890",
            "ANTHROPIC_API_KEY": "sk-secret",
            "OPENAI_API_KEY": "sk-secret",
            "HOME": "/Users/alice",
        },
    }
    params.update(overrides)
    return docker_run_argv(**params)


def test_docker_run_argv_mounts_and_env() -> None:
    argv = _argv()
    assert argv[0:3] == ("docker", "run", "-i")
    assert "--rm" in argv
    joined = " ".join(argv)
    # worktree and state dir bind at their SAME absolute paths
    assert "-v /work/proj:/work/proj" in joined
    assert "-v /state:/state" in joined
    # session dir rw from the worker home; cred volume read-only
    assert (
        f"-v /state/container-workers/claude-wk/home/.claude/projects:"
        f"{CONTAINER_HOME}/.claude/projects" in joined
    )
    assert "-v hyprial-xfer-cred-abc:/hyprial-cred:ro" in joined
    assert f"--tmpfs {CONTAINER_HOME}:rw,uid=501,gid=20" in joined
    # proxy whitelisted; API keys and host HOME never enter the container
    assert "-e HTTP_PROXY" in joined
    assert "http://proxy:7890" not in joined
    assert "ANTHROPIC_API_KEY" not in joined
    assert "OPENAI_API_KEY" not in joined
    assert "-v /Users/alice" not in joined
    assert joined.index("-e HOME=" + CONTAINER_HOME) >= 0
    # the worker identity delta rides -e flags; inner argv tails the image
    assert "-e HYPRIAL_WORKER_ACTOR" in joined
    assert "agent:o:m:wk" not in joined
    assert argv[-3:] == ("hyprial-worker:test", "claude-worker", "--flag")


def test_docker_run_argv_without_credentials_mounts_nothing(tmp_path: Path) -> None:
    argv = _argv(cred_volume=None)
    assert "/hyprial-cred" not in " ".join(argv)


def test_wrap_worker_launch_uses_spec_fields(tmp_path: Path) -> None:
    spec = HarnessLaunchSpec(
        harness="pi",
        name="wk",
        headless=True,
        cwd="/work/proj",
        containerized=True,
        container_image="hyprial-worker:custom",
    )
    argv = wrap_worker_launch(
        spec, inner_argv=("pi", "--mode", "rpc"), env_delta={}, state_dir=tmp_path
    )
    joined = " ".join(argv)
    assert "hyprial-worker:custom" in joined
    assert f"-v {tmp_path}:{tmp_path}" in joined
    assert f"{CONTAINER_HOME}/.pi/agent/sessions" in joined
    assert volume_name("wk") in joined


# -- argv[0] rewrite (review §A: #259's resolved_command pins HOST paths) -------


def test_rewrite_inner_argv_replaces_host_path_with_image_binary() -> None:
    from hyprial.daemon.impl.transfer.execution.container import rewrite_inner_argv

    # The launcher feeds resolved_command's host absolute path; inside the
    # container only the image's PATH-pinned binary exists.
    assert rewrite_inner_argv(
        "codex", ("/Users/amyws/.local/bin/codex", "app-server", "--stdio")
    ) == ("codex", "app-server", "--stdio")
    assert rewrite_inner_argv("pi", ("/opt/homebrew/bin/pi", "--mode", "rpc")) == (
        "pi",
        "--mode",
        "rpc",
    )
    # Bare names and image-fixed paths pass through untouched.
    assert rewrite_inner_argv("pi", ("pi",)) == ("pi",)
    assert rewrite_inner_argv(
        "claude", ("/opt/hyprial/sdk-venv/bin/python", "/opt/hyprial/_agent_sdk_worker.py")
    ) == ("/opt/hyprial/sdk-venv/bin/python", "/opt/hyprial/_agent_sdk_worker.py")
    with pytest.raises(ContainerError):
        rewrite_inner_argv("pi", ())


def test_wrap_worker_launch_host_pinned_command_never_leaks(tmp_path: Path) -> None:
    """The §A pin: a spec carrying a host-pinned command MUST launch the
    image binary.  Removing rewrite_inner_argv from wrap_worker_launch
    turns this red (the host path would tail the argv)."""

    spec = HarnessLaunchSpec(
        harness="codex",
        name="wk",
        headless=True,
        cwd="/work/proj",
        containerized=True,
        command=("/Users/amyws/.local/bin/codex",),
    )
    inner = (*spec.resolved_command(("codex",)), "app-server", "--stdio")
    assert inner[0] == "/Users/amyws/.local/bin/codex"  # the #259 behaviour
    argv = wrap_worker_launch(spec, inner_argv=inner, env_delta={}, state_dir=tmp_path)
    assert "/Users/amyws/.local/bin/codex" not in " ".join(argv)
    assert argv[-3:] == ("codex", "app-server", "--stdio")


# -- env whitelist (review O1: delta keys ⊆ whitelist, not just known secrets absent)


def _docker_env_keys(argv: tuple[str, ...]) -> set[str]:
    keys: set[str] = set()
    for index, arg in enumerate(argv):
        if arg == "-e":
            keys.add(argv[index + 1].split("=", 1)[0])
    return keys


def _assert_env_whitelist(argv: tuple[str, ...]) -> None:
    from hyprial.daemon.impl.transfer.execution.container import (
        CONTAINER_ENV_FIXED,
        CONTAINER_ENV_PREFIXES,
        PASSTHROUGH_ENV)

    allowed = set(CONTAINER_ENV_FIXED) | set(PASSTHROUGH_ENV)
    for key in _docker_env_keys(argv):
        assert key in allowed or key.startswith(CONTAINER_ENV_PREFIXES), key


def test_pi_client_env_delta_within_whitelist(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    client = PiRpcClient(
        _container_spec("pi", tmp_path), "ref-1", worker_channel=_channel(tmp_path)
    )
    _assert_env_whitelist(client.command)


def test_codex_client_env_delta_within_whitelist(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    client = CodexAppServerClient(
        _container_spec("codex", tmp_path),
        session_ref="ref-1",
        worker_channel=_channel(tmp_path),
    )
    _assert_env_whitelist(client.command)


def test_claude_client_env_delta_within_whitelist(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient

    client = IsolatedAgentSdkClient(
        _container_spec("claude", tmp_path), worker_channel=_channel(tmp_path)
    )
    _assert_env_whitelist(client.command)


# -- prepare/prune with a fake docker ------------------------------------------


class FakeDocker(DockerRunner):
    def __init__(self, *, image_present: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.present = image_present
        self.containers = ["abc123"] if image_present else []

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> None:
        self.calls.append(list(argv))
        if argv[:2] == ["load", "-i"]:
            self.present = True

    def output(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        if argv[:2] == ["ps", "-aq"]:
            return "\n".join(self.containers)
        return ""

    def image_present(self, image: str) -> bool:
        return self.present


def test_prepare_worker_loads_image_and_shreds_staging(tmp_path: Path) -> None:
    staging = staging_dir(tmp_path, "wk")
    (staging / ".claude").mkdir(parents=True)
    (staging / ".claude" / ".credentials.json").write_text("{}")
    (staging / "image.tar").write_bytes(b"tar")
    docker = FakeDocker(image_present=False)
    volume = prepare_worker(
        docker,
        image="hyprial-worker:test",
        name="wk",
        staging=staging,
        state_dir=tmp_path,
        harness="claude",
    )
    assert volume == volume_name("wk")
    joined = [" ".join(call) for call in docker.calls]
    assert any(call.startswith("load -i") for call in joined)
    assert any(call.startswith("volume create") for call in joined)
    populate = next(call for call in joined if "/cred" in call and "run" in call)
    assert "image.tar" in populate  # excluded from the volume copy
    assert not staging.exists()  # shredded: no credential residue on host
    home = worker_home(tmp_path, "claude", "wk")
    assert (home / ".claude" / "projects").is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_prepare_worker_no_credentials_skips_volume(tmp_path: Path) -> None:
    staging = staging_dir(tmp_path, "wk")
    staging.mkdir(parents=True)
    docker = FakeDocker()
    prepare_worker(
        docker,
        image="hyprial-worker:test",
        name="wk",
        staging=staging,
        state_dir=tmp_path,
        harness="pi",
        credentials=False,
    )
    joined = [" ".join(call) for call in docker.calls]
    assert not any("volume create" in call for call in joined)


def test_prune_worker_removes_everything(tmp_path: Path) -> None:
    home = worker_home(tmp_path, "claude", "wk").parent
    home.mkdir(parents=True)
    docker = FakeDocker()
    problems = prune_worker(
        docker, harness="claude", name="wk", state_dir=tmp_path
    )
    assert problems == []
    joined = [" ".join(call) for call in docker.calls]
    assert any("rm -f abc123" in call for call in joined)
    assert any(f"volume rm -f {volume_name('wk')}" in call for call in joined)
    assert not home.exists()


def test_prune_worker_keep_volume(tmp_path: Path) -> None:
    docker = FakeDocker()
    prune_worker(docker, harness="claude", name="wk", keep_volume=True)
    joined = [" ".join(call) for call in docker.calls]
    assert not any("volume rm" in call for call in joined)


# -- spec fields -----------------------------------------------------------------


def test_spec_container_fields_round_trip() -> None:
    spec = HarnessLaunchSpec(
        harness="claude",
        name="wk",
        headless=True,
        containerized=True,
        pinned_owner="amyws",
        container_image="hyprial-worker:1",
    )
    loaded = HarnessLaunchSpec.from_json(spec.to_json(), "spec")
    assert loaded.containerized is True
    assert loaded.pinned_owner == "amyws"
    assert loaded.container_image == "hyprial-worker:1"


def test_spec_container_fields_default_off() -> None:
    spec = HarnessLaunchSpec.from_json(
        {"provider": "pi", "name": "wk", "headless": True}, "spec"
    )
    assert spec.containerized is False
    assert spec.pinned_owner is None
    assert "containerized" not in spec.to_json()


def test_spec_pinned_owner_rejects_colon() -> None:
    with pytest.raises(DesiredStateError):
        HarnessLaunchSpec.from_json(
            {
                "provider": "pi",
                "name": "wk",
                "headless": True,
                "containerized": True,
                "pinnedOwner": "a:b",
            },
            "spec",
        )


# -- image hygiene guard ---------------------------------------------------------


def test_dockerfile_never_copies_credentials() -> None:
    dockerfile = (REPO_ROOT / "scripts" / "build" / "docker" / "Dockerfile.hyprial-worker").read_text()
    for line in dockerfile.splitlines():
        if line.strip().upper().startswith("COPY"):
            assert ".credentials.json" not in line
            assert "auth.json" not in line
            assert "/hyprial-cred" not in line
    assert (REPO_ROOT / "scripts" / "build" / "docker" / "container-entrypoint.sh").exists()


# -- harness client wrapping -------------------------------------------------------


def _channel(tmp_path: Path):
    from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

    return build_worker_channel(
        actor="agent:amyws:hq:wk",
        session_ref="ref-1",
        hyprial_home=tmp_path,
        state_dir=tmp_path / "state",
    )


def _container_spec(harness: str, tmp_path: Path) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name="wk",
        headless=True,
        cwd=str(tmp_path / "proj"),
        containerized=True,
        pinned_owner="amyws",
    )


def test_pi_client_containerized_wraps_command(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    channel = _channel(tmp_path)
    spec = _container_spec("pi", tmp_path)
    client = PiRpcClient(spec, "ref-1", worker_channel=channel)
    assert client.command[0] == "docker"
    joined = " ".join(client.command)
    assert CONTAINER_PI_EXTENSION in joined
    assert "--mode rpc" in joined
    assert "-e HYPRIAL_WORKER_ACTOR" in joined
    assert "agent:amyws:hq:wk" not in joined
    assert client._env is not None
    assert client._env["HYPRIAL_WORKER_ACTOR"] == "agent:amyws:hq:wk"


def test_codex_client_containerized_wraps_command(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    channel = _channel(tmp_path)
    spec = _container_spec("codex", tmp_path)
    client = CodexAppServerClient(spec, session_ref="ref-1", worker_channel=channel)
    assert client.command[0] == "docker"
    assert "app-server --stdio" in " ".join(client.command)


def test_claude_client_containerized_uses_image_paths(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient

    channel = _channel(tmp_path)
    spec = _container_spec("claude", tmp_path)
    client = IsolatedAgentSdkClient(spec, worker_channel=channel)
    assert client.command[0] == "docker"
    joined = " ".join(client.command)
    assert CONTAINER_SDK_WORKER in joined
    assert str(Path(__file__).resolve().parents[4] / 'tests') not in joined


def test_claude_options_env_uses_bare_docker_key_and_child_environment(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient

    channel = _channel(tmp_path)
    spec = _container_spec("claude", tmp_path)
    client = IsolatedAgentSdkClient(spec, worker_channel=channel)
    assert "HYPRIAL_AGENT_SDK_OPTIONS" in client.command
    assert not any(
        value.startswith("HYPRIAL_AGENT_SDK_OPTIONS=") for value in client.command
    )
    options = json.loads(client._env["HYPRIAL_AGENT_SDK_OPTIONS"])
    assert options["cwd"] == spec.cwd
