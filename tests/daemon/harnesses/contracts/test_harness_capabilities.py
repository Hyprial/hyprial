"""Capability declaration table tests (design §2.4).

The table is the single source of truth for what each (harness, mode) pair
can do.  These tests lock tonight's honest baseline: unlocking a capability
later requires changing the table and these assertions in the same PR.
"""

from __future__ import annotations

import importlib

import pytest

from hyprial.daemon.impl.harnesses.capabilities import (
    CapabilitySupport,
    declare,
    support,
)

from hyprial.kernel import SupportLevel

from hyprial.kernel import Capability

EXPECTED_ROWS = {
    ("claude", False),
    ("claude", True),
    ("pi", False),
    ("pi", True),
    ("codex", False),
    ("codex", True),
    ("dsh", False),
    ("dsh", True),
    ("jev", False),
    ("jev", True),
    ("user-proxy", False),
    ("user-proxy", True),
}

KNOWN_MECHANISMS = frozenset(
    {"channel", "agent_sdk", "rpc", "app_server", "dsh_api", "dsh_tui", "pty", "extension", "python_worker"}
)

# Mechanism -> implementation symbols that must exist (declaration drift
# guard: a native/degraded claim must link to real code).
MECHANISM_IMPLEMENTATIONS = {
    "channel": [
        ("hyprial.shell.impl.cli.commands.agent.runtime.claude", "_start_interactive_claude"),
        ("hyprial.daemon.impl.mcp.channel", "serve_channel_stdio"),
    ],
    "agent_sdk": [("hyprial.daemon.impl.harnesses.agent_sdk", "ClaudeAgentSdkProcess")],
    "rpc": [("hyprial.daemon.impl.harnesses.pi.rpc_process", "PiRpcProcess")],
    "app_server": [
        ("hyprial.daemon.impl.harnesses.codex", "CodexAppServerProcess"),
        ("hyprial.shell.impl.cli.commands.agent.runtime.sessions", "_start_interactive_codex"),
    ],
    "dsh_api": [("hyprial.daemon.impl.harnesses.dsh", "DshHarnessProcess")],
    "dsh_tui": [
        ("hyprial.shell.impl.cli.commands.agent.runtime.dsh", "_start_interactive_dsh"),
        ("hyprial.daemon.impl.harnesses.dsh_tui", "DshCarrierAdapter"),
    ],
    "pty": [("hyprial.daemon.impl.harnesses.protocol.common", "PtyHarnessProcess")],
    "extension": [
        ("hyprial.shell.impl.cli.commands.agent.runtime.sessions", "_start_interactive_pi"),
        ("hyprial.daemon.impl.harnesses.pi", "PI_HARNESS_ATTACH_EXTENSION"),
    ],
    "python_worker": [("hyprial.daemon.impl.harnesses.python_worker", "PythonHarnessProcess")],
}


def test_every_declared_row_is_one_of_the_expected_pairs() -> None:
    for harness in ("claude", "pi", "codex", "dsh", "jev", "user-proxy"):
        for headless in (False, True):
            row = declare(harness, headless=headless)
            assert isinstance(row, dict)
            assert row  # no empty rows


def test_rows_are_complete_and_well_formed() -> None:
    for harness, headless in EXPECTED_ROWS:
        row = declare(harness, headless=headless)
        for capability, entry in row.items():
            assert capability in Capability
            assert entry.level in SupportLevel
            assert entry.mechanism in KNOWN_MECHANISMS | {None}
            if entry.level is SupportLevel.DEGRADED:
                assert entry.note, "degraded entries must note what is lost"


def test_capability_support_rejects_unknown_mechanism() -> None:
    with pytest.raises(ValueError, match="unknown mechanism"):
        CapabilitySupport(SupportLevel.NATIVE, "smoke-signals")


def test_capability_support_requires_note_when_degraded() -> None:
    with pytest.raises(ValueError, match="must note"):
        CapabilitySupport(SupportLevel.DEGRADED, "pty")


def test_declared_mechanisms_have_implementation_symbols() -> None:
    used = {
        entry.mechanism
        for harness, headless in EXPECTED_ROWS
        for entry in declare(harness, headless=headless).values()
        if entry.mechanism is not None and entry.level is not SupportLevel.UNSUPPORTED
    }
    assert used <= KNOWN_MECHANISMS
    for mechanism in sorted(used):
        for module_name, symbol in MECHANISM_IMPLEMENTATIONS[mechanism]:
            module = importlib.import_module(module_name)
            assert hasattr(module, symbol), f"{module_name}.{symbol} missing"


def test_undeclared_harness_raises() -> None:
    # lark is an adapter, not a harness, and has no row tonight.
    with pytest.raises(KeyError):
        declare("lark", headless=True)


