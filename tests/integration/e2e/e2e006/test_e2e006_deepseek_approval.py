from __future__ import annotations

import importlib.util
import inspect

from hyprial.daemon.impl.harnesses.codex.app_server import APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest


ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "contract" / "e2e" / "run-resident-providers.py"


def load_runner():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "e2e006_deepseek_approval_under_test", RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(RUNNER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(RUNNER.parent))
    return module


def test_pi_skill_uses_a_bounded_approved_project(tmp_path):
    runner = load_runner()
    skill = tmp_path / "source-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: source-skill\ndescription: fixture\n---\n")
    project = runner.stage_pi_project(tmp_path / "scenario", skill)
    case = runner.provider_cases(skill, pi_cwd=project)[0]
    assert case.args == ("--approve",)
    assert case.cwd == project
    assert (project / ".git").is_dir()
    assert (project / ".pi/skills/source-skill/SKILL.md").read_text() == (skill / "SKILL.md").read_text()
    from hyprial.daemon.impl.harnesses.pi.loader import resolve_approved_pi_project
    resolve_approved_pi_project(cwd=str(project), runtime_args=case.args)


def test_missing_agent_owned_key_is_blocked():
    runner = load_runner()
    assert runner.start_failure_status(
        "provider 'deepseek' requires an agent-owned DEEPSEEK_API_KEY; "
        "agent-home P2 never falls back to Pi auth under HOME"
    ) == "BLOCKED"


def test_codex_deepseek_probe_never_routes_auto_reviewer_to_provider(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    cases = {case.provider: case for case in runner.provider_cases(tmp_path)}

    # on-request, not never: `never` refuses MCP tool calls in codex's exec
    # runtime, which is what kept E2E-006 codex.message-reply red (card
    # 87dc8276, three-arm measurement 2026-09-05).  MCP tools raise no
    # prompt under on-request; shell approvals fall to hyprial's unattended
    # rejecter, which is the documented headless behaviour.
    assert cases["codex"].args == ("-a", "on-request", "-s", "workspace-write")
    assert "codex-auto-review" not in cases["codex"].args


def test_native_codex_probe_rejects_synthesized_turn_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = load_runner()
    case = next(
        item for item in runner.provider_cases(tmp_path) if item.provider == "codex"
    )
    request_id = "resident-codex-request"
    # Guard: the retired automatic-reply prefix (removed by the WP5-A hard cut)
    # must still be rejected as a "native" turn result.
    synthesized_id = str(
        uuid5(NAMESPACE_URL, f"hyprial-agent-sdk-reply:{request_id}")
    )
    reply = {
        "conversationId": "resident-codex-conversation",
        "messageId": synthesized_id,
        "message": "RESIDENT_CODEX_OK",
    }

    class Daemon:
        def command(self, *args: str) -> dict[str, object]:
            assert args[:2] == ("message", "send")
            return {"ok": True, "messageId": request_id}

    monkeypatch.setattr(
        runner,
        "wait_for_reply",
        lambda *_args, **_kwargs: {"pendingMessages": [reply]},
    )
    monkeypatch.setattr(
        runner,
        "pending_message_texts",
        lambda *_args, **_kwargs: [reply],
    )

    with pytest.raises(AssertionError, match="native harness_reply was required"):
        runner.native_probe_roundtrip(
            Daemon(), case, "resident-codex-conversation", 1.0
        )


def test_e2e006_launch_budget_matches_the_other_controlled_runners() -> None:
    """Card 54158b16: 45 s was a single fast launch's budget; E2E-013 and
    E2E-007 give 90 s, and a codex session start alone measures 9-43 s."""
    runner = load_runner()
    assert inspect.signature(runner.run_cli).parameters["timeout"].default == 90.0


def test_e2e006_post_restart_wait_covers_a_sequential_restore(tmp_path: Path) -> None:
    runner = load_runner()
    seen: dict[str, object] = {}

    class _Daemon:
        def wait(self, _read, _predicate, *, timeout: float) -> dict[str, object]:
            seen["timeout"] = timeout
            return {}

        def command(self, *_args: str, **_kwargs: object) -> dict[str, object]:
            return {}

    case = runner.provider_cases(tmp_path)[0]
    runner.wait_running(_Daemon(), case)  # type: ignore[arg-type]
    # 120 s > the composed codex startup budget (90 s); the post-restart wait
    # must outlast the launcher's own budget, not fail before it. (card 104164aa)
    assert seen["timeout"] == 120.0
    assert seen["timeout"] > APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT


def test_isolated_daemon_command_takes_a_timeout_for_slow_teardowns() -> None:
    sys.path.insert(0, str(RUNNER.parent))
    try:
        import _daemon_runner
    finally:
        sys.path.remove(str(RUNNER.parent))
    assert (
        "timeout" in inspect.signature(_daemon_runner.IsolatedDaemon.command).parameters
    )
