"""Legacy managed Claude must not use the daemon's native login."""
from pathlib import Path

import pytest

from hyprial.identity import AgentRegistry, compose_worker_child_launch
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.daemon.impl.harnesses.agent_sdk import _validated_claude_launch_environment
from hyprial.daemon.impl.harnesses.claude.runtime import ClaudeRuntimeError


@pytest.fixture
def launch(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(tmp_path / "registry.sqlite3", owner="test", machine="host", hyprial_home=home)
    agent = registry.create("worker", preferred_harness="claude")
    channel = build_worker_channel(actor=agent.uri, session_ref="session", hyprial_home=home,
                                  state_dir=tmp_path / "state", owner="test", node_id="host")
    spec = HarnessLaunchSpec(harness="claude", name="worker", headless=True)
    def compose(root, *, harness=None):
        return compose_worker_child_launch(registry=registry, hyprial_home=home, channel=channel,
            environ={"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(root),
                     "CODEX_HOME": str(root), "PI_CODING_AGENT_DIR": str(root)},
            agent_name="worker", harness=harness)
    try:
        yield registry, channel, spec, compose
    finally:
        registry.close()


def native(root):
    root.mkdir(mode=0o700, parents=True)
    path = root / ".credentials.json"
    path.write_text('{"synthetic":true}')
    path.chmod(0o600)


def test_ambient_native_login_fails_before_start(launch, tmp_path):
    _, channel, spec, compose = launch
    root = tmp_path / "daemon-native"
    native(root)
    with pytest.raises(ClaudeRuntimeError) as caught:
        _validated_claude_launch_environment(spec, env=None, worker_channel=channel, complete_launch=compose(root))
    assert caught.value.code == "CLAUDE_CREDENTIAL_MISSING"
    assert caught.value.permanent_start_failure is True


@pytest.mark.parametrize("with_file", [True, False])
def test_resident_native_root_is_preserved(launch, with_file):
    registry, channel, spec, compose = launch
    root = Path(registry.home_receipt("worker").path) / "secrets/native/claude"
    native(root)
    root.parent.chmod(0o700)
    root.parent.parent.chmod(0o700)
    if not with_file:
        (root / ".credentials.json").unlink()
    from hyprial.daemon.impl.harnesses.claude.runtime import validate_legacy_claude_auth
    assert validate_legacy_claude_auth({
        "HYPRIAL_AGENT_DIR": str(root.parents[2]), "CLAUDE_CONFIG_DIR": str(root),
    }) == "agent-native"
    _, environment, _ = _validated_claude_launch_environment(
        spec, env=None, worker_channel=channel, complete_launch=compose(root))
    assert environment["CLAUDE_CONFIG_DIR"] == str(root)


def test_p1_composes_own_private_root_and_missing_stays_missing(launch, tmp_path):
    registry, channel, spec, compose = launch
    home = Path(registry.home_receipt("worker").path)
    root = home / "secrets/native/claude"
    bait = tmp_path / "host-native"
    native(bait)
    for _ in range(2):
        complete = compose(bait)
        assert complete.environment.for_exec()["CLAUDE_CONFIG_DIR"] == str(root)
        assert root.is_dir() and root.stat().st_mode & 0o777 == 0o700
        with pytest.raises(ClaudeRuntimeError) as caught:
            _validated_claude_launch_environment(
                spec, env=None, worker_channel=channel, complete_launch=complete)
        assert caught.value.code == "CLAUDE_CREDENTIAL_MISSING"


@pytest.mark.parametrize("existing_native", [False, True])
def test_env_grant_uses_own_native_and_session_roots(launch, tmp_path, existing_native):
    from hyprial.identity import AgentSecretStore
    import secrets

    registry, channel, spec, compose = launch
    home = Path(registry.home_receipt("worker").path)
    root = home / "secrets/native/claude"
    if existing_native:
        native(root)
        root.parent.chmod(0o700)
        root.parent.parent.chmod(0o700)
    registry.create("custodian")
    store = AgentSecretStore(Path(registry.home_receipt("custodian").path))
    store.set(service="claude", field="CLAUDE_CODE_OAUTH_TOKEN", value=secrets.token_hex(24),
              actor="user:test", via="cli", at_ms=1)
    registry.grant_secret("worker", grant_id="oauth", source="agent-shared",
                          entry_id="claude", field_name="CLAUDE_CODE_OAUTH_TOKEN",
                          environment_names=("CLAUDE_CODE_OAUTH_TOKEN",), revision=1,
                          owner_actor="custodian")
    _, environment, _ = _validated_claude_launch_environment(
        spec, env=None, worker_channel=channel, complete_launch=compose(tmp_path / "host"))
    root = home / "secrets/native/claude"
    assert environment["CLAUDE_CONFIG_DIR"] == str(root)
    assert (root / "projects").is_symlink()
    assert (root / "projects").resolve() == (home / "state/sessions/claude").resolve()
    assert (root / "projects").stat().st_mode & 0o777 == 0o700
    registry.revoke_secret_grant("worker", "oauth")
    if existing_native:
        # Explicit P1 auth retains precedence; revocation restores own native.
        _validated_claude_launch_environment(
            spec, env=None, worker_channel=channel, complete_launch=compose(root))
        return
    # A successful env launch does not turn a new root into a native login.
    with pytest.raises(ClaudeRuntimeError) as caught:
        _validated_claude_launch_environment(
            spec, env=None, worker_channel=channel, complete_launch=compose(root))
    assert caught.value.code == "CLAUDE_CREDENTIAL_MISSING"


def test_explicit_native_install_admits_a_launch_provisioned_root(launch, tmp_path):
    from hyprial.identity import NativeCredentialError, install_native_credential_set

    registry, channel, spec, compose = launch
    home = Path(registry.home_receipt("worker").path)
    root = home / "secrets/native/claude"
    complete = compose(tmp_path / "host")
    with pytest.raises(ClaudeRuntimeError):
        _validated_claude_launch_environment(
            spec, env=None, worker_channel=channel, complete_launch=complete)
    with pytest.raises(NativeCredentialError):
        install_native_credential_set(home, "claude", [(".credentials.json", b"")])
    with pytest.raises(ClaudeRuntimeError) as caught:
        _validated_claude_launch_environment(
            spec, env=None, worker_channel=channel, complete_launch=compose(root))
    assert caught.value.code == "CLAUDE_CREDENTIAL_MISSING"
    install_native_credential_set(home, "claude", [(".credentials.json", b'{"synthetic":true}')])
    # Recompose after installation, as the next launch does.
    _, environment, _ = _validated_claude_launch_environment(
        spec, env=None, worker_channel=channel, complete_launch=compose(root))
    assert environment["CLAUDE_CONFIG_DIR"] == str(root)
    assert (root / "projects").resolve() == (home / "state/sessions/claude").resolve()


@pytest.mark.parametrize("harness, selector", [("codex", "CODEX_HOME"), ("pi", "PI_CODING_AGENT_DIR")])
def test_other_harness_roots_remain_unchanged(launch, tmp_path, harness, selector):
    registry, _, _, compose = launch
    bait = tmp_path / "host-native"
    complete = compose(bait, harness=harness)
    assert complete.environment.for_exec()[selector] == str(bait)
    assert not (Path(registry.home_receipt("worker").path) / "secrets/native/claude").exists()


def test_native_session_link_rejects_foreign_target(launch, tmp_path):
    registry, channel, spec, compose = launch
    root = Path(registry.home_receipt("worker").path) / "secrets/native/claude"
    native(root)
    root.parent.chmod(0o700)
    root.parent.parent.chmod(0o700)
    (root / "projects").symlink_to(tmp_path)
    with pytest.raises(ClaudeRuntimeError, match="session"):
        _validated_claude_launch_environment(
            spec, env=None, worker_channel=channel, complete_launch=compose(root))
