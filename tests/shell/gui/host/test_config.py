"""Host configuration: the launcher environment is the only source of authority."""

from __future__ import annotations

import pytest

from hyprial.shell.impl.gui.host.config import ConfigError, HostConfig, read_config

GRANT = "launch-grant-".ljust(64, "g")


def env(**overrides: str) -> dict[str, str]:
    base = {
        "HYPRIAL_GUI_BACKEND_DRIVER": "daemon",
        "HYPRIAL_BACKEND_ENDPOINT": "http://127.0.0.1:8791",
        "HYPRIAL_GUI_UPSTREAM_TOKEN": "synthetic-upstream-token",
        "HYPRIAL_GUI_LAUNCH_GRANT": GRANT,
    }
    base.update(overrides)
    return base


def test_an_absent_principal_is_the_anonymous_bootstrap() -> None:
    config = read_config(env())
    assert isinstance(config, HostConfig)
    assert config.principal is None


def test_a_serving_principal_carries_actor_owner_and_node() -> None:
    config = read_config(env(HYPRIAL_GUI_PRINCIPAL="user:owner", HYPRIAL_NODE_ID="node"))
    assert config.principal is not None
    assert (config.principal.actor, config.principal.owner, config.principal.node) == (
        "user:owner",
        "owner",
        "node",
    )


@pytest.mark.parametrize("value", ["", "   ", "owner-only", "agent:owner", "user:", "user:a:b"])
def test_an_empty_or_malformed_principal_never_degrades_to_bootstrap(value: str) -> None:
    with pytest.raises(ConfigError, match="canonical"):
        read_config(env(HYPRIAL_GUI_PRINCIPAL=value, HYPRIAL_NODE_ID="node"))


@pytest.mark.parametrize("node", [None, "", "  ", "a:b"])
def test_a_serving_principal_requires_the_verified_node(node: str | None) -> None:
    values = env(HYPRIAL_GUI_PRINCIPAL="user:owner")
    if node is not None:
        values["HYPRIAL_NODE_ID"] = node
    with pytest.raises(ConfigError, match="HYPRIAL_NODE_ID"):
        read_config(values)


@pytest.mark.parametrize("driver", [None, "", "fixture", "Daemon"])
def test_the_driver_is_exactly_daemon_or_contract_mock(driver: str | None) -> None:
    values = env()
    del values["HYPRIAL_GUI_BACKEND_DRIVER"]
    if driver is not None:
        values["HYPRIAL_GUI_BACKEND_DRIVER"] = driver
    with pytest.raises(ConfigError, match="HYPRIAL_GUI_BACKEND_DRIVER"):
        read_config(values)
    mock = env(HYPRIAL_GUI_BACKEND_DRIVER="contract-mock", HYPRIAL_GUI_PRINCIPAL="user:owner", HYPRIAL_NODE_ID="node")
    assert read_config(mock).driver == "contract-mock"


def test_the_upstream_token_and_launch_grant_are_required_and_the_grant_is_long() -> None:
    values = env()
    del values["HYPRIAL_GUI_UPSTREAM_TOKEN"]
    with pytest.raises(ConfigError, match="HYPRIAL_GUI_UPSTREAM_TOKEN"):
        read_config(values)
    with pytest.raises(ConfigError, match="HYPRIAL_GUI_LAUNCH_GRANT"):
        read_config(env(HYPRIAL_GUI_LAUNCH_GRANT="short"))
    assert read_config(env(HYPRIAL_GUI_LAUNCH_GRANT=f"  {GRANT}  ")).launch_grant == GRANT


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "not a url",
        "https://127.0.0.1:8791",
        "http://example.com:8791",
        "http://0.0.0.0:8791",
        "http://127.0.0.1",
        "http://127.0.0.1:8791/path",
        "http://127.0.0.1:8791/?q=1",
        "http://127.0.0.1:8791/#x",
        "http://user:pass@127.0.0.1:8791",
        "http://127.0.0.1:99999",
    ],
)
def test_the_endpoint_must_be_a_loopback_http_origin_with_a_port(endpoint: str) -> None:
    with pytest.raises(ConfigError, match="HYPRIAL_BACKEND_ENDPOINT"):
        read_config(env(HYPRIAL_BACKEND_ENDPOINT=endpoint))


@pytest.mark.parametrize(
    ("endpoint", "origin"),
    [
        ("http://127.0.0.1:8791", "http://127.0.0.1:8791"),
        ("http://127.0.0.1:8791/", "http://127.0.0.1:8791"),
        ("http://localhost:8791", "http://localhost:8791"),
        ("http://[::1]:8791", "http://[::1]:8791"),
    ],
)
def test_a_loopback_endpoint_is_normalised_to_its_origin(endpoint: str, origin: str) -> None:
    assert read_config(env(HYPRIAL_BACKEND_ENDPOINT=endpoint)).endpoint == origin


def test_launch_file_paths_are_absolute_and_named_by_the_launcher_contract() -> None:
    parsed = read_config(
        env(
            HYPRIAL_GUI_HANDOFF_PATH="/private/runtime/launch.url",
            HYPRIAL_GUI_PUBLICATION_PATH="/private/runtime/launch.ready",
        )
    )
    assert parsed.handoff_path == "/private/runtime/launch.url"
    assert parsed.publication_path == "/private/runtime/launch.ready"
    for name, value in (
        ("HYPRIAL_GUI_HANDOFF_PATH", "relative/launch.url"),
        ("HYPRIAL_GUI_HANDOFF_PATH", "/private/runtime/other.url"),
        ("HYPRIAL_GUI_PUBLICATION_PATH", "relative/launch.ready"),
    ):
        with pytest.raises(ConfigError, match=name):
            read_config(env(**{name: value}))
    blank = read_config(env(HYPRIAL_GUI_HANDOFF_PATH="  "))
    assert blank.handoff_path is None and blank.publication_path is None


def test_durations_and_capacity_have_defaults_and_ranges() -> None:
    config = read_config(env())
    assert (config.launch_ttl_ms, config.session_ttl_ms, config.max_sessions) == (
        60_000,
        43_200_000,
        32,
    )
    bounded = read_config(
        env(
            HYPRIAL_GUI_LAUNCH_TTL_MS="1000",
            HYPRIAL_GUI_SESSION_TTL_MS="86400000",
            HYPRIAL_GUI_MAX_SESSIONS="1024",
        )
    )
    assert (bounded.launch_ttl_ms, bounded.session_ttl_ms, bounded.max_sessions) == (
        1_000,
        86_400_000,
        1_024,
    )
    for name, value in (
        ("HYPRIAL_GUI_LAUNCH_TTL_MS", "999"),
        ("HYPRIAL_GUI_LAUNCH_TTL_MS", "86400001"),
        ("HYPRIAL_GUI_LAUNCH_TTL_MS", "1.5"),
        ("HYPRIAL_GUI_SESSION_TTL_MS", "soon"),
        ("HYPRIAL_GUI_MAX_SESSIONS", "0"),
        ("HYPRIAL_GUI_MAX_SESSIONS", "1025"),
    ):
        with pytest.raises(ConfigError, match=name):
            read_config(env(**{name: value}))


def test_the_public_face_is_import_light_and_exposes_the_documented_names() -> None:
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "import hyprial.shell.impl.gui.host as host\n"
        "assert sorted(host.__all__) == ['ConfigError', 'HostConfig', 'build_app', 'read_config', 'serve']\n"
        "heavy = [m for m in ('uvicorn', 'httpx', 'hyprial.shell.impl.gui.host.app') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
        "assert callable(host.build_app) and callable(host.serve)\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
