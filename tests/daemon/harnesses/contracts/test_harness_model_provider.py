from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection
from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.codex import CodexAppServerClient
from hyprial.daemon.impl.harnesses.dsh import DshApiClient
from hyprial.daemon.impl.harnesses.model_provider import (
    DEEPSEEK_MODEL,
    ModelProviderError,
    claude_provider_environment,
    codex_provider_configuration,
    require_deepseek_key,
    validate_model_selection,
)
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

KEY = "test-deepseek-key-never-log"


def spec(harness: str) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        cwd="/workspace",
        model_provider="deepseek",
        model=DEEPSEEK_MODEL,
    )


def test_provider_and_model_round_trip_without_a_credential() -> None:
    original = spec("codex")
    payload = original.to_json()

    assert payload["provider"] == "codex"
    assert payload["modelProvider"] == "deepseek"
    assert payload["model"] == DEEPSEEK_MODEL
    assert KEY not in json.dumps(payload)
    assert HarnessLaunchSpec.from_json(payload, "provider") == original


def test_lifecycle_projection_payload_preserves_explicit_model_provider() -> None:
    payload = HarnessLaunchProjection(
        harness="codex",
        name="worker",
        headless=True,
        model_provider="deepseek",
        model=DEEPSEEK_MODEL,
    ).to_payload()

    assert payload["provider"] == "codex"
    assert payload["modelProvider"] == "deepseek"
    assert payload["model"] == DEEPSEEK_MODEL


def test_codex_deepseek_uses_responses_env_key_without_secret_in_argv() -> None:
    selected = spec("codex")
    args, environment = codex_provider_configuration(
        selected, {"DEEPSEEK_API_KEY": KEY}
    )

    assert 'model_provider="deepseek"' in args
    assert 'cli_auth_credentials_store="ephemeral"' in args
    assert 'model_providers.deepseek.wire_api="responses"' in args
    assert 'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"' in args
    assert "model_providers.deepseek.requires_openai_auth=false" in args
    assert KEY not in args
    assert environment == {"DEEPSEEK_API_KEY": KEY}

    client = CodexAppServerClient(
        selected,
        command=("/fake/codex",),
        env={"DEEPSEEK_API_KEY": KEY},
    )
    assert client.command[-2:] == ("app-server", "--stdio")
    assert KEY not in client.command


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_containerized_deepseek_secret_never_enters_docker_argv(
    harness: str, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

    selected = HarnessLaunchSpec(
        harness=harness,
        name="worker",
        headless=True,
        cwd=str(tmp_path),
        containerized=True,
        model_provider="deepseek",
        model=DEEPSEEK_MODEL,
    )
    channel = build_worker_channel(
        actor="agent:owner:node:worker",
        session_ref="session-1",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
    )
    if harness == "codex":
        client = CodexAppServerClient(
            selected,
            env={"DEEPSEEK_API_KEY": KEY},
            worker_channel=channel,
        )
    else:
        client = IsolatedAgentSdkClient(
            selected,
            env={"DEEPSEEK_API_KEY": KEY},
            worker_channel=channel,
        )
    assert KEY not in client.command
    assert "-e" in client.command
    assert "DEEPSEEK_API_KEY" in client.command


def test_deepseek_key_falls_back_to_pi_auth_without_persisting_it(
    tmp_path: Path,
) -> None:
    auth = tmp_path / ".pi" / "agent" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": KEY}}),
        encoding="utf-8",
    )

    assert require_deepseek_key({"HOME": str(tmp_path)}) == KEY