def test_jev_and_user_proxy_declare_bounded_python_workers() -> None:
    from hyprial.daemon.impl.harnesses.capabilities import HarnessConcurrency, concurrency

    jev = support("jev", headless=True, capability=Capability.HEADLESS_EXEC)
    assert jev is not None and jev.level is SupportLevel.NATIVE
    assert jev.mechanism == "python_worker"
    assert concurrency("jev", headless=True) == HarnessConcurrency("pool", 10)
    proxy = support("user-proxy", headless=True, capability=Capability.HEADLESS_EXEC)
    assert proxy is not None and proxy.level is SupportLevel.NATIVE
    assert proxy.mechanism == "python_worker"
    # A relay must keep order (design-user-proxy-harness.md §7).
    assert concurrency("user-proxy", headless=True) == HarnessConcurrency("sequential", 1)


def test_user_proxy_sends_only_as_a_forward() -> None:
    send = support("user-proxy", headless=True, capability=Capability.PROACTIVE_SEND)
    assert send is not None and send.level is SupportLevel.DEGRADED
    assert "forward" in (send.note or "")
    model = support("user-proxy", headless=True, capability=Capability.MODEL_SELECT)
    assert model is not None and model.level is SupportLevel.UNSUPPORTED


# --- Golden baseline assertions (change table + tests together to unlock) ---


def test_golden_cc_interactive_attach_is_channel() -> None:
    entry = support(
        "claude", headless=False, capability=Capability.INTERACTIVE_ATTACH
    )
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "channel"


@pytest.mark.parametrize(
    "capability",
    [
        Capability.INTERACTIVE_ATTACH,
        Capability.WAKE_PUSH,
        Capability.PROACTIVE_SEND,
        Capability.TOOL_INJECTION,
    ],
)
def test_golden_pi_interactive_attach_capabilities_are_extension(
    capability: Capability,
) -> None:
    """Unlocked by feat/tui-attach: the pi_harness_attach.ts carrier plus the
    generalized daemon carrier protocol (register/refresh/heartbeat over the
    closed SESSION_CARRIER_SOURCES set) make the interactive pi session a
    first-class Harness citizen."""

    entry = support("pi", headless=False, capability=capability)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "extension"


def test_golden_pi_interactive_session_lifecycle_is_extension() -> None:
    entry = support("pi", headless=False, capability=Capability.SESSION_LIFECYCLE)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "extension"


def test_golden_cc_headless_exec_is_agent_sdk() -> None:
    entry = support("claude", headless=True, capability=Capability.HEADLESS_EXEC)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "agent_sdk"


def test_golden_pi_headless_exec_is_rpc() -> None:
    entry = support("pi", headless=True, capability=Capability.HEADLESS_EXEC)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "rpc"


@pytest.mark.parametrize(
    "capability",
    [
        Capability.HEADLESS_EXEC,
        Capability.WAKE_PUSH,
        Capability.TURN_CONTROL,
        Capability.EVENT_STREAM,
    ],
)
def test_golden_codex_headless_app_server_capabilities_are_native(
    capability: Capability,
) -> None:
    entry = support("codex", headless=True, capability=capability)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "app_server"


def test_golden_codex_session_lifecycle_resumes_across_daemon_restart() -> None:
    entry = support(
        "codex", headless=True, capability=Capability.SESSION_LIFECYCLE
    )
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "app_server"


def test_golden_codex_interactive_attach_is_app_server() -> None:
    entry = support(
        "codex", headless=False, capability=Capability.INTERACTIVE_ATTACH
    )
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "app_server"


def test_golden_codex_interactive_wake_push_is_app_server() -> None:
    entry = support("codex", headless=False, capability=Capability.WAKE_PUSH)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "app_server"


@pytest.mark.parametrize(
    ("harness", "level", "mechanism", "plugin_kinds"),
    (
        (
            "claude",
            SupportLevel.NATIVE,
            "channel",
            frozenset({"mcp", "skill", "claude-plugin"}),
        ),
        (
            "pi",
            SupportLevel.NATIVE,
            "extension",
            frozenset({"skill", "pi-extension"}),
        ),
        ("codex", SupportLevel.DEGRADED, "app_server", frozenset({"mcp"})),
        (
            "dsh",
            SupportLevel.DEGRADED,
            "dsh_tui",
            frozenset({"mcp"}),
        ),
    ),
)
def test_golden_interactive_plugin_injection_is_declared_once(
    harness: str,
    level: SupportLevel,
    mechanism: str | None,
    plugin_kinds: frozenset[str],
) -> None:
    entry = support(
        harness, headless=False, capability=Capability.PLUGIN_INJECTION
    )
    assert entry is not None
    assert entry.level is level
    assert entry.mechanism == mechanism
    assert entry.plugin_kinds == plugin_kinds
    if level is SupportLevel.DEGRADED:
        assert "MCP servers" in (entry.note or "")


@pytest.mark.parametrize(
    "capability", [Capability.PROACTIVE_SEND, Capability.TOOL_INJECTION]
)
def test_golden_codex_interactive_has_the_harness_mcp_surface(
    capability: Capability,
) -> None:
    # WP5-B: proven by tests/daemon/harnesses/codex/test_codex_interactive_mcp.py
    entry = support("codex", headless=False, capability=capability)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "app_server"


