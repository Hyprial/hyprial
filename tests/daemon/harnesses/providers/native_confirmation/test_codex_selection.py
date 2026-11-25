"""Actual Codex app-server native selection is separate from a successful model turn."""

import asyncio
import json
import os
from pathlib import Path
import shutil

import pytest

from hyprial.daemon.impl.harnesses.codex import CodexAppServerClient
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.identity import compose_worker_child_launch, resolve_agent_runtime_context
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.harnesses.providers.test_daemon_provider_login import (
    ACTOR, provider_app as provider_app,
)
from tests.daemon.harnesses.providers.test_native_auth_imports import _login, _record
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE
from tests.support.runtime._owned_applications import owned_applications as owned_applications


@pytest.mark.skipif(shutil.which("codex") is None, reason="real Codex is required for native selection")
def test_real_codex_file_selection_does_not_confirm_before_a_response(provider_app, tmp_path):
    app = provider_app
    registry = app._agent_registry
    content = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "synthetic-not-a-vendor-key"}).encode()
    _login(app, "codex", ["auth.json"], content)
    home = Path(registry.home_receipt(ACTOR).path)
    workspace = home / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    context = resolve_agent_runtime_context(registry=registry, agent_name=ACTOR,
        harness="codex", cwd=str(workspace), tool_profile=TEST_TOOL_PROFILE)
    channel = build_worker_channel(actor=registry.require(ACTOR).uri, session_ref="native-evidence-test",
        hyprial_home=app.hyprial_home, state_dir=app.state_dir, runtime_context=context)
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "isolated-user")}
    Path(environment["HOME"]).mkdir(mode=0o700)
    launch = compose_worker_child_launch(registry=registry, hyprial_home=app.hyprial_home,
        channel=channel, environ=environment, agent_name=ACTOR, harness="codex")
    proof = launch.native_auth_launch
    assert proof is not None
    proof.confirm(1000)
    assert _record(app, "codex")["startupConfirmedAt"] is None

    async def exercise():
        spec = HarnessLaunchSpec("codex", ACTOR, True, cwd=str(workspace))
        client = CodexAppServerClient(spec, command=(shutil.which("codex"),), complete_launch=launch)
        async with client:
            assert client.native_load_evidence.auth_store == "file"
            assert client.native_load_evidence.account_type == "apiKey"
            assert _record(app, "codex")["startupConfirmedAt"] is None
            proof.confirm(2000)
            assert _record(app, "codex")["startupConfirmedAt"] == 2000

    asyncio.run(exercise())