def test_p2_codex_never_falls_back_to_pi_auth_in_the_tool_home(
    tmp_path: Path,
) -> None:
    auth = tmp_path / ".pi" / "agent" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": KEY}}),
        encoding="utf-8",
    )

    with pytest.raises(ModelProviderError, match="agent-owned DEEPSEEK_API_KEY"):
        codex_provider_configuration(
            spec("codex"),
            {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")},
            allow_legacy_home_fallback=False,
        )


def test_p2_claude_never_falls_back_to_pi_auth_in_the_tool_home(
    tmp_path: Path,
) -> None:
    auth = tmp_path / ".pi" / "agent" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": KEY}}),
        encoding="utf-8",
    )

    with pytest.raises(ModelProviderError, match="agent-owned DEEPSEEK_API_KEY"):
        claude_provider_environment(
            spec("claude"),
            {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")},
            allow_legacy_home_fallback=False,
        )


def test_deepseek_key_missing_error_never_contains_a_secret(tmp_path: Path) -> None:
    with pytest.raises(ModelProviderError, match="requires DEEPSEEK_API_KEY") as raised:
        require_deepseek_key({"HOME": str(tmp_path)})
    assert KEY not in str(raised.value)


def test_legacy_auth_io_failure_keeps_existing_retry_semantics(tmp_path, monkeypatch):
    def unavailable(_path, **_kwargs):
        raise OSError("temporary fixture read failure")

    monkeypatch.setattr(Path, "read_text", unavailable)
    with pytest.raises(ModelProviderError) as raised:
        require_deepseek_key({"HOME": str(tmp_path)})
    assert not getattr(raised.value, "permanent_start_failure", False)


def test_claude_deepseek_maps_anthropic_environment_and_model() -> None:
    client = IsolatedAgentSdkClient(
        spec("claude"),
        env={"DEEPSEEK_API_KEY": KEY},
        worker_command=("/fake/agent-sdk-worker",),
    )

    assert client.options["model"] == DEEPSEEK_MODEL
    assert client._env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert client._env["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert client._env["ANTHROPIC_MODEL"] == DEEPSEEK_MODEL
    assert client._env["CLAUDE_CODE_SUBAGENT_MODEL"] == DEEPSEEK_MODEL
    assert KEY not in client.command


def test_pi_receives_native_provider_and_model_arguments() -> None:
    client = PiRpcClient(
        spec("pi"),
        "session-ref",
        command=("/fake/pi",),
    )

    assert client.command[:5] == (
        "/fake/pi",
        "--provider",
        "deepseek",
        "--model",
        DEEPSEEK_MODEL,
    )


class DshApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, method: str, payload: dict[str, object]) -> object:
        self.calls.append((method, payload))
        if method == "host.describe":
            return {"provider": "kimi-coding", "model": "k3"}
        if method == "session.create":
            return {"sessionId": "s1"}
        if method == "session.models":
            return {
                "current": {"provider": "kimi-coding", "model": "k3"},
                "routable": True,
                "groups": [],
                "failures": [],
            }
        if method == "session.selectModel":
            return {
                "selected": {
                    "provider": payload["provider"],
                    "model": payload["model"],
                }
            }
        if method == "session.cancel":
            return {"accepted": True}
        raise AssertionError(f"unexpected method {method}")


def test_dsh_selects_model_on_the_new_session_not_the_host_default() -> None:
    async def scenario() -> None:
        api = DshApi()
        client = DshApiClient(spec("dsh"), api=api)
        async with client:
            assert client.session_id == "s1"
        assert (
            "session.selectModel",
            {
                "sessionId": "s1",
                "provider": "deepseek-official",
                "model": DEEPSEEK_MODEL,
            },
        ) in api.calls

    asyncio.run(scenario())


def test_codex_rejects_deepseek_models_not_served_by_responses() -> None:
    # Deliberately a name this repository never ships.  The probe here used to
    # be "deepseek-v4-pro"; that name is now RETIRED, so it would be refused by
    # the earlier gate -- the test would keep passing while no longer
    # exercising the codex whitelist it is named after.
    with pytest.raises(ModelProviderError, match="currently supports only"):
        validate_model_selection("codex", "deepseek", "deepseek-some-other-model")


def test_the_second_retired_name_is_refused_too() -> None:
    """Allen 2026-09-16: deepseek-flash is the only deepseek model name.

    So the former "pro" tier is not a separate model any more -- it is a dead
    name, and it gets the same refusal as the other one.
    """

    with pytest.raises(ModelProviderError, match="is retired"):
        validate_model_selection("claude", "deepseek", "deepseek-v4-pro")


def test_the_retired_model_name_is_refused_and_says_what_to_use() -> None:
    """The old name must fail loudly, not resolve through an alias.

    An alias keeps every out-of-tree caller working until the vendor drops
    the name, and on that day they fail together with no list of who they
    are.  This refusal is the list: each caller names itself the first time
    it runs.  So the message has to carry the replacement, or the person
    reading it cannot act on it.
    """

    with pytest.raises(ModelProviderError) as refusal:
        validate_model_selection("pi", "deepseek", "deepseek-v4-flash")
    message = str(refusal.value)
    assert "deepseek-v4-flash" in message
    assert DEEPSEEK_MODEL in message
    assert "deliberate refusal" in message


def test_the_refusal_fires_with_no_provider_named() -> None:
    """Load-bearing: the check sits BEFORE the ``provider is None`` return.

    Every other rule in this function is provider-scoped and returns early
    when no provider was given.  A retired name is not provider-scoped -- it
    is dead on every route -- so putting the check after that return would
    exempt the most common call shape (``--model`` with no ``--provider``)
    while still passing every provider-scoped test.
    """

    with pytest.raises(ModelProviderError, match="is retired"):
        validate_model_selection("pi", None, "deepseek-v4-flash")


def test_the_refusal_repeats_who_asked_when_the_caller_knows() -> None:
    """Without this, the refusal proves someone still sends the old name and
    says nothing about where to change it -- which is the problem itself."""

    with pytest.raises(ModelProviderError) as refusal:
        validate_model_selection(
            "pi", "deepseek", "deepseek-v4-flash", context="hyprial agent start --name squire"
        )
    assert "hyprial agent start --name squire" in str(refusal.value)

    with pytest.raises(ModelProviderError) as anonymous:
        validate_model_selection("pi", "deepseek", "deepseek-v4-flash")
    assert "did not identify itself" in str(anonymous.value)


def test_the_replacement_name_is_accepted_on_every_route() -> None:
    validate_model_selection("pi", "deepseek", DEEPSEEK_MODEL)
    validate_model_selection("codex", "deepseek", DEEPSEEK_MODEL)
    validate_model_selection("claude", "deepseek", DEEPSEEK_MODEL)
    validate_model_selection("pi", None, DEEPSEEK_MODEL)


def test_start_persists_explicit_selection_separately_from_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(
        method: str, params: dict[str, Any] | None = None, **_kwargs: object
    ) -> dict[str, Any]:
        calls.append((method, params or {}))
        return {"ok": True, "id": "codex:worker"}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/codex")
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "codex",
            "--name",
            "worker",
            "--cwd",
            str(tmp_path),
            "--headless",
            "--provider",
            "deepseek",
            "--model",
            DEEPSEEK_MODEL,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][1]["harness"] == "codex"
    assert calls[0][1]["provider"] == "deepseek"
    assert calls[0][1]["model"] == DEEPSEEK_MODEL
    assert calls[1][1]["provider"] == "codex"
    assert calls[1][1]["modelProvider"] == "deepseek"
    assert calls[1][1]["model"] == DEEPSEEK_MODEL
    assert calls[1][1]["args"] == []


