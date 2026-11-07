"""An alert nobody has seen fire is indistinguishable from one that cannot.

These cover the alert module and, structurally, the one property that cannot be
checked from inside it: that it is reached **only** on failure. A test suite
that only proves "it fires" leaves "it fires on everything" wide open, and the
two are the same observation from any single successful run.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.autoupdate.alert import (
    MARKER_FILENAME,
    AlertOutcome,
    RestartProcessObservation,
    RestartProcessState,
    StartFailureRecord,
    alert_text,
    latest_start_failure,
    notify_upgrade_failure,
    record_alert_outcome,
    restart_failure_detail,
    start_failure_line,
    write_failure_marker,
)

ROOT = Path(__file__).resolve().parents[3]


class _Recorder:
    """Stands in for the Lark gateway. Records; never reaches the network."""

    def __init__(self, message_id: str = "om_fake_1", fail: Exception | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self._message_id = message_id
        self._fail = fail

    def send_owner_dm(self, open_id: str, text: str, *, idempotency_key: str) -> str:
        self.calls.append((open_id, text, idempotency_key))
        if self._fail is not None:
            raise self._fail
        return self._message_id


def _home(tmp_path: Path, *, owner: bool = True) -> tuple[Path, Path]:
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
                        "name": "hq-feishu",
                        "appId": "cli_test",
                        "credentialRef": "lark-hq-feishu",
                        "routes": [
                            {
                                "name": "r1",
                                "type": "direct",
                                "nativeId": "ou_allen",
                            }
                        ],
                    }
                ],
            }
        ),
        "utf-8",
    )
    (home / "secrets" / "lark-hq-feishu.json").write_text(
        json.dumps({"appSecret": "s3cret"}), "utf-8"
    )
    profile: dict[str, object] = {
        "owner": "allen",
        "ownerKey": "allen",
        "loginName": "aaron@example",
        "agents": [],
        "preferredReceiver": {"machine": "m", "machineKey": "m"},
        "notificationRules": [],
    }
    if owner:
        profile["ownerOpenId"] = {
            "channel": "hq-feishu",
            "openId": "ou_allen",
            "boundAt": "2026-08-31T00:00:00Z",
        }
    (state / "users.json").write_text(
        json.dumps({"version": 1, "users": [profile]}), "utf-8"
    )
    return home, state


def test_the_owner_gets_a_message_and_its_id_is_kept(tmp_path: Path) -> None:
    """Delivered means: a real id came back, and we wrote it down.

    The id matters more than the boolean: it is the only handle anyone has for
    checking off-path that the message actually arrived. Without it, "we called
    send" is the whole of the evidence.
    """

    home, state = _home(tmp_path)
    recorder = _Recorder()
    outcome = notify_upgrade_failure(
        hyprial_home=home,
        state_dir=state,
        host="box-1",
        summary="升级完成但重启失败",
        detail="daemon restart failed: boom",
        idempotency_key="k1",
        sender_factory=lambda app_id, secret: recorder,
    )
    assert outcome.delivered is True
    assert outcome.message_id == "om_fake_1"
    assert outcome.open_id == "ou_allen"
    assert len(recorder.calls) == 1
    open_id, text, key = recorder.calls[0]
    assert open_id == "ou_allen"
    assert key == "k1"
    assert "box-1" in text and "daemon restart failed: boom" in text
    assert "没有在跑的 daemon" not in text


def test_a_send_that_raises_is_reported_not_propagated(tmp_path: Path) -> None:
    """The alert must never replace the failure it is reporting.

    If this raised, `UPGRADE_RESTART_FAILED` would be swallowed and the caller
    would be told about a Lark problem instead of a dead daemon -- strictly the
    worse of the two messages to lose.
    """

    home, state = _home(tmp_path)
    recorder = _Recorder(fail=RuntimeError("feishu unreachable"))
    outcome = notify_upgrade_failure(
        hyprial_home=home,
        state_dir=state,
        host="box-1",
        summary="s",
        detail="d",
        idempotency_key="k",
        sender_factory=lambda app_id, secret: recorder,
    )
    assert outcome.attempted is True
    assert outcome.delivered is False
    assert "feishu unreachable" in (outcome.reason or "")


def test_no_owner_bound_is_reported_as_its_own_thing(tmp_path: Path) -> None:
    """"Nobody to tell" and "telling failed" need different fixes.

    Collapsing them into `delivered=False` would send whoever reads it looking
    for a network fault that is not there.
    """

    home, state = _home(tmp_path, owner=False)
    outcome = notify_upgrade_failure(
        hyprial_home=home,
        state_dir=state,
        host="box-1",
        summary="s",
        detail="d",
        idempotency_key="k",
        sender_factory=lambda app_id, secret: _Recorder(),
    )
    assert outcome.attempted is False
    assert outcome.delivered is False
    assert "no owner" in (outcome.reason or "")


def test_the_marker_holds_the_failure_before_any_send_happens(
    tmp_path: Path,
) -> None:
    """The half that cannot fail for network reasons.

    Written first, on purpose: the send can fail with no daemon left to notice.
    The marker's `alert` field is null until the send has been attempted, so a
    process that dies in between leaves a record that says so.
    """

    _, state = _home(tmp_path)
    marker = write_failure_marker(
        state_dir=state, host="box-1", summary="s", detail="d"
    )
    assert marker.name == MARKER_FILENAME
    payload = json.loads(marker.read_text("utf-8"))
    assert payload["summary"] == "s"
    assert payload["host"] == "box-1"
    assert payload["alert"] is None

    record_alert_outcome(marker, AlertOutcome(attempted=True, delivered=True,
                                              message_id="om_x"))
    payload = json.loads(marker.read_text("utf-8"))
    assert payload["alert"]["delivered"] is True
    assert payload["alert"]["messageId"] == "om_x"


def test_a_failed_write_back_says_so_instead_of_going_quiet(
    tmp_path: Path, capsys
) -> None:
    """`alert: null` must not have two silent causes.

    Without this, a null in the marker means either "the process died mid-send"
    or "the send finished and writing it back failed" -- and the reader would go
    hunting for a crash that never happened. The stderr line is what separates
    them: null with no line is a death, null with a line is a failed write-back.

    ⚠️ It still must not raise: the marker already carries the failure, and the
    upgrade error is the message that matters.
    """

    record_alert_outcome(
        tmp_path / "does-not-exist.json",
        AlertOutcome(attempted=True, delivered=False, reason="feishu down"),
    )
    err = capsys.readouterr().err
    assert "could not record the alert outcome" in err
    # The outcome itself has to survive, not just the fact that recording failed
    # -- otherwise the send result is lost either way.
    assert "feishu down" in err


def test_alert_text_does_not_invent_a_process_state() -> None:
    text = alert_text(summary="升级完成但重启失败", detail="boom", host="box-9")
    assert "box-9" in text
    assert "boom" in text
    assert "没有在跑的 daemon" not in text


_PRODUCTION_START_FAILED = {
    "ts": "2026-09-13T07:17:36.765Z",
    "level": "warn",
    "component": "daemon",
    "event": "daemon.start.failed",
    "phase": "actor-runtime",
    "errorType": "DomainCommandError",
    "error": "agent …ciia-dipc already has a live connector on pi …",
}


def test_latest_start_failure_is_bounded_to_this_restart(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.jsonl"
    old = {
        **_PRODUCTION_START_FAILED,
        "ts": "2026-09-13T07:16:00.000Z",
        "phase": "ipc-server",
        "errorType": "OldFailure",
        "error": "stale failure",
    }
    log_path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (old, _PRODUCTION_START_FAILED)
        )
        + "\n",
        "utf-8",
    )

    failure = latest_start_failure(
        log_path, started_at=datetime(2026, 9, 13, 7, 17, 26, tzinfo=UTC)
    )

    assert failure is not None
    assert failure.phase == "actor-runtime"
    assert failure.error_type == "DomainCommandError"
    assert failure.error == "agent …ciia-dipc already has a live connector on pi …"


def test_an_old_start_failure_is_not_reported_as_this_restart(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.jsonl"
    log_path.write_text(
        json.dumps(_PRODUCTION_START_FAILED, ensure_ascii=False) + "\n", "utf-8"
    )

    failure = latest_start_failure(
        log_path, started_at=datetime(2026, 9, 13, 7, 18, tzinfo=UTC)
    )

    assert failure is None


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            RestartProcessObservation(44710, RestartProcessState.RUNNING),
            "pid 44710 仍在(可能在失败后的 shutdown 中)",
        ),
        (
            RestartProcessObservation(44710, RestartProcessState.EXITED),
            "pid 44710 已退出",
        ),
        (
            RestartProcessObservation(44710, RestartProcessState.UNKNOWN),
            "pid 44710 观测失败",
        ),
    ],
)
def test_restart_alert_reports_each_observed_process_state_without_contradiction(
    observation: RestartProcessObservation, expected: str
) -> None:
    detail = restart_failure_detail(
        error="daemon did not become ready within 15s",
        start_failure=None,
        process=observation,
    )
    text = alert_text(summary="升级完成但重启失败", detail=detail, host="box-9")

    assert expected in text
    assert "日志里还没有 start.failed 记录" in text
    assert not ("left running" in text and "没有在跑的 daemon" in text)


def test_production_start_failure_fields_are_present_in_the_complete_alert(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "daemon.jsonl"
    log_path.write_text(
        json.dumps(_PRODUCTION_START_FAILED, ensure_ascii=False) + "\n", "utf-8"
    )
    failure = latest_start_failure(
        log_path, started_at=datetime(2026, 9, 13, 7, 17, 26, tzinfo=UTC)
    )
    assert failure is not None
    detail = restart_failure_detail(
        error=(
            "daemon did not become ready within 15s; the daemon process "
            "(pid 44710) was still running when the wait ended"
        ),
        start_failure=failure,
        process=RestartProcessObservation(44710, RestartProcessState.RUNNING),
    )

    text = alert_text(
        summary="升级到 dev (0.4.2.dev1) 已完成,但 daemon 重启失败",
        detail=detail,
        host="node-hq-01.example-tailnet.ts.net",
    )

    assert text == (
        "⚠️ hyprial 升级后 daemon 未能重启\n"
        "主机: node-hq-01.example-tailnet.ts.net\n"
        "升级到 dev (0.4.2.dev1) 已完成,但 daemon 重启失败\n\n"
        "daemon restart failed: daemon did not become ready within 15s; "
        "the daemon process (pid 44710) was still running when the wait ended\n"
        "本次 daemon.start.failed: phase=actor-runtime "
        "errorType=DomainCommandError "
        "error=agent …ciia-dipc already has a live connector on pi …\n"
        "进程状态: pid 44710 仍在(可能在失败后的 shutdown 中)"
    )


def test_the_marker_carries_the_same_start_failure_line_the_alert_does(
    tmp_path: Path,
) -> None:
    """Marker and alert must not tell the same event in different amounts.

    Before this the alert named the cause and the marker did not -- and the
    marker is the half that survives on an unattended machine, so the one
    record a reader finds was the one without the cause. Both are rendered by
    `start_failure_line`, so the shared text is asserted, not a copy of it.
    """

    _, state = _home(tmp_path)
    failure = StartFailureRecord(
        timestamp=datetime(2026, 9, 13, 7, 17, 36, 765000, tzinfo=UTC),
        phase="actor-runtime",
        error_type="DomainCommandError",
        error="no agent named 'ciia-dipc' on this machine",
    )
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail=(
            "daemon restart failed: daemon did not become ready within 15s\n"
            f"{start_failure_line(failure)}"
        ),
        start_failure=failure,
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert payload["detail"] == (
        "daemon restart failed: daemon did not become ready within 15s\n"
        "本次 daemon.start.failed: phase=actor-runtime "
        "errorType=DomainCommandError "
        "error=no agent named 'ciia-dipc' on this machine"
    )
    assert payload["startFailure"] == {
        "phase": "actor-runtime",
        "errorType": "DomainCommandError",
        "error": "no agent named 'ciia-dipc' on this machine",
    }


def test_a_marker_with_no_start_failure_states_it_instead_of_an_empty_object(
    tmp_path: Path,
) -> None:
    """`{}` would be present and unreadable; the sentence is the honest form.

    Both halves of the rule are asserted: the sentence is in `detail`, and the
    structured field is a written-out `null` -- not `{}`, and not omitted, so
    that "we looked and found nothing" cannot be read as "this field never
    existed".
    """

    _, state = _home(tmp_path)
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail=(
            "daemon restart failed: daemon did not become ready within 15s\n"
            f"{start_failure_line(None)}"
        ),
        start_failure=None,
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert "本次启动: 日志里还没有 start.failed 记录" in payload["detail"]
    assert payload["startFailure"] is None
    assert payload["startFailure"] != {}


def test_a_marker_with_no_start_failure_dimension_omits_the_key(
    tmp_path: Path,
) -> None:
    """Omitted is not null: null asserts a search, omission does not.

    The survivor marker (old daemon torn down but the process still running) is
    not a start failure at all, so the question does not apply. Writing `null`
    there would read as "we looked for this launch's daemon.start.failed and
    found none" -- a claim about a search the survivor branch never runs -- and
    it would be the only place in the file where that null had no "no record"
    sentence beside it. The key is absent instead, and so is the sentence.

    This is also why the parameter's default is a sentinel rather than `None`:
    a caller who names nothing has not answered the question.
    """

    _, state = _home(tmp_path)
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail="daemon teardown completed but the old process did not exit",
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert "startFailure" not in payload
    assert "本次启动: 日志里还没有 start.failed 记录" not in payload["detail"]


def test_recording_the_alert_outcome_keeps_the_start_failure(
    tmp_path: Path,
) -> None:
    """The one in-repo reader rewrites the payload; the new field must survive.

    `record_alert_outcome` writes the same file back, and it is the only code
    that reads a marker by name. A reader that rebuilt the payload from known
    keys would drop `startFailure` silently -- the durable cause would vanish
    the moment the send finished. This pins that it round-trips.
    """

    _, state = _home(tmp_path)
    failure = StartFailureRecord(
        timestamp=datetime(2026, 9, 13, 7, 17, 36, 765000, tzinfo=UTC),
        phase="actor-runtime",
        error_type="DomainCommandError",
        error="boom",
    )
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail=f"daemon restart failed: x\n{start_failure_line(failure)}",
        start_failure=failure,
    )

    record_alert_outcome(
        marker, AlertOutcome(attempted=True, delivered=True, message_id="om_x")
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert payload["startFailure"] == {
        "phase": "actor-runtime",
        "errorType": "DomainCommandError",
        "error": "boom",
    }
    assert payload["alert"]["delivered"] is True


# ---------------------------------------------------------------------------
# ⭐ The control group. It cannot live inside the module, so it lives here.
# ---------------------------------------------------------------------------


def _native_upgrade_source() -> str:
    from hyprial.shell.impl.cli.commands.common import pending_restart as owner
    from hyprial.shell.impl.cli.commands.common.services import get_services
    if owner.get_services is not get_services:
        raise RuntimeError("Upgrade service getter is not the canonical native binding")
    source = Path(owner.__file__).read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "services"
                                               for target in node.targets):
            if not (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "get_services" and not node.value.args and not node.value.keywords):
                raise RuntimeError("Noncanonical lexical services assignment in the upgrade provider")
    return source


def _native_alert_call_name(call: ast.Call) -> str | None:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            and target.value.id == "services"
            and target.attr in {"notify_upgrade_failure", "write_failure_marker"}):
        return target.attr
    return None


def test_the_alert_is_reached_only_from_the_failure_handler() -> None:
    """Success must not alert -- and "it fired" cannot prove that by itself.

    An alert that fires on every upgrade would pass every test above. What
    distinguishes "fires on failure" from "fires on everything" is *where it is
    called from*, and that is checked structurally: every call site must be
    lexically inside an `except` handler.

    ⚠️ Structural, not behavioural: this proves the call cannot be reached from
    the success path, not that a real failure delivers a real message. The
    second half needs the drill -- deliberately break a restart, confirm the
    message arrives, and confirm a successful upgrade stays silent. Both
    observations, or neither counts.
    """

    tree = ast.parse(_native_upgrade_source())
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    guarded: set[int] = set()
    for handler in handlers:
        for node in ast.walk(handler):
            if isinstance(node, ast.Call):
                name = _native_alert_call_name(node)
                if name in {"notify_upgrade_failure", "write_failure_marker"}:
                    guarded.add(node.lineno)

    all_calls: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _native_alert_call_name(node)
            if name in {"notify_upgrade_failure", "write_failure_marker"}:
                all_calls.add(node.lineno)

    assert all_calls, "the alert is not wired in at all"
    assert all_calls == guarded, (
        f"alert calls outside an except handler at lines "
        f"{sorted(all_calls - guarded)} -- a successful upgrade would notify, "
        "and then 'it fired' would say nothing about whether it fired for the "
        "right reason"
    )


def test_the_marker_is_written_before_the_send_is_attempted() -> None:
    """Ordering, asserted on the source because it is an ordering claim.

    "Send, and write if it fails" loses the fact whenever the process dies
    mid-send -- which is a live possibility here, since the machine has just
    failed to bring a daemon up.
    """

    source = _native_upgrade_source()
    write_at = source.index("write_failure_marker(")
    notify_at = source.index("notify_upgrade_failure(\n")
    assert write_at < notify_at, (
        "the send is attempted before the durable marker is written"
    )
