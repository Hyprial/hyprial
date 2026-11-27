from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.supervisor import environment as environment_module
from hyprial.daemon.impl.terminals.supervisor.environment import (
    HcSocketPathTooLongError,
    build_private_hc_environment,
    write_private_hc_config,
)
from hyprial.kernel import hc_core_socket_path, hc_session_name


def test_private_environment_is_secret_free_and_home_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = {
        "HOME": "/host/home",
        "PATH": "/usr/bin",
        "USER": "agent",
        "LANG": "C.UTF-8",
        "LC_CTYPE": "zh_CN.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "ANTHROPIC_API_KEY": "sk-secret",
        "OPENAI_API_KEY": "sk-2",
        "HYPRIAL_TOKEN": "tok",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "AWS_SECRET_ACCESS_KEY": "aws",
    }
    monkeypatch.setattr(environment_module, "local_ipc_path_limit", lambda: None)
    private = build_private_hc_environment(home, source)

    assert private["HOME"] == "/host/home"
    assert private["PATH"] == "/usr/bin"
    assert private["LANG"] == "C.UTF-8"
    assert private["LC_CTYPE"] == "zh_CN.UTF-8"
    assert private["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert private["HC_SESSION"] == hc_session_name(home)
    assert private["HC_SOCKET"] == str(hc_core_socket_path(home))
    core_root = home / "state" / "hc" / "core"
    assert private["XDG_STATE_HOME"] == str(core_root / "state")
    assert private["XDG_CONFIG_HOME"] == str(core_root / "config")
    assert private["XDG_DATA_HOME"] == str(core_root / "data")
    assert not any(
        key.startswith(("ANTHROPIC_", "OPENAI_", "HYPRIAL_", "CLAUDE_", "AWS_"))
        for key in private
    )


def test_private_environment_accepts_explicit_non_secret_passthrough(tmp_path: Path) -> None:
    private = build_private_hc_environment(
        tmp_path,
        {"HOME": "/host", "PATH": "/bin"},
        passthrough={"MAGPIE_URL": "http://127.0.0.1:3425", "NO_PROXY": "127.0.0.1"},
    )
    assert private["MAGPIE_URL"] == "http://127.0.0.1:3425"
    assert private["NO_PROXY"] == "127.0.0.1"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAGPIE_URL", "http://user:token@127.0.0.1:3425"),
        ("MAGPIE_URL", "http://127.0.0.1:3425/?token=secret"),
        ("NO_PROXY", "127.0.0.1@attacker.example"),
    ],
)
def test_passthrough_rejects_credentials_and_secret_bearing_values(
    tmp_path: Path, key: str, value: str
) -> None:
    with pytest.raises(ValueError):
        build_private_hc_environment(
            tmp_path, {"HOME": "/host", "PATH": "/bin"}, passthrough={key: value}
        )


def test_holder_socket_length_is_checked_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "hyprial.daemon.impl.terminals.supervisor.environment.local_ipc_path_limit",
        lambda: len(str(hc_core_socket_path(tmp_path))) + 9,
    )
    with pytest.raises(HcSocketPathTooLongError):
        build_private_hc_environment(tmp_path, {})


def test_private_config_is_written_under_secure_core_config(tmp_path: Path) -> None:
    path = write_private_hc_config(tmp_path)
    assert path == tmp_path / "state" / "hc" / "core" / "config" / "herdr-core" / "config.toml"
    assert "enabled = false" in path.read_text(encoding="utf-8")
