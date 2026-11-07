"""An alert that only fires on failure is an alert nobody has ever seen work.

Twice on 2026-08-31 this channel was needed and twice nothing arrived. The
reasons were different and the symptom was identical:

    15:17  the process died on `cannot import name 'updates' from 'hyprial'`
           before it reached the alert at all -- no marker, no message
    17:30  the alert ran and could not deliver: the owner's channel is stored
           as `channel:org:node:allen-squire` while the gateway is named
           `allen-squire`, and the lookup compared them directly

⚠️ Both were invisible for one reason: **nothing used this code while things
were fine**. So the fix is not a better failure path -- it is to stop having a
failure-only path. Every upgrade now self-checks and reports, and the short
"all good" line is not noise: it is the evidence that the channel still works,
arriving at the only time we can afford to find out that it does not.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.shell.impl.cli.commands.common import pending_restart
from hyprial.shell.impl.cli.commands.upgrade import commands as upgrade_commands

import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.adapters.lark import sdk as lark_sdk
from hyprial.daemon.impl.autoupdate.alert import (
    UPGRADE_ALREADY_CURRENT,
    UPGRADE_DECLINED_DOWNGRADE,
    UPGRADE_FAILED,
    UPGRADE_INSTALLED,
    UPGRADE_UNCONFIRMED,
    SelfCheckResult,
    notify_upgrade_outcome,
    run_self_check,
)
from hyprial.kernel import DaemonLaunchResult
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _locked_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    def export_constraints(
        url: str, commit: str, destination: Path, *, runner: object = None
    ) -> Path:
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_owner_dm(self, open_id: str, text: str, *, idempotency_key: str) -> str:
        self.sent.append(text)
        return "om_fake"


def _home(tmp_path: Path, *, channel: str) -> tuple[Path, Path]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    (home / "secrets").mkdir(parents=True)
    state.mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "allen-squire",
                        "appId": "cli_test",
                        "credentialRef": "lark-allen-squire",
                        "routes": [
                            {"name": "r", "type": "direct", "nativeId": "ou_x"}
                        ],
                    }
                ],
            }
        ),
        "utf-8",
    )
    (home / "secrets" / "lark-allen-squire.json").write_text(
        json.dumps({"appSecret": "s"}), "utf-8"
    )
    (state / "users.json").write_text(
        json.dumps(
            {
                "version": 1,
                "users": [
                    {
                        "owner": "allen",
                        "ownerKey": "allen",
                        "loginName": "aaron@example",
                        "agents": [],
                        "preferredReceiver": {"machine": "m", "machineKey": "m"},
                        "notificationRules": [],
                        "ownerOpenId": {
                            "channel": channel,
                            "openId": "ou_allen",
                            "boundAt": "2026-08-31T00:00:00Z",
                        },
                    }
                ],
            }
        ),
        "utf-8",
    )
    return home, state


def _notify(
    tmp_path: Path,
    *,
    ok: bool,
    action: str = UPGRADE_INSTALLED,
    channel: str = "allen-squire",
):
    home, state = _home(tmp_path, channel=channel)
    recorder = _Recorder()
    outcome = notify_upgrade_outcome(
        hyprial_home=home,
        state_dir=state,
        host="box-1",
        action=action,
        upgrade_detail="dev (0.4.3)",
        check=SelfCheckResult(ok, "hyprial 0.4.3 answers `version --json`" if ok else "boom"),
        idempotency_key="k",
        sender_factory=lambda app_id, secret: recorder,
    )
    return outcome, recorder


# ---------------------------------------------------------------------------
# ⭐ The bug that made every alert undeliverable, in both spellings.
# ---------------------------------------------------------------------------


def test_the_owner_channel_resolves_whether_it_is_a_uri_or_a_bare_name(
    tmp_path: Path,
) -> None:
    """`channel:org:node:allen-squire` and `allen-squire` are the same channel.

    Production stores the first; `channels.json` names the second. The original
    lookup compared them directly, so it never matched -- every alert reported
    "which is not configured" and none was ever sent. It stayed hidden because
    the only code path that ran it was the failure path, which had never run.
    """

    spellings = (
        "allen-squire",
        "channel:bobadmin:node-hq-01.example-tailnet.ts.net:allen-squire",
    )
    for index, channel in enumerate(spellings):
        outcome, recorder = _notify(tmp_path / f"case{index}", ok=True, channel=channel)
        assert outcome.delivered is True, (
            f"{channel!r} did not resolve: {outcome.reason}"
        )
        assert len(recorder.sent) == 1


def test_a_binding_that_is_not_a_channel_uri_does_not_match_by_its_tail(
    tmp_path: Path,
) -> None:
    """⭐ The observation that separates the shared reader from a bare split.

    The first fix took whatever followed the last colon. That resolves
    `garbage:allen-squire` to the `allen-squire` gateway -- a malformed binding
    quietly steering the owner's alert at a gateway it never named. The
    address-parsing guard refused that split, and going through
    `parse_channel_uri` fixes both things at once: it is the grammar's one
    reader, and it requires the prefix and four segments, so a string that is
    not a channel URI is reported rather than guessed at.
    """

    outcome, recorder = _notify(tmp_path, ok=True, channel="garbage:allen-squire")
    assert outcome.delivered is False
    assert recorder.sent == []
    assert "garbage:allen-squire" in (outcome.reason or "")


# ---------------------------------------------------------------------------
# ⭐ Control groups ① and ②: short when healthy, detailed otherwise.
# ---------------------------------------------------------------------------


def test_a_healthy_upgrade_sends_one_short_line(tmp_path: Path) -> None:
    """Short on purpose: this message arrives after every upgrade.

    Its job is not to inform -- it is to prove the channel still works, on a day
    when nothing is wrong. If it were long it would be ignored, and an ignored
    proof is not a proof.
    """

    outcome, recorder = _notify(tmp_path, ok=True)
    assert outcome.delivered is True
    text = recorder.sent[0]
    assert "✅" in text
    assert "自检" not in text, "the healthy line must not carry the diagnostic body"
    assert len(text.splitlines()) <= 2, f"not short: {text!r}"
    # ⭐ Asserted from this side too, not only from the no-op's. Both lines are
    # short and both start with ✅; if they also read alike, then "did this
    # machine upgrade today?" is a question the owner cannot answer from the
    # message -- two facts sharing one observation, which is the failure this
    # whole change is about.
    assert "已是最新" not in text and "未安装" not in text, (
        f"a completed upgrade is worded like the no-op heartbeat: {text!r}"
    )


def test_a_failed_self_check_sends_the_detail(tmp_path: Path) -> None:
    """The upgrade installed and the result does not work -- that needs words."""

    outcome, recorder = _notify(tmp_path, ok=False)
    assert outcome.delivered is True
    text = recorder.sent[0]
    assert "⚠️" in text and "自检" in text and "boom" in text
    assert "box-1" in text


def test_a_failed_upgrade_sends_the_detail_too(tmp_path: Path) -> None:
    outcome, recorder = _notify(tmp_path, ok=False, action=UPGRADE_FAILED)
    assert "⚠️" in recorder.sent[0]
    assert "dev (0.4.3)" in recorder.sent[0]


# ---------------------------------------------------------------------------
# ⭐ The state that used to be reported as two different wrong things.
#
# `upgrade` runs on a timer, so "nothing to install" is the ordinary outcome --
# which makes it the one that must be right. It was reported as `✅ 升级完成`
# (a hardcoded `True` at the call site), and the obvious repair -- forward the
# result's real `upgraded` -- would have reported it as `⚠️ hyprial 升级失败`. Both
# tests below exist because both spellings were reachable.
# ---------------------------------------------------------------------------


def test_a_run_that_installed_nothing_says_so_and_still_sends(
    tmp_path: Path,
) -> None:
    """The heartbeat, and it must be honest about what it is.

    Still delivered: this is the line that proves, on an ordinary day, that the
    channel bad news would travel down is open. But it must not claim an
    upgrade happened -- that is the message someone reads when they are trying
    to work out whether a machine picked up a fix.
    """

    outcome, recorder = _notify(tmp_path, ok=True, action=UPGRADE_ALREADY_CURRENT)
    assert outcome.delivered is True, "the heartbeat must still be sent"
    text = recorder.sent[0]
    assert "✅" in text
    assert "已是最新" in text and "未安装" in text
    assert "升级完成" not in text, (
        "a run that installed nothing reported itself as a completed upgrade"
    )
    assert len(text.splitlines()) <= 2, f"not short: {text!r}"


def test_a_run_that_installed_nothing_is_never_called_a_failure(
    tmp_path: Path,
) -> None:
    """⭐ The control group against the repair that looks obvious.

    Forwarding the result's `upgraded` into the old boolean sends `⚠️ hyprial
    升级失败` here. Since most timer runs install nothing, that is a warning
    every day about a machine that is fine -- and a warning that is always
    there gets ignored, which costs exactly the channel this module exists to
    keep trustworthy.
    """

    _, recorder = _notify(tmp_path, ok=True, action=UPGRADE_ALREADY_CURRENT)
    text = recorder.sent[0]
    assert "⚠️" not in text and "失败" not in text, (
        f"a healthy no-op was reported as a failure: {text!r}"
    )


def test_a_declined_downgrade_is_reported_as_itself(tmp_path: Path) -> None:
    """⭐ The guard from #323 finally has somewhere to be said.

    That change added the refusal with the complaint that "a guard leaving no
    trace is indistinguishable from a rollback that never happened". This is
    the trace. It borrows neither neighbour's words: `✅` would hide a real
    event behind a healthy tick, and `失败` would send someone to repair a
    machine that is working exactly as intended.
    """

    outcome, recorder = _notify(
        tmp_path, ok=True, action=UPGRADE_DECLINED_DOWNGRADE
    )
    assert outcome.delivered is True
    text = recorder.sent[0]
    assert "降级" in text
    assert "✅" not in text, "a refused rollback is not a healthy tick"
    assert "升级失败" not in text, "nothing failed; the guard did its job"
    assert "dev (0.4.3)" in text


# ---------------------------------------------------------------------------
# ⭐ "Still starting" is not "failed".
#
# `_launch_daemon_process` returned ok/started/"was left running" when the daemon
# was alive and had not reported ready inside the budget. `hyprial init` treated that
# as a warning and succeeded; the upgrade path called it DAEMON_START_TIMEOUT and
# declared the upgrade dead. Measured on 2026-08-31: a six-adapter machine took
# 161s against a 90s budget, first try. The daemon came up.
#
# ⚠️ That verdict mattered more once the owner alert started delivering, because
# the first message this channel ever sent would have been a false alarm — aimed
# at the machines with the most adapters, whose owners most need to trust it.
#
# 2026-09-02, the readiness split removed the producer: restore runs on the
# daemon's own thread and no longer sits inside the readiness wait, so a
# launch timeout can only mean the daemon never served -- a real failure.
# The UNCONFIRMED vocabulary below stays until #183 lands or is dropped;
# these tests now pin the message, not a live path to it.
# ---------------------------------------------------------------------------


def test_a_daemon_that_is_still_starting_is_not_reported_as_a_failure(
    tmp_path: Path,
) -> None:
    outcome, recorder = _notify(tmp_path, ok=True, action=UPGRADE_UNCONFIRMED)
    assert outcome.delivered is True
    text = recorder.sent[0]
    assert "失败" not in text, f"a still-starting daemon was called a failure: {text!r}"
    assert "尚未确认" in text and "hyprial daemon status" in text


def test_the_unconfirmed_message_does_not_send_anyone_at_a_command_that_hangs(
    tmp_path: Path,
) -> None:
    """⭐ Measured, not inferred — and the first draft got this backwards.

    The socket is published before adapter restore and `hyprial daemon status` never reads
    `daemon.json`, so it *looks* like IPC answers throughout. Measured
    2026-08-31 19:43, it does not: `ps` failed twice during restore and
    succeeded at 19:44:25 when the daemon reported ready. Telling someone to
    run it "right now" hands them a timeout, and a timeout reads exactly like
    "my daemon is dead" — the conclusion this message exists to prevent.
    """

    _, recorder = _notify(tmp_path, ok=True, action=UPGRADE_UNCONFIRMED)
    text = recorder.sent[0]
    assert "现在" not in text, (
        f"the message tells the owner to check immediately, and during restore "
        f"that call times out: {text!r}"
    )
    assert "不代表它死了" in text, (
        "the message must say that an unanswered `hyprial daemon status` is not death — that "
        "is the inference the owner will otherwise draw"
    )


def test_the_unconfirmed_message_never_promises_that_recovery_completes(
    tmp_path: Path,
) -> None:
    """⭐ "Recovery complete" is a state that may never arrive.

    Restore is best effort: an adapter can stay down indefinitely while the
    daemon serves normally — `hyprial-kanban` on this machine has sat
    `quarantined` with `processRunning=False` for a long time while `hyprial daemon status`
    answered fine.

    ⛔ The harm in the earlier wording ("answers ⇒ recovery complete") was not
    imprecision. It told the reader to stop looking at exactly the moment
    something might still be wrong — and that quarantined adapter was found
    only because someone kept looking after `ps` began answering.
    """

    _, recorder = _notify(tmp_path, ok=True, action=UPGRADE_UNCONFIRMED)
    text = recorder.sent[0]
    assert "恢复完成" not in text, (
        f"the message promises a terminal state that may never arrive: {text!r}"
    )
    assert "best effort" in text and "adapter" in text, (
        "the owner is never told that not every adapter comes back — so a "
        "partial restore reads as breakage"
    )


def test_the_unconfirmed_message_gives_a_condition_for_calling_it_a_fault(
    tmp_path: Path,
) -> None:
    """"Wait a while" with no end is how a real fault gets waited out forever."""

    from hyprial.daemon.impl.autoupdate.alert import _UNCONFIRMED_ESCALATE_AFTER

    _, recorder = _notify(tmp_path, ok=True, action=UPGRADE_UNCONFIRMED)
    text = recorder.sent[0]
    assert _UNCONFIRMED_ESCALATE_AFTER in text and "那才是故障" in text, (
        f"no threshold at which the owner should treat this as broken: {text!r}"
    )


def test_the_escalation_threshold_is_marked_as_uncalibrated() -> None:
    """⭐ The number is a guess, and the message presents it as a boundary.

    A reader takes "after N minutes it is a fault" for something we measured.
    We measured one restore, of 161s. This pins the admission next to the
    constant so the next person cannot mistake it for a finding — the same
    reason the still-starting branch carries its own "not implemented yet"
    paragraph.
    """

    source = (
        ROOT / "src" / "hyprial" / "daemon" / "impl" / "autoupdate" / "alert.py"
    ).read_text("utf-8")
    declaration = source.index("_UNCONFIRMED_ESCALATE_AFTER =")
    preamble = source[:declaration]
    marker = preamble.rindex("# ⛔⛔ A PLACEHOLDER")
    assert "161s" in preamble[marker:], (
        "the only real measurement is not cited beside the guess"
    )
    assert "Calibrate" in preamble[marker:], (
        "the threshold is no longer marked as needing calibration; the next "
        "reader will take a number we invented for one we measured"
    )


def test_an_unconfirmed_restart_is_not_reported_as_success_either(
    tmp_path: Path,
) -> None:
    """⭐ The other half, and the reason the third state has to exist.

    Simply not raising would have let `_action_for` fall through to "did
    anything install?" — which is true here — and send `✅ 升级完成` while
    nothing had confirmed the daemon was ready. Trading a false alarm for a
    false all-clear is not an improvement; both state something unchecked.
    """

    _, recorder = _notify(tmp_path, ok=True, action=UPGRADE_UNCONFIRMED)
    text = recorder.sent[0]
    assert "✅" not in text and "升级完成" not in text, (
        f"an unconfirmed restart was reported as a completed upgrade: {text!r}"
    )


def test_a_restart_that_never_serves_is_a_restart_failure() -> None:
    """The serving-boundary timeout IS the failure, asserted on the source.

    This test replaces its own opposite: the still-starting branch it used to
    pin existed because restore sat inside the readiness budget, so a healthy
    but slow fleet measured as "never became ready" (161s against 90s,
    2026-08-31).  With restore on the daemon's own thread, the wait covers
    only bind + accept + ping -- exhausting it means the new daemon never
    came up, and the restart-failed path (marker + owner alert) is the honest
    report.  The behavioural half lives in test_cli_init.py, which drives a
    timeout through `_launch_daemon_process`; what is pinned here is that the
    third verdict and its fleet-scaled budget do not come back.
    """

    source = (
        Path(pending_restart.__file__)
    ).read_text("utf-8")
    assert "stillStarting" not in source, (
        "the still-starting verdict is back; a launch timeout is a startup "
        "failure now, because restore no longer sits inside the readiness wait"
    )
    assert "ready_timeout=90.0" not in source, (
        "the restart's wait is a fleet-scaled budget again; it must be the "
        "small serving-boundary budget"
    )
    assert "ready_timeout=DAEMON_START_HARD_CAP_SECONDS" in source, (
        "the upgrade restart no longer observes a live child through the "
        "named absolute startup bound"
    )


def test_the_reporter_prefers_unconfirmed_over_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real path, not source order: a still-restoring daemon reaches
    UPGRADE_UNCONFIRMED.

    The previous body asserted the *order of return statements* in
    ``_action_for`` (``UPGRADE_UNCONFIRMED`` before ``UPGRADE_INSTALLED``)
    and stayed green while the branch was unreachable on intg -- the write
    point for ``confirmed: False`` had been deleted with the 90s wall clock
    (F2/U7), so nothing could ever produce the state the chooser prefers.
    Order pinned on dead code proves nothing about reachability.

    This drives the actual upgrade flow with only the daemon boundary
    stubbed: launch answers the typed result, ping answers the daemon's real
    restoring shape, and the owner alert runs its whole real path (binding
    lookup, gateway resolution, credential read) with only the Lark SDK
    gateway substituted by a recorder. Restore still in progress
    (``phase=restoring``) must reach the owner as UPGRADE_UNCONFIRMED -- the
    message that says "尚未确认完成 adapter 恢复" -- and never as the
    "✅ 升级完成" all-clear nobody verified.
    """

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    # Guard 3's positive shape: this process sits inside the tool directory
    # uv would write, so the upgrade under test reaches its (faked) install.
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.delenv("HYPRIAL_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CONFIG", raising=False)
    home, state = _home(tmp_path, channel="allen-squire")

    before = updates.Installation(
        version="0.4.0.dev1",
        commit="a" * 40,
        requested_revision="v0.4.0.dev1",
        url="ssh://git@example.test/hyprial.git",
    )
    after = updates.Installation(
        version="0.4.0.dev2",
        commit="d" * 40,
        requested_revision="v0.4.0.dev2",
        url="ssh://git@example.test/hyprial.git",
    )
    installations = [before, after]
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: installations.pop(0) if installations else after,
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_args, **_kwargs: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2"
        ),
    )
    monkeypatch.setattr(_cli_get_services(), "_prime_post_install_restart_imports", lambda: None)
    monkeypatch.setattr(
        _cli_subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "installed\n", ""),
    )

    # The daemon's real ping shape mid-restore (application.py's ping
    # handler): flat, `phase` carries the restore thread's state. Not a
    # hand-invented form -- this is the contract the daemon publishes.
    restoring_ping = {
        "running": True,
        "pid": 4202,
        "epoch": "new",
        "version": "0.4.0.dev2",
        "phase": "restoring",
        "restorePending": True,
    }

    lifecycle = {"stopped": False}


    def daemon_request(
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, object]:
        del params, restore_wait
        if method == "ps":
            return {"daemon": {"running": True, "pid": 4101, "epoch": "old"}}
        if method == "shutdown":
            lifecycle["stopped"] = True
            return {"stopping": True, "pid": 4101}
        if method == "ping":
            # Card b9872e94: the pre-restart snapshot asks ping, so answer by
            # phase -- the old generation until shutdown, the fresh one after.
            if not lifecycle["stopped"]:
                return {
                    "running": True,
                    "pid": 4101,
                    "epoch": "old",
                    "version": "0.4.0.dev1",
                    "phase": "reconciled",
                }
            return dict(restoring_ping)
        raise AssertionError(f"unexpected daemon request: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    # The fake old daemon exits once asked to; never probe the real pid 4101
    # (a live unrelated process there costs the full stop budget -- #1149).
    monkeypatch.setattr(
        _cli_get_services(), "_stopped_process", lambda _pid, _identity: lifecycle["stopped"]
    )
    def restart_daemon(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        lifecycle["stopped"] = True
        return DaemonLaunchResult.launched(restoring_ping)

    monkeypatch.setattr(pending_restart, "restart_daemon", restart_daemon)

    recorder = _Recorder()

    class _RecordingGateway:
        @classmethod
        def from_credentials(cls, app_id: str, secret: str, **_kwargs: object):
            del app_id, secret
            return recorder

    monkeypatch.setattr(lark_sdk, "LarkSdkGateway", _RecordingGateway)

    result = _cli_get_services()._perform_upgrade_and_report(force=False)

    restart = result["restart"]
    assert restart.get("confirmed") is False, (
        "a daemon still mid-restore must be recorded as unconfirmed -- an "
        "absent key is exactly the dead-reader regression"
    )
    assert recorder.sent, "the owner must hear about the unconfirmed restart"
    first = recorder.sent[0]
    assert "尚未确认完成 adapter 恢复" in first, (
        "a still-restoring daemon reported as something other than "
        "UPGRADE_UNCONFIRMED -- the false all-clear is what this branch exists "
        "to prevent"
    )
    assert "✅ 升级完成" not in first


def test_the_call_site_reads_what_happened_instead_of_asserting_it() -> None:
    """⚠️ Asserted on the source, because the defect was a literal.

    The old call site passed `upgraded=True` on the whole success path. Every
    behavioural test above would pass again if someone reintroduced a constant
    there, because they all call `notify_upgrade_outcome` directly and never
    exercise the line that decides which state to pass.
    """

    source = (
        Path(upgrade_commands.__file__)
    ).read_text("utf-8")
    tree = ast.parse(source)
    chooser = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_action_for"
    )
    read_keys = {
        node.args[0].value
        for node in ast.walk(chooser)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert {"upgraded", "declinedDowngrade"} <= read_keys, (
        "the state is no longer read off the upgrade result; a constant here "
        "is how the no-op path came to announce a completed upgrade"
    )


# ---------------------------------------------------------------------------
# The self-check itself.
# ---------------------------------------------------------------------------


def test_the_self_check_runs_the_installed_binary_not_this_process(
    tmp_path: Path,
) -> None:
    """⚠️ Checking `import hyprial` from in here would check the wrong artefact.

    `autoupdate` deliberately preloads the pre-upgrade modules so the restart
    survives the distribution being replaced. So this process holds the OLD
    code; asking it whether hyprial imports would pass however broken the new build
    is. The check has to run the binary on disk.
    """

    seen: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"ok": True, "packageVersion": "0.4.3"}), ""
        )

    result = run_self_check(executable=tmp_path / "hyprial", runner=runner)
    assert result.ok is True
    assert seen == [[str(tmp_path / "hyprial"), "version", "--json"]]


def test_a_build_that_cannot_start_fails_the_self_check() -> None:
    """The 15:17 shape: installed, and dead on import."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "ImportError: cannot import name 'updates' from 'hyprial'"
        )

    result = run_self_check(executable=Path("/nonexistent/hyprial"), runner=runner)
    assert result.ok is False
    assert "cannot import name" in result.detail


def test_a_self_check_that_cannot_run_is_not_a_pass() -> None:
    """"Could not check" must not read as "checked and fine"."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise OSError("no such file")

    result = run_self_check(executable=Path("/nonexistent/hyprial"), runner=runner)
    assert result.ok is False
    assert "could not run" in result.detail


# ---------------------------------------------------------------------------
# ⭐ Control group ④: the report must never change the upgrade's outcome.
# ---------------------------------------------------------------------------


def test_the_report_cannot_turn_a_completed_upgrade_into_a_failure() -> None:
    """"We could not verify" and "it did not work" are different facts.

    Conflating them sends someone to re-install something that installed fine.
    Checked structurally: the wrapper re-raises what `_perform_upgrade` raised
    and swallows anything the reporting itself throws -- so no failure can
    originate here.
    """

    source = (
        Path(upgrade_commands.__file__)
    ).read_text("utf-8")
    tree = ast.parse(source)
    wrapper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_perform_upgrade_and_report"
    )
    raises = [n for n in ast.walk(wrapper) if isinstance(n, ast.Raise)]
    assert raises, "the wrapper must re-raise the upgrade's own error"
    assert all(n.exc is None for n in raises), (
        "the wrapper raises an error of its own; reporting must not be able to "
        "fail an upgrade that completed"
    )
    handlers = [n for n in ast.walk(wrapper) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) >= 2, (
        "both the failure and the success report must be individually guarded"
    )


def test_importing_the_cli_does_not_drag_in_the_daemon_package() -> None:
    """⭐ A one-line import cost every `hyprial` invocation 1.2 seconds.

    `cli` imports this alert module at module scope (pinned by the test below),
    and URI parsing now comes from the dependency-free `hyprial.uri` leaf rather
    than executing the eager `hyprial.daemon` package. The old import path took
    `import hyprial.cli` from 63ms to 1266ms -- paid by every CLI call, including
    `daemon stop`, whose IPC budget is 2 seconds. Nothing raised; the cost is
    the whole of the symptom.

    ⚠️ A full run at that commit also failed `test_e2e009_controlled` on a
    `daemon stop` IPC timeout. Whether this import caused it is **open**, and
    the check that looked like it settled the question did not:

        the failure appeared in a 24-minute full run -- a saturated machine
        the mutation re-ran that file alone -- an idle one

    The IPC budget is a fixed 2s. Idle, both 56ms and 1266ms clear it by a mile,
    so "green with and without the import" could not have come out any other
    way. ⛔ Zero discriminating power: it did not exonerate the import, it only
    showed that an idle machine has room to spare. Under load that 1.2s is
    taken out of an already-squeezed budget -- and even idle, the with-import
    run was 13% slower (45.8s vs 40.5s).

    ⇒ Established: "no causal link **under isolation**". Not established: "no
    causal link". The honest resolution is to run both directions under load.

    ⇒ The measurement below stands on its own: a 20x startup regression on
    every invocation is worth refusing whether or not it broke that test.
    Asserted as a structural fact rather than a timing budget -- a budget would
    be flaky, and the thing that went wrong is not "slow", it is "the daemon
    package is now on the startup path".
    """

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hyprial.cli, sys; print('hyprial.daemon' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        cwd=ROOT,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", (
        "importing hyprial.cli now executes the hyprial.daemon package, which adds "
        "~1.2s to every hyprial invocation; import it lazily and prime it in "
        "_prime_post_install_restart_imports instead"
    )


def test_the_alert_primes_its_lazy_import_before_the_install_boundary() -> None:
    """The other half of the lazy import -- and it is not optional.

    The alert runs after `uv tool install --force` has replaced the
    distribution, where a first import resolves against a path that no longer
    exists. Making the identity import lazy without naming it here would move
    the cost off startup and onto the one code path that cannot afford to fail.
    """

    tree = ast.parse(
        (
            ROOT / "src/hyprial/shell/impl/upgrade_bootstrap.py"
        ).read_text("utf-8")
    )
    primer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "prime_post_install_imports"
    )
    primed = {
        node.value
        for node in ast.walk(primer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    primed |= {
        alias.name
        for node in ast.walk(primer)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "parse_channel_uri" in primed, (
        "the owner-alert channel lookup imports the channel-URI parser "
        "lazily, so it must be loaded before the install boundary; otherwise "
        "the alert fails exactly on the upgrade that needed to report "
        "something"
    )


def test_the_alert_module_is_imported_before_the_upgrade_replaces_the_files() -> None:
    """⚠️ Where the module lives does not matter; when it is imported does.

    The upgrade replaces the installed distribution underneath this process, so
    anything imported *after* that point may not be there. The alert survives
    because `cli` imports it at module scope, long before. Moving that import
    inside a function would keep every test above green and make the alert
    vanish exactly when it is needed -- so the import site is pinned here.
    """

    tree = ast.parse(
        (
            ROOT / "src/hyprial/shell/impl/upgrade_bootstrap.py"
        ).read_text("utf-8")
    )
    primer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "prime_post_install_imports"
    )
    primed = {
        alias.name
        for node in ast.walk(primer)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert (
        "run_self_check" in primed or "notify_upgrade_outcome" in primed
    ), (
        "the alert is no longer primed before the install boundary; after "
        "`uv tool install` swaps the distribution a lazy import can fail, and "
        "it would fail precisely on the upgrade that needed to report "
        "something"
    )
