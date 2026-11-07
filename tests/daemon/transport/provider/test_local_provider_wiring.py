"""The local-provider wiring must actually reach the local provider.

Asserting that the helper returns the right strings would pass while nothing
was listening; the property that matters is that a launch configured this way
sends its request to the fake and not to the vendor.  So the fake is started
and the request is observed arriving.

The failure this guards is specific: E2E runs whose variance came from the
network, the provider's quota, and round-trip latency rather than from the
code under test -- and which therefore could not distinguish "our code broke"
from "the provider was slow today".
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.model_provider import (
    DEEPSEEK_MODEL,
    claude_provider_environment,
    codex_provider_configuration,
)

ROOT = Path(__file__).resolve().parents[4]
SCENARIOS = ROOT / "contract" / "e2e"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_probe_{name}", SCENARIOS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _spec(harness: str) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        cwd="/workspace",
        model_provider="deepseek",
        model=DEEPSEEK_MODEL,
    )


def test_configured_launch_reaches_the_local_provider_not_the_vendor() -> None:
    """End to end through the real seam: helper -> hyprial config -> live server.

    To make this red, drop the override lookup in model_provider: the
    environment goes back to the vendor URL, nothing arrives locally, and the
    recorded request list stays empty.
    """

    fake_provider = _load("_fake_provider")
    deepseek = _load("_deepseek")

    with fake_provider.FakeProvider(reply="hello from the fake") as provider:
        environment = deepseek.local_provider_environment(
            openai_base_url=provider.openai_base_url,
            anthropic_base_url=provider.anthropic_base_url,
        )

        # The harness-facing configuration hyprial would actually launch with.
        claude_env = claude_provider_environment(_spec("claude"), environment)
        assert claude_env["ANTHROPIC_BASE_URL"] == provider.anthropic_base_url

        argv, _ = codex_provider_configuration(_spec("codex"), environment)
        assert (
            f'model_providers.deepseek.base_url="{provider.openai_base_url}"'
            in argv
        )

        # Not a string comparison: drive the endpoint that configuration names
        # and confirm the request lands here.
        request = urllib.request.Request(
            f"{claude_env['ANTHROPIC_BASE_URL']}/v1/messages",
            data=json.dumps({"messages": []}).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=5
        ) as response:
            body = response.read().decode()

        assert provider.requests, "the configured endpoint received nothing"
        assert "message_stop" in body


def test_the_fake_can_withhold_the_terminating_event() -> None:
    """The fault a real provider cannot be asked for on demand.

    This is the repository's recurring failure -- waiting on a signal that
    never arrives -- and it is observable: pi reports it rather than hanging,
    so a scenario injecting it can go red rather than merely time out.
    """

    fake_provider = _load("_fake_provider")
    plan = fake_provider.FaultPlan.fail_always(fake_provider.NO_TERMINATOR)

    with fake_provider.FakeProvider(reply="partial", plan=plan) as provider:
        request = urllib.request.Request(
            f"{provider.openai_base_url}/chat/completions", data=b"{}"
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=5
        ) as response:
            body = response.read().decode()

    assert "chat.completion.chunk" in body, "the stream produced no content"
    assert "[DONE]" not in body, "the terminator was supposed to be withheld"


def test_the_fake_serves_responses_streams_and_records_request_bodies() -> None:
    fake_provider = _load("_fake_provider")

    with fake_provider.FakeProvider(reply="What can I help with?") as provider:
        request = urllib.request.Request(
            f"{provider.openai_base_url}/responses",
            data=json.dumps({"input": "hello", "stream": True}).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=5
        ) as response:
            body = response.read().decode()

    assert provider.request_bodies == [{"input": "hello", "stream": True}]
    assert "response.output_text.delta" in body
    assert "response.completed" in body
    assert "What can I help with?" in body


def test_the_fake_records_the_contract_it_was_written_against() -> None:
    """A mock drifts from the service it imitates without announcing it.

    Same shape as evidence going stale against the tree it was gathered on:
    neither says so. Calibration compares these before comparing payloads, so
    "out of date" surfaces as a finding rather than as a confusing failure.
    """

    fake_provider = _load("_fake_provider")
    versions = fake_provider.CONTRACT_VERSIONS

    assert versions.get("openai.chat.completions")
    assert versions.get("openai.responses")
    assert versions.get("anthropic.messages")
