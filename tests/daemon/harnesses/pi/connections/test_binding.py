"""A connection selects an opaque model without a vendor default or secret argv."""

from dataclasses import replace
import json

import pytest

from hyprial.identity import ModelConnection
from hyprial.daemon.impl.harnesses.pi.bindings import PiModelBinding


def binding(**overrides):
    return PiModelBinding(
        binding_ref="fixture:agent-a:member-a", binding_revision=1,
        connection=ModelConnection("https://example.invalid/v1", "openai-completions",
                                   "openai-chat-completions/v1", "a" * 32, 1),
        model="hy4-flash", key="synthetic-pi-test-key", **overrides,
    )


@pytest.mark.parametrize("model", ["hy4-flash", "qwen3.7-flash", "eepseek-v4.1-flash-1017", "gpt-5-custom", "tenant/model-v1"])
def test_opaque_model_and_exact_template(model):
    selected = replace(binding(), model=model)
    definition = selected.definitions()[selected.provider_id]
    item = definition["models"][0]
    assert definition["baseUrl"] == selected.connection.endpoint
    assert definition["api"] == "openai-completions"
    assert definition["apiKey"] == "HYPRIAL_PI_CONNECTION_KEY"
    assert item["id"] == item["name"] == model
    assert item["reasoning"] is False and item["input"] == ["text"]
    assert item["contextWindow"] == 32768 and item["maxTokens"] == 4096
    assert item["compat"] == {
        "supportsStore": False, "supportsDeveloperRole": False,
        "maxTokensField": "max_tokens", "supportsStrictMode": False,
        "requiresReasoningContentOnAssistantMessages": False,
    }
    assert set(item["cost"].values()) == {0}
    assert "thinkingLevelMap" not in item
    assert "deepseek" not in selected.provider_id


def test_snapshot_is_non_secret_and_rotation_keeps_provider_identity():
    selected = binding()
    snapshot = selected.snapshot()
    assert snapshot["schemaVersion"] == 1
    assert snapshot["connectionRef"] == "a" * 32
    assert snapshot["costStatus"] == "unknown"
    assert "synthetic-pi-test-key" not in repr(selected) + json.dumps(snapshot) + json.dumps(selected.definitions())
    assert selected.provider_id == replace(selected, key="rotated-fixture-key").provider_id
    assert selected.provider_id != replace(selected, connection=replace(selected.connection, config_revision=2)).provider_id
    assert selected.provider_id != replace(selected, connection=replace(selected.connection, connection_ref="b" * 32)).provider_id


@pytest.mark.parametrize("model", ["", " hy4-flash", "hy4-flash ", "a\nb", "a\x7fb", "x" * 257])
def test_invalid_ids_are_rejected_without_echo(model):
    with pytest.raises(ValueError, match="invalid model"):
        replace(binding(), model=model)


@pytest.mark.parametrize("limits", [(0, 1), (4096, 4096), (4096, 4097), (True, 2), (5000, 1.5)])
def test_invalid_local_limits(limits):
    with pytest.raises(ValueError):
        binding(context_window=limits[0], max_tokens=limits[1])


@pytest.mark.parametrize("key", ["", "fixture key", "a\tb", "secret\ntext"])
def test_invalid_key_cannot_reach_environment(key):
    with pytest.raises(ValueError):
        replace(binding(), key=key)
