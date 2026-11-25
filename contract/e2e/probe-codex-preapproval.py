#!/usr/bin/env python3
"""PROBE (not a gate): does per-server pre-approval let harness tools through?

⛔ This is not an E2E scenario and nothing gates on it. It exists to answer one
question that no amount of reading settles, and to answer it in a way whose
negative result still means something.

The question
------------
`4aab5b0` launches codex with `-a never`. Codex then never raises an approval
request at all, so the harness's MCP tools are refused without anyone being
asked. Allen ruled (2026-08-31) that the harness MCP server should instead be
pre-approved -- `default_tools_approval_mode = "auto"`, added in
`_worker_channel_config` (codex.py). Whether pre-approval actually runs *before*
codex's "does this need approval?" step -- i.e. whether it gets past a global
`never` -- is **unmeasured**. That is what this probe measures.

Two arms, and why the order matters
-----------------------------------
    arm 1   -a on-request + pre-approval    does the key land on this path at all?
    arm 2   -a never      + pre-approval    can it get past `never`?  (the real question)

**Run arm 1 first.** Arm 2 has an outcome that is *silence*, and silence cannot
be read until every other cause of silence has been cleared. If arm 2 ran first
and the tools failed, "the key never reached codex" and "codex refused despite
the key" would look identical. Arm 1 is the step that removes the first one.

The control is not optional
---------------------------
Each arm also runs **without** the key. That arm must show an approval request.
If it does not, the run never exercised a tool call at all, and the arm *with*
the key proves nothing -- its success and "the experiment did not happen" are
the same observation.

Why we do not grep for a method name
------------------------------------
The obvious observable -- grep the hyprial logs for a refusal string -- was tried
and is wrong twice over. That string lives only in the `execCommandApproval` /
`applyPatchApproval` branches, and codex has no MCP-specific approval method at
all; MCP tool approval appears to travel by MCP *elicitation*, which hyprial does
not handle anywhere. Guessing the name right requires being right twice (the
name, then the semantics of the reply).

So this probe does not predict the signature. `CodexAppServerProcess` records
**every** server-request method it receives, whatever it is called, before it
answers (codex.py:1544). The signal is therefore the **set difference** between
the two arms: whatever requests the control gets and the pre-approved arm does
not. That is the same move as the config experiment's third arm -- let the
control establish the signature instead of predicting it.

What this probe deliberately does NOT do
----------------------------------------
It prints evidence; it does not decide. Arm 2 has **three** outcomes, not two,
and they call for different actions:

    tools succeed, no approval requested   pre-approval beat `never`
    codex refuses the call itself          `never` won; capability table needs
                                           the degraded entry (notes §7)
    an approval request DID arrive         `never` was not in force at all --
                                           a third state, investigate separately

Collapsing the last two into "it failed" throws away a whole branch. Read the
printed evidence against docs/notes/e2e-controlled-codex-line-2026-08-31.md §7.

⚠️ Two surfaces, not one
------------------------
Only the hyprial side is captured here. Codex's own refusal sentence does not appear
on any hyprial surface -- `contract/e2e/README.md:131` says so explicitly,
and grepping the hyprial side for it is what cost three days. For arm 2, also read
the rollout under the run-scoped CODEX_HOME this probe creates. ⛔ Do not read
`~/.codex/sessions`: that is the developer's own directory, and it will be full
of unrelated content that looks like content.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import ROOT, short_temp_root
from _deepseek import (
    MODEL as DEEPSEEK_MODEL,
    PROVIDER as DEEPSEEK_PROVIDER,
    DeepSeekUnavailable,
    deepseek_environment,
)

sys.path.insert(0, str(ROOT / "src"))
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import codex as codex_module
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerProcess
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

CODEX = Path(os.environ.get("HYPRIAL_CODEX_BIN") or shutil.which("codex") or "codex")

PRE_APPROVAL_KEY = "default_tools_approval_mode"

# Written before the run, on purpose. An arm whose expected shape was not stated
# in advance cannot be failed by its output -- whatever comes back gets read as
# confirmation of something.
EXPECTATIONS = {
    ("on-request", "control"): "MUST record at least one server-request method",
    ("on-request", "pre-approved"): "expect no approval request; tool call succeeds",
    ("never", "control"): "MUST show the tool refused (codex asks nobody)",
    ("never", "pre-approved"): "THE QUESTION -- three outcomes, see module docstring",
}


def emit(arm: str, variant: str, payload: dict[str, object]) -> None:
    """One machine-readable line per arm. Judgement is the reader's."""

    print(
        f"PROBE {arm} {variant} " + json.dumps(payload, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _without_pre_approval(original):
    """Strip the key on the way out -- the control arm, built from the real path.

    Patching the seam rather than hand-rolling a second config keeps the control
    byte-identical to the treatment except for the one field. A hand-written
    control would differ in ways nobody enumerated.
    """

    def patched(channel):
        config = original(channel)
        config["mcp_servers"]["harness-bridge"].pop(PRE_APPROVAL_KEY, None)
        return config

    return patched


def run_arm(
    *,
    approval_policy: str,
    variant: str,
    codex_home: Path,
    provider_environment: dict[str, str],
    workspace: Path,
) -> dict[str, object]:
    """One arm. Returns evidence; decides nothing."""

    channel = build_worker_channel(
        actor="agent:probe:local:codex-preapproval",
        session_ref=f"probe-{approval_policy}-{variant}",
        hyprial_home=workspace / f"home-{approval_policy}-{variant}",
        state_dir=workspace / f"state-{approval_policy}-{variant}",
    )
    spec = HarnessLaunchSpec(
        harness="codex",
        name=f"codex-preapproval-{approval_policy}-{variant}",
        headless=True,
        cwd=str(workspace),
        args=("-a", approval_policy, "-s", "workspace-write"),
        model_provider=DEEPSEEK_PROVIDER,
        model=DEEPSEEK_MODEL,
    )
    environment = dict(provider_environment)
    # Run-scoped, so the rollout for surface 2 lands somewhere known and the
    # developer's own ~/.codex is neither read nor written.
    environment["CODEX_HOME"] = str(codex_home)

    original = codex_module._worker_channel_config
    if variant == "control":
        codex_module._worker_channel_config = _without_pre_approval(original)
    try:
        process = CodexAppServerProcess(
            spec,
            command=(str(CODEX.resolve()),),
            env=environment,
            worker_channel=channel,
        )
        try:
            if not process.wait_ready(timeout=30):
                return {"ready": False, "error": process.last_error}
            process.enqueue(
                HarnessDelivery(
                    f"probe-{approval_policy}-{variant}",
                    "codex-preapproval-probe",
                    "sender",
                    "agent:probe:local:sender",
                    # Names the tool explicitly: an ambiguous prompt lets the
                    # model route around the tool, and "the model chose not to"
                    # is indistinguishable from "the tool was refused".
                    "Call the harness_whoami tool from the harness-bridge MCP "
                    "server and reply with exactly what it returns. If the tool "
                    "is unavailable or refused, reply exactly TOOL_REFUSED and "
                    "say which error you saw.",
                )
            )
            deadline = time.monotonic() + 180
            outcome = None
            while time.monotonic() < deadline:
                values = process.drain_results()
                if values:
                    outcome = values[0]
                    break
                time.sleep(0.1)
            methods = list(process.server_request_methods)
            return {
                "ready": True,
                "status": None if outcome is None else str(outcome.status),
                "completed": outcome is not None
                and outcome.status is HarnessResultStatus.COMPLETED,
                # ⭐ The attributable signal. `server_request_methods` is an
                # aggregate with no server identity in it, so it corroborates;
                # it cannot say which server caused a request.
                "tool_text": None if outcome is None else str(getattr(outcome, "text", ""))[:400],
                "server_request_methods": methods,
                "codex_home": str(codex_home),
            }
        finally:
            process.stop()
    finally:
        codex_module._worker_channel_config = original


def assert_control_is_actually_different() -> None:
    """Refuse to run if the control arm is not, in fact, a control.

    ⚠️ The control is built by patching `codex_module._worker_channel_config`.
    If that patch fails to take -- the name moves, a caller captures a local
    reference, the key stops being injected there -- then the "control" is a
    second treatment arm. Both arms would then show no approval request, and
    the probe would report that pre-approval works **because nothing differed**.

    That is the exact failure this probe exists to avoid, so it is checked
    before any codex process starts, and it aborts rather than warns. Both
    directions are asserted: the key present unpatched, absent patched. Only
    checking the patched side would pass if the key were never injected at all.

    (`CodexAppServerClient` calls the function through a module-global lookup,
    which is why patching the module attribute reaches it. Note it is that class
    and not `CodexAppServerProcess` -- looking for the call on the process class
    finds nothing and reads like "the patch point is off the path".)
    """

    workspace = Path(tempfile.mkdtemp(prefix="codex-preapproval-selfcheck-"))
    try:
        channel = build_worker_channel(
            actor="agent:probe:local:selfcheck",
            session_ref="selfcheck",
            hyprial_home=workspace / "home",
            state_dir=workspace / "state",
        )
        original = codex_module._worker_channel_config
        treated = original(channel)["mcp_servers"]["harness-bridge"]
        if PRE_APPROVAL_KEY not in treated:
            raise AssertionError(
                f"{PRE_APPROVAL_KEY} is not injected at all -- this probe compares "
                "a treatment that does not exist against a control, and both arms "
                "would agree for the wrong reason"
            )
        try:
            codex_module._worker_channel_config = _without_pre_approval(original)
            control = codex_module._worker_channel_config(channel)["mcp_servers"]["harness-bridge"]
        finally:
            codex_module._worker_channel_config = original
        if PRE_APPROVAL_KEY in control:
            raise AssertionError(
                "the control patch did not take -- the 'control' arm would carry "
                "pre-approval too, and two identical arms would be read as proof "
                "that pre-approval works"
            )
        print(
            f"PROBE SELFCHECK ok treatment_has_key=True control_has_key=False "
            f"key={PRE_APPROVAL_KEY}",
            flush=True,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    # Before anything expensive, and before anything that costs provider tokens.
    assert_control_is_actually_different()
    try:
        provider_environment = deepseek_environment()
    except DeepSeekUnavailable as unavailable:
        print(f"PROBE SKIP provider unavailable: {unavailable}", flush=True)
        return 2

    root = Path(short_temp_root(tempfile.mkdtemp(prefix="codex-preapproval-")))
    evidence: dict[str, dict[str, object]] = {}
    try:
        # Arm 1 before arm 2: see the module docstring. Arm 2's silence is not
        # readable until arm 1 has cleared "the key never landed".
        for approval_policy in ("on-request", "never"):
            for variant in ("control", "pre-approved"):
                key = f"{approval_policy}/{variant}"
                payload = run_arm(
                    approval_policy=approval_policy,
                    variant=variant,
                    codex_home=root / f"codex-home-{approval_policy}-{variant}",
                    provider_environment=provider_environment,
                    workspace=root,
                )
                payload["expected"] = EXPECTATIONS[(approval_policy, variant)]
                evidence[key] = payload
                emit(approval_policy, variant, payload)

        def methods_of(key: str) -> set[str]:
            value = evidence[key].get("server_request_methods")
            return set(value) if isinstance(value, list) else set()

        for approval_policy in ("on-request", "never"):
            control = methods_of(f"{approval_policy}/control")
            treated = methods_of(f"{approval_policy}/pre-approved")
            emit(
                approval_policy,
                "difference",
                {
                    # Whatever the control was asked and the pre-approved arm was
                    # not. No method name was predicted anywhere in this probe.
                    "suppressed_by_pre_approval": sorted(control - treated),
                    "still_requested": sorted(treated),
                    "control_saw_nothing": not control,
                    "⚠️": (
                        "control_saw_nothing means this arm never exercised a tool "
                        "call -- the pre-approved half of it says nothing"
                        if not control
                        else "control fired, so the pre-approved half is readable"
                    ),
                },
            )
        print(
            "PROBE DONE -- evidence only. Read it against "
            "docs/notes/e2e-controlled-codex-line-2026-08-31.md §7, and for the "
            "`never` arm also read the rollout under the CODEX_HOME printed "
            "above. The hyprial side does not carry codex's own refusal sentence.",
            flush=True,
        )
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
