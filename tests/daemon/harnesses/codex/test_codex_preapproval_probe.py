"""The probe's own control check has to be checked by something.

`contract/e2e/probe-codex-preapproval.py` is not a gate -- nothing
depends on it, and it only runs when a human runs it. That is fine for a probe,
but it means the probe can rot for weeks and the first sign of it would be a
run whose evidence is quietly worthless.

The part worth protecting is `assert_control_is_actually_different`. It exists
because the probe's control arm is built by patching a module attribute, and if
that patch stops taking -- the function is renamed, a caller starts holding a
local reference, the key stops being injected there -- then the "control" is a
second treatment arm. Both arms would agree, and agreement is exactly what the
probe reads as "pre-approval worked". **The failure mode points at the
conclusion we are hoping for**, which is the worst kind to leave unguarded.

These tests run in the normal suite. They start no codex process, spend no
provider tokens, and touch no network: the self-check only builds a worker
channel and calls the injection function.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
PROBE_PATH = ROOT / "contract" / "e2e" / "probe-codex-preapproval.py"


def _load_probe():
    """Import the probe by path -- its filename is not a Python identifier."""

    scenarios = str(PROBE_PATH.parent)
    if scenarios not in sys.path:
        sys.path.insert(0, scenarios)
    spec = importlib.util.spec_from_file_location("_probe_preapproval", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_still_imports() -> None:
    """The cheapest rot detector: a probe nothing imports stops importing.

    Every other test here would pass vacuously if collection of the probe
    failed, so this one exists to fail first and say why.
    """

    probe = _load_probe()
    assert hasattr(probe, "assert_control_is_actually_different")
    assert probe.PRE_APPROVAL_KEY == "default_tools_approval_mode"


def test_the_control_self_check_passes_against_the_real_injection() -> None:
    """Green means: the key is injected, and the control patch removes it."""

    probe = _load_probe()
    probe.assert_control_is_actually_different()


def test_the_self_check_fails_when_the_control_patch_does_nothing() -> None:
    """The mutation the self-check exists for.

    A no-op patch turns the control arm into a second treatment arm. If this
    goes green, the self-check cannot detect an inert control -- which is the
    one thing it is for.
    """

    probe = _load_probe()
    probe._without_pre_approval = lambda original: original
    with pytest.raises(AssertionError, match="control patch did not take"):
        probe.assert_control_is_actually_different()


def test_the_self_check_fails_when_the_key_is_never_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, and the reason both halves are asserted.

    A self-check that only verified "the patched config lacks the key" would
    pass in a world where the key is never injected at all -- the probe would
    then compare a treatment that does not exist against a control, and the two
    arms would agree for entirely the wrong reason.
    """

    probe = _load_probe()
    from hyprial.daemon.impl.harnesses import codex as codex_module

    original = codex_module._worker_channel_config

    def without_the_key(channel):
        config = original(channel)
        config["mcp_servers"]["harness-bridge"].pop(probe.PRE_APPROVAL_KEY, None)
        return config

    monkeypatch.setattr(codex_module, "_worker_channel_config", without_the_key)
    with pytest.raises(AssertionError, match="is not injected at all"):
        probe.assert_control_is_actually_different()


def test_the_probe_states_its_expectations_before_it_runs() -> None:
    """Every arm must have a written expectation, set before any output exists.

    An arm whose expected shape was never stated cannot be failed by its result;
    whatever comes back gets read as confirmation of something. The `never` /
    control arm is named explicitly because it is the positive control: if it
    were dropped, the pre-approved arm's success and "the experiment never ran"
    would be the same observation.
    """

    probe = _load_probe()
    arms = {("on-request", "control"), ("on-request", "pre-approved"),
            ("never", "control"), ("never", "pre-approved")}
    assert set(probe.EXPECTATIONS) == arms
    assert all(str(value).strip() for value in probe.EXPECTATIONS.values())