def test_golden_codex_interactive_session_lifecycle_supports_exact_resume() -> None:
    entry = support(
        "codex", headless=False, capability=Capability.SESSION_LIFECYCLE
    )
    assert entry is not None
    assert entry.level is SupportLevel.DEGRADED
    assert entry.mechanism == "app_server"
    assert "exact thread resume" in (entry.note or "")
    assert "daemon-restart recovery" in (entry.note or "")


def test_golden_dsh_interactive_attach_is_the_tui_carrier() -> None:
    entry = support("dsh", headless=False, capability=Capability.INTERACTIVE_ATTACH)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "dsh_tui"


@pytest.mark.parametrize(
    "capability",
    [
        Capability.WAKE_PUSH,
        Capability.PROACTIVE_SEND,
        Capability.TOOL_INJECTION,
    ],
)
def test_golden_dsh_interactive_carrier_is_coordinator_capable(
    capability: Capability,
) -> None:
    entry = support("dsh", headless=False, capability=capability)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "dsh_tui"


def test_golden_dsh_interactive_model_selection_is_owned_by_the_tui() -> None:
    entry = support("dsh", headless=False, capability=Capability.MODEL_SELECT)
    assert entry is not None
    assert entry.level is SupportLevel.UNSUPPORTED
    assert "dsh-tui" in (entry.note or "")


def test_golden_dsh_interactive_session_lifecycle_is_degraded_until_proven() -> None:
    entry = support("dsh", headless=False, capability=Capability.SESSION_LIFECYCLE)
    assert entry is not None
    assert entry.level is SupportLevel.DEGRADED
    assert entry.mechanism == "dsh_tui"
    assert "daemon-restart recovery" in (entry.note or "")


def test_golden_dsh_headless_exec_is_native_api() -> None:
    entry = support("dsh", headless=True, capability=Capability.HEADLESS_EXEC)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "dsh_api"


@pytest.mark.parametrize(
    "capability", [Capability.PROACTIVE_SEND, Capability.TOOL_INJECTION]
)
def test_golden_dsh_headless_worker_tools_are_native_api(
    capability: Capability,
) -> None:
    entry = support("dsh", headless=True, capability=capability)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "dsh_api"


def test_golden_dsh_worker_session_is_not_resumed_across_daemon_restart() -> None:
    entry = support("dsh", headless=True, capability=Capability.SESSION_LIFECYCLE)
    assert entry is not None
    assert entry.level is SupportLevel.DEGRADED
    assert entry.mechanism == "dsh_api"
    assert "fresh worker MCP identity" in (entry.note or "")
    assert "daemon restart" in (entry.note or "")


@pytest.mark.parametrize(
    "capability", [Capability.PROACTIVE_SEND, Capability.TOOL_INJECTION]
)
def test_golden_codex_headless_worker_tools_are_native_app_server(
    capability: Capability,
) -> None:
    entry = support("codex", headless=True, capability=capability)
    assert entry is not None
    assert entry.level is SupportLevel.NATIVE
    assert entry.mechanism == "app_server"


def test_golden_cc_managed_pi_and_dsh_can_coordinate() -> None:
    """Coordinator = attach-or-exec plus wake/send/tools, all native.

    Pi headless joined the coordinator-capable set when the RPC worker grew
    the harness-bridge extension carrier (pi_harness_bridge.ts), codex
    headless joined when CodexAppServerProcess wired the worker_channel
    through thread/start config overrides, and interactive pi joined when the
    attach carrier (pi_harness_attach.ts) plus the generalized daemon carrier
    protocol landed: managed pi and codex workers can now proactively send
    and use the injected harness_* toolset as their own canonical actor, and
    so can an attached interactive pi session.  Interactive codex joined with
    WP5-B (harness-bridge MCP mounted on its app-server).
    """

    def can_coordinate(harness: str, headless: bool) -> bool:
        row = declare(harness, headless=headless)

        def native(capability: Capability) -> bool:
            entry = row.get(capability)
            return entry is not None and entry.level is SupportLevel.NATIVE

        anchor = native(Capability.INTERACTIVE_ATTACH) or native(
            Capability.HEADLESS_EXEC
        )
        return anchor and all(
            native(capability)
            for capability in (
                Capability.WAKE_PUSH,
                Capability.PROACTIVE_SEND,
                Capability.TOOL_INJECTION,
            )
        )

    coordinators = {
        (harness, headless)
        for harness, headless in EXPECTED_ROWS
        if can_coordinate(harness, headless)
    }
    assert coordinators == {
        ("claude", False),
        ("claude", True),
        ("pi", False),
        ("pi", True),
        ("codex", False),
        ("codex", True),
        ("dsh", False),
        ("dsh", True),
    }