def test_start_rejects_duplicate_public_and_passthrough_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *_args, **_kwargs: calls.append(method) or {"ok": True},
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "pi",
            "--name",
            "worker",
            "--headless",
            "--provider",
            "deepseek",
            "--",
            "--provider",
            "deepseek",
        ],
    )

    assert result.exit_code != 0
    assert "supplied both" in result.output
    assert calls == []


def test_pi_model_definitions_define_exactly_the_canonical_deepseek_model() -> None:
    """One source: pi's definition is derived from model_provider's names."""

    from hyprial.daemon.impl.harnesses.model_provider import (
        DEEPSEEK_MODEL,
        DEEPSEEK_PROVIDER,
        RETIRED_MODEL_NAMES,
        pi_model_definitions,
    )

    providers = pi_model_definitions({})
    assert set(providers) == {DEEPSEEK_PROVIDER}
    ids = [model["id"] for model in providers[DEEPSEEK_PROVIDER]["models"]]  # type: ignore[index]
    assert ids == [DEEPSEEK_MODEL]
    assert not set(ids) & set(RETIRED_MODEL_NAMES)
    # The endpoint seam applies to pi like every other harness.
    local = pi_model_definitions({"HYPRIAL_OPENAI_BASE_URL": "http://127.0.0.1:9/v1/"})
    assert local[DEEPSEEK_PROVIDER]["baseUrl"] == "http://127.0.0.1:9/v1"


def test_every_p2_pi_launch_carries_the_model_provider_definitions(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.harnesses.model_provider import pi_model_definitions
    from hyprial.daemon.impl.harnesses.pi.loader import (
        PiProjectTrustReceipt,
        pi_sdk_launch_from_public_projection,
    )

    roots = {name: tmp_path / name for name in ("projection", "native", "sessions", "home", "pkg")}
    for root in roots.values():
        root.mkdir()
    (tmp_path / ".git").mkdir()
    environment = {"HOME": str(roots["home"]), "PI_CODING_AGENT_DIR": str(roots["native"])}
    launch = pi_sdk_launch_from_public_projection(
        {
            "mode": "agent-home-p2",
            "harness": "pi",
            "projectionRoot": str(roots["projection"]),
            "nativeRoot": str(roots["native"]),
            "sessionRoot": str(roots["sessions"]),
            "environment": environment,
        },
        trust=PiProjectTrustReceipt(tmp_path, tmp_path, True),
        mode="tui",
        session_id="p2-carries-models",
        pi_package_root=roots["pkg"],
    )

    payload = json.loads(launch.argv[-1])
    assert payload["modelDefinitions"] == pi_model_definitions(environment)
