"""Starting the daemon must not import the whole Lark SDK.

``import lark_oapi`` runs the package initialiser, and that imports the entire
generated API surface: 10,416 modules, measured on 2026-09-24 as 15.7s of a
17.7s ``import hyprial.daemon.application`` on the packaged Windows runtime.
The daemon reaches this module on every start, so it paid that even when nobody
had ever signed in to Lark.

``hyprial.daemon.impl.adapters.lark.sdk`` now resolves those names on first use instead.
Nothing on a warm developer machine makes the difference visible, which is why
the guard is a subprocess that reads ``sys.modules`` rather than a timing.
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark.outbound import sdk_base


def _spawn(script: str) -> subprocess.CompletedProcess[str]:
    source = Path(__file__).resolve().parents[5] / 'src'
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_a_daemon_start_that_never_touches_lark_does_not_import_the_sdk() -> None:
    result = _spawn("""
import sys
import hyprial.daemon.impl.application  # noqa: F401
unexpected = sorted(name for name in sys.modules if name.startswith("lark_oapi"))
assert not unexpected, unexpected
""")
    assert result.returncode == 0, result.stderr


def test_the_deferred_lark_names_still_resolve_to_the_sdk() -> None:
    result = _spawn("""
import sys
from hyprial.daemon.impl.adapters.lark import sdk
assert "lark_oapi" not in sys.modules, "importing the adapter must not import the SDK"
assert sdk.lark.Client.__name__ == "Client"
assert sdk.lark.Client is sdk.lark.Client, "a deferred name must resolve once, not per access"
assert sdk.GetMessageRequest.builder() is not None
assert "lark_oapi" in sys.modules, "the deferred name never reached the SDK"
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["lark_oapi", "lark_oapi.api.im.v1"])
def test_deferred_external_resolver_is_lazy_and_cached(module, monkeypatch) -> None:
    calls = []
    symbol = object()

    def resolve(name):
        calls.append(name)
        return SimpleNamespace(Symbol=symbol)

    monkeypatch.setattr(sdk_base.importlib, "import_module", resolve)
    deferred = sdk_base._Deferred(module, "Symbol")
    assert calls == []
    assert deferred._resolve() is symbol
    assert deferred._resolve() is symbol
    assert calls == [module]


@pytest.mark.parametrize("module", ["hyprial.daemon.impl.application", "lark_oapi.api.contact.v3"])
def test_deferred_loader_rejects_unreviewed_modules_before_import(module, monkeypatch) -> None:
    calls = []

    def resolve(name):
        calls.append(name)
        return SimpleNamespace()

    monkeypatch.setattr(sdk_base.importlib, "import_module", resolve)
    deferred = sdk_base._Deferred(module)
    with pytest.raises(ValueError, match="unsupported deferred SDK module"):
        deferred._resolve()
    assert calls == []
