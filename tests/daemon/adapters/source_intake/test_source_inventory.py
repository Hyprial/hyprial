"""Every planned platform must ship importable source without starting a backend."""

import builtins
import importlib

import pytest


PLATFORMS = (
    "lark", "weixin", "dingtalk", "wecom", "qq", "slack", "telegram",
    "discord", "whatsapp", "imessage", "matrix", "mail",
)
OPTIONAL_BACKENDS = frozenset({
    "dingtalk_stream", "aibot", "wecom_aibot_sdk", "nio", "vodozemac", "tryx",
})


@pytest.mark.parametrize("platform", PLATFORMS)
def test_platform_source_import_does_not_require_optional_backend(platform, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name.split(".", 1)[0] not in OPTIONAL_BACKENDS, name
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    package = importlib.import_module(f"hyprial.daemon.impl.adapters.{platform}")
    assert package.__file__
    assert package.__all__
    assert any(callable(getattr(package, name)) for name in package.__all__)


def test_wecom_bot_and_application_keep_distinct_runtime_contracts():
    package = importlib.import_module("hyprial.daemon.impl.adapters.wecom")
    assert package.WeComBotRuntime is not package.WeComApplicationRuntime
    assert package.WeComBotCredentials is not package.WeComApplicationCredentials
