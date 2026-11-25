"""Authentication source selection must distinguish native files from Key grants."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.model_provider import pi_provider_environment
from hyprial.daemon.impl.harnesses.agent_sdk import _validated_claude_launch_environment
from tests.daemon.harnesses.agent_sdk.driver.identity.test_legacy_credential import launch as launch


@pytest.mark.parametrize("environment", [{}, {"DEEPSEEK_API_KEY": "synthetic-grant"}])
def test_pi_marks_only_native_deepseek_and_clears_previous_selection(tmp_path, environment):
    root = tmp_path / "native"
    root.mkdir(mode=0o700)
    (root / "auth.json").write_text(json.dumps({"deepseek": {"type": "api_key", "key": "synthetic-native"}}))
    selected = []
    spec = HarnessLaunchSpec(name="worker", harness="pi", headless=True, model_provider="deepseek", model="deepseek-flash")
    result = pi_provider_environment(spec, environment, native_root=root,
        allow_legacy_home_fallback=False, on_native_selected=selected.append)
    assert selected == ([None] if environment else [None, root])
    assert result == environment


def test_unsupported_pi_source_keeps_confirmation_unknown(tmp_path):
    selected = []
    spec = HarnessLaunchSpec(name="worker", harness="pi", headless=True, model_provider="openai", model="gpt-5")
    pi_provider_environment(spec, {}, native_root=tmp_path, on_native_selected=selected.append)
    assert selected == [None]


@pytest.mark.parametrize("grant", [False, True])
def test_claude_selects_native_only_without_environment_grant(launch, tmp_path, grant):
    registry, channel, spec, compose = launch
    from hyprial.identity import install_native_credential_set

    home = Path(registry.home_receipt("worker").path)
    install_native_credential_set(home, "claude", [(".credentials.json", b'{"synthetic":true}')],
        import_record=registry.native_credential_import_record("worker"))
    complete = compose(tmp_path)
    selected = []
    complete = replace(complete, native_auth_launch=SimpleNamespace(select=selected.append))
    if grant:
        environment = complete.environment.for_exec()
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = "synthetic-grant"
        complete = replace(complete, environment=SimpleNamespace(for_exec=lambda: environment))
    _validated_claude_launch_environment(spec, env=None, worker_channel=channel, complete_launch=complete)
    assert selected == ([None] if grant else [home / "secrets/native/claude"])
