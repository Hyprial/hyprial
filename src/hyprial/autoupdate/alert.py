"""Tell the owner when an upgrade left the machine without a daemon.

On 2026-08-31 an autoupdate installed cleanly, stopped the running daemon, and
never started a replacement. Production was down for 56 minutes, and **nothing
told anyone**. It was found by a person happening to read `hyprial autoupdate
status` afterwards.

⚠️ Why this does not use `deliver_alarm`
----------------------------------------
The daemon already has an alarm path -- `DaemonPortClient.deliver_alarm` ->
lifecycle actor -> Lark adapter. It is tested, and it is the wrong one here:
**every hop runs inside the daemon**, and the moment this alert exists for is
the moment there is no daemon. An alert built on it would be silent exactly
when it is needed, and green in every test, because tests have a live daemon.

So this sends from the `hyprial autoupdate run` subprocess itself, reading the
gateway credentials off disk. That subprocess is the one process guaranteed to
still be alive: it is what performed the upgrade.

⚠️ It costs one thing, stated plainly: a second process now reads the Lark app
secret. Previously only the daemon did. Same machine, same user, same file --
but it is a wider surface, and a wider surface is easy to miss in a diff.

Never raises
------------
Every failure here is captured and returned. An alert that cannot be delivered
must not replace, mask, or delay the upgrade error it is reporting -- the
original failure is the more important message, and it is already on its way to
the caller. The returned record is attached to that error, so the attempt (and
its outcome) lands in the durable autoupdate log either way.

The two control groups
----------------------
Both are required, and neither is sufficient alone:

  success does not alert   asserted at the call site, not here: this module is
                           only ever called from the failure branch. Without
                           that test, "it fired" cannot distinguish "it fires
                           on failure" from "it fires on everything".
  delivery is verified     `messageId` is returned and recorded so a human can
                           check the message actually arrived. Without it, "we
                           sent it" and "it was received" are the same
                           observation from in here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol

from ..persistent_config import PersistentConfigStore

ALERT_TITLE = "⚠️ hyprial 升级后 daemon 未能重启"

MARKER_FILENAME = "upgrade-restart-failed.json"

#: The migration-required marker (design §6b).  ⚠️ A DIFFERENT filename and
#: ``kind`` from ``MARKER_FILENAME`` on purpose: both markers are written as a
#: whole-file overwrite, so sharing a name would let a self-upgrade restart
#: failure and a pending app migration silently clobber each other's record.
MIGRATION_MARKER_FILENAME = "app-migration-required.json"

MIGRATION_ALERT_TITLE = "⚠️ hyprial 自升级完成;有 app 仍是 git 安装,需迁移到 release"

# ⭐ What the upgrade actually did. Named states rather than a bool, because a
# bool was forced to answer two questions at once ("did anything install?" and
# "did it succeed?") and got the common case wrong in both directions -- see
# `notify_upgrade_outcome`. Naming the states is what lets the declined
# downgrade be reported as itself instead of borrowing another state's words.
UPGRADE_INSTALLED = "installed"
UPGRADE_ALREADY_CURRENT = "already-current"
UPGRADE_DECLINED_DOWNGRADE = "declined-downgrade"
# ⭐ Installed, and the daemon has not yet confirmed it is ready. Neither
# success nor failure: reporting it as either one states something we do not
# know. Since card 3c116ad2 the restart path produces it again -- the launch
# proves only the serving boundary, so the flow reads ping's phase once and
# records unconfirmed whenever restore has not settled (phase != reconciled)
# -- and the follow-up poll closes the state from the same CLI process.
UPGRADE_UNCONFIRMED = "unconfirmed"
#: Installed by the timer and deliberately NOT restarted: the owner restarts
#: with ``hyprial autoupdate restart`` (Allen 2026-09-23: "改为用户手动回复重启
#: 才重启"; the reply path is a command he or an agent runs).
UPGRADE_AWAITING_RESTART = "awaiting-restart"
UPGRADE_FAILED = "failed"

# ⛔⛔ A PLACEHOLDER, NOT A MEASUREMENT. The only timing anyone has measured is
# a single restore of 161s on a six-adapter machine (2026-08-31). This number
# was picked to be comfortably larger than that and nothing more -- no machine
# has been observed taking anywhere near it, and none has been observed failing
# after it either.
#
# It is stated in the owner's message as the line between "still recovering"
# and "now it is a fault", so a reader will reasonably take it for something we
# know. Calibrate it against real restores before it earns that reading, and
# whoever does so should also decide whether a fixed number is right at all:
# restore time scales with adapter count, which this does not.
_UNCONFIRMED_ESCALATE_AFTER = "十分钟"


class _OwnerSender(Protocol):
    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str: ...


@dataclass(frozen=True)
class AlertOutcome:
    """What the attempt did. Every field is meant to be read by a human later."""

    attempted: bool
    delivered: bool
    reason: str | None = None
    message_id: str | None = None
    open_id: str | None = None

    def to_json(self) -> dict[str, object]:
        record: dict[str, object] = {
            "attempted": self.attempted,
            "delivered": self.delivered,
        }
        if self.reason is not None:
            record["reason"] = self.reason
        if self.message_id is not None:
            # ⭐ Kept so delivery can be confirmed off-path. "We called send"
            # is not evidence that anyone received anything.
            record["messageId"] = self.message_id
        if self.open_id is not None:
            record["openId"] = self.open_id
        return record


@dataclass(frozen=True)
class StartFailureRecord:
    """One structured startup failure from the daemon's durable log."""

    timestamp: datetime
    phase: str
    error_type: str
    error: str


class RestartProcessState(StrEnum):
    """The three honest outcomes of the PID-and-birth observation."""

    RUNNING = "running"
    EXITED = "exited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RestartProcessObservation:
    """Current state of the exact daemon process started by this restart."""

    pid: int | None
    state: RestartProcessState


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _start_failure_from_entry(entry: object) -> StartFailureRecord | None:
    if not isinstance(entry, dict) or entry.get("event") != "daemon.start.failed":
        return None
    timestamp = _timestamp(entry.get("ts"))
    if timestamp is None:
        return None

    def field(name: str) -> str:
        value = entry.get(name)
        return value if isinstance(value, str) and value else "unknown"

    return StartFailureRecord(
        timestamp=timestamp,
        phase=field("phase"),
        error_type=field("errorType"),
        error=field("error"),
    )


def latest_start_failure(
    log_path: Path, *, started_at: datetime
) -> StartFailureRecord | None:
    """Return the newest ``daemon.start.failed`` from this launch attempt.

    ``started_at`` is captured by the upgrade process immediately before it
    invokes the launcher.  That parent-side wall-clock reading precedes the
    child process and therefore excludes every prior daemon generation without
    requiring the failed daemon to have reached a correlation-id write point.
    Invalid or partial JSONL records do not become evidence.
    """

    latest: StartFailureRecord | None = None
    try:
        stream = Path(log_path).open("r", encoding="utf-8")
    except OSError:
        return None
    with stream:
        for line in stream:
            try:
                entry: Any = json.loads(line)
            except (UnicodeError, ValueError, RecursionError):
                continue
            candidate = _start_failure_from_entry(entry)
            if candidate is None or candidate.timestamp < started_at:
                continue
            if latest is None or candidate.timestamp >= latest.timestamp:
                latest = candidate
    return latest


def start_failure_line(start_failure: StartFailureRecord | None) -> str:
    """Render this launch's startup-failure diagnosis, or say there is none.

    ⭐ One renderer, two readers: the owner alert (:func:`restart_failure_detail`)
    and the durable failure marker (:func:`write_failure_marker`). Before this
    the alert named the cause and the marker did not, so the two told the same
    event in two different amounts -- and the one that survived on disk was the
    one without the cause. Sharing the sentence is what keeps them from drifting
    apart again.

    ⛔ Absence is stated, never omitted: "we found no record" and "this line was
    left out" must not read the same. The latter is the defect this replaced.
    """

    if start_failure is None:
        return "本次启动: 日志里还没有 start.failed 记录"
    return (
        "本次 daemon.start.failed: "
        f"phase={start_failure.phase} "
        f"errorType={start_failure.error_type} "
        f"error={start_failure.error}"
    )


def restart_failure_detail(
    *,
    error: str,
    start_failure: StartFailureRecord | None,
    process: RestartProcessObservation,
) -> str:
    """Render the failure cause and current process state without inference."""

    failure_line = start_failure_line(start_failure)

    if process.state is RestartProcessState.RUNNING and process.pid is not None:
        process_line = (
            f"进程状态: pid {process.pid} 仍在(可能在失败后的 shutdown 中)"
        )
    elif process.state is RestartProcessState.EXITED and process.pid is not None:
        process_line = f"进程状态: pid {process.pid} 已退出"
    elif process.pid is not None:
        process_line = f"进程状态: pid {process.pid} 观测失败"
    else:
        process_line = "进程状态: 观测失败(启动错误没有提供 pid)"
    return f"daemon restart failed: {error}\n{failure_line}\n{process_line}"


def _gateway_for_channel(store: PersistentConfigStore, channel: str):
    """Find the gateway a binding names -- by URI or by bare name.

    ⚠️ These two spellings are the same channel, and the first version of this
    file only understood the second:

        users.json     "channel:h2oslabs:hyprial-hq.orkhon-bee.ts.net:allen-squire"
        channels.json  name = "allen-squire"

    So every lookup missed, every alert reported "which is not configured", and
    the alert never delivered anywhere. It stayed invisible because the failure
    path had never once run in production -- the bug and its concealment were
    the same fact.

    ⚠️ The name segment comes from `parse_channel_uri`, not from splitting on
    the last colon here. The first fix did split, and the address-parsing guard
    refused it -- correctly. A second reader of a grammar it does not own can
    drift from the writer, and when it drifts the symptom is a lookup that
    silently never matches: precisely the bug this function exists to fix, in a
    second copy.

    The shared reader is also stricter than the split was. It requires the
    `channel:`/`adapter:` prefix and four non-empty segments, so a malformed
    binding now falls through to the exact-match branch instead of yielding
    whatever followed the last colon -- which could have matched an unrelated
    gateway and sent the owner's alert somewhere else.

    ⚠️ The import remains inside the function because the upgrade path primes
    every lazy dependency before `uv tool install --force` swaps the backing
    distribution.  The parser now lives in the dependency-free ``hyprial.uri``
    leaf module, so loading it never executes the eager daemon package.
    """

    from ..uri import parse_channel_uri

    gateways = list(store.load().channels.gateways)
    parsed = parse_channel_uri(channel)
    name = parsed[2] if parsed is not None else channel
    for item in gateways:
        if item.name == channel or item.name == name:
            return item
    return None


def _owner_binding(store: PersistentConfigStore) -> tuple[str, str] | None:
    """Return (channel, openId) for the first owner that has one bound."""

    for profile in store.load().users.users:
        binding = profile.get("ownerOpenId")
        if isinstance(binding, dict):
            channel = binding.get("channel")
            open_id = binding.get("openId")
            if isinstance(channel, str) and isinstance(open_id, str):
                if channel and open_id:
                    return channel, open_id
    return None


def alert_text(*, summary: str, detail: str, host: str) -> str:
    """One message that says what broke and where, without inventing state.

    The restart-failure caller includes a PID-and-birth observation in
    ``detail``.  A generic footer cannot do that job: the process may still be
    shutting down, may have exited, or may be unobservable by send time.
    """

    return (
        f"{ALERT_TITLE}\n"
        f"主机: {host}\n"
        f"{summary}\n"
        f"\n"
        f"{detail}"
    )


class _StartFailureNotApplicable:
    """Sentinel: this marker has no start-failure dimension at all.

    ⭐ The field has **three** states, and collapsing any two of them is a lie
    about a search:

      * a ``StartFailureRecord`` -- this launch's ``daemon.start.failed`` was
        found in the log;
      * ``None``                  -- we looked and there is no such record
        (the restart-failure path);
      * this sentinel             -- the event is not a start failure, so the
        question does not apply (the survivor path).

    ⛔ Writing ``null`` for the third state asserts "we looked and found
    nothing" about a search nobody ran. Its key is omitted instead, and the
    omission is the honest record. This is why a bare default cannot be
    ``None``: a caller who names nothing has not answered the question, and
    the default must say so rather than answer it for them.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return "START_FAILURE_NOT_APPLICABLE"


START_FAILURE_NOT_APPLICABLE = _StartFailureNotApplicable()


def write_failure_marker(
    *,
    state_dir: Path,
    host: str,
    summary: str,
    detail: str,
    start_failure: (
        StartFailureRecord | None | _StartFailureNotApplicable
    ) = START_FAILURE_NOT_APPLICABLE,
) -> Path:
    """Put the fact on disk **before** trying to send anything.

    ⚠️ The send can fail silently -- Feishu unreachable, token expired, network
    down -- and at that moment there is no daemon left to observe that it
    failed. So the durable record must not depend on the send having worked.

    Ordering is the whole point: **write, then send**, never "send, and write if
    it fails". On 2026-08-31 the fact *was* on disk the whole time
    (`UPGRADE_RESTART_FAILED` sat in the autoupdate status for 56 minutes) --
    what was missing was the notification. These are two halves, not two
    options; this writes the half that cannot fail for network reasons.

    Raising here is acceptable and deliberate: if we cannot even write a local
    file, the caller should hear about it. The *send* is the part that must
    never raise, not this.
    """

    path = Path(state_dir) / MARKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "kind": "upgrade-restart-failed",
        "host": host,
        "summary": summary,
        "detail": detail,
        "recordedAtEpoch": int(time.time()),
        "pid": os.getpid(),
        # Filled in by `record_alert_outcome` once the send has been
        # attempted. ⚠️ Its absence has **two** causes, and on its own it does
        # not separate them: the process died mid-send, or the send finished
        # and folding the result back in here failed. `record_alert_outcome`
        # therefore prints the outcome to stderr before giving up, so the two
        # are told apart by whether that line exists -- a null with no stderr
        # line is a death, a null with one is a failed write-back.
        "alert": None,
    }
    # ⭐ The diagnosis, not only the symptom. `detail` carries the same sentence
    # `start_failure_line` renders; this is the machine-readable half for a
    # reader that should not have to parse prose. Before this field the marker
    # said only "daemon restart failed: <timeout>" and the cause
    # (`daemon.start.failed` phase/errorType/error) lived exclusively in
    # daemon.jsonl -- so whoever read the marker had to already know to look
    # somewhere else, which is exactly what an unattended failure cannot assume.
    #
    # ⚠️ `None` (passed explicitly) means "we looked and no start.failed record
    # was found for this restart", and it is written out rather than omitted so
    # that meaning is readable. `{}` would be a third, dishonest shape: present
    # and unreadable. What None means is spelled out by the same sentence that
    # lands in `detail` -- a marker that carries None and no such sentence
    # would be the silent omission this field exists to prevent.
    #
    # ⛔ The key is **omitted** when the caller did not pass `start_failure` at
    # all: that means the event has no start-failure dimension (the survivor
    # path), and writing `null` there would assert a search that never ran.
    if not isinstance(start_failure, _StartFailureNotApplicable):
        payload["startFailure"] = (
            None
            if start_failure is None
            else {
                "phase": start_failure.phase,
                "errorType": start_failure.error_type,
                "error": start_failure.error,
            }
        )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    return path


def record_alert_outcome(marker: Path, outcome: "AlertOutcome") -> None:
    """Fold the send result into the marker. Best effort, never raises.

    ⚠️ Swallowing the write-back failure silently would make `alert: null`
    ambiguous: it would mean either "the process died mid-send" or "the send
    completed and writing it here failed", and whoever read the marker would go
    looking for a crash that never happened. So the outcome goes to stderr
    before giving up. That is not a second durable store -- it is the cheapest
    way to make the two causes tell themselves apart.
    """

    try:
        payload = json.loads(marker.read_text("utf-8"))
        payload["alert"] = outcome.to_json()
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    except Exception as error:  # noqa: BLE001 -- the marker already holds the failure
        print(
            f"hyprial: could not record the alert outcome in {marker}: "
            f"{type(error).__name__}: {error}; "
            f"outcome={json.dumps(outcome.to_json(), ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )


def write_app_migration_marker(
    *,
    state_dir: Path,
    host: str,
    apps: list[str],
) -> Path:
    """Record the pending app migrations on disk **before** the owner DM (§6b).

    Same write-then-send discipline as ``write_failure_marker``: the send can
    fail with no daemon left to notice, so the durable half must not depend on
    it.  ⚠️ Its own file (``MIGRATION_MARKER_FILENAME``), never
    ``MARKER_FILENAME`` -- a shared whole-file overwrite would let the two
    unattended-upgrade markers erase each other.

    Raising here is deliberate: a local file we cannot even write is worth
    surfacing.  The *send* is the never-raising half (``_send_owner_message``).
    """

    path = Path(state_dir) / MIGRATION_MARKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "app-migration-required",
        "host": host,
        "apps": list(apps),
        "recordedAtEpoch": int(time.time()),
        "pid": os.getpid(),
        # Filled in by `record_alert_outcome` once the send is attempted; see
        # that function for why a null-with-no-stderr and a null-with-stderr
        # mean different things.
        "alert": None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    return path


def notify_app_migration_required(
    *,
    hyprial_home: Path,
    state_dir: Path,
    host: str,
    apps: list[str],
    idempotency_key: str,
    sender_factory: Callable[[str, str], _OwnerSender] | None = None,
) -> AlertOutcome:
    """DM the owner the list of apps to migrate and the per-app command (§6b).

    The remediation command is ``hyprial install <name>`` (ruling: #292 keeps
    ``hyprial upgrade`` for hyprial's own self-upgrade, and ``install`` on an
    already-installed app is the upgrade/migration entry).  Reuses the module's
    never-raises owner DM path so a failed migration notice can never mask the
    upgrade that just succeeded."""

    remediation = "\n".join(f"  · hyprial install {app}" for app in apps)
    summary = f"{MIGRATION_ALERT_TITLE} · {len(apps)} 个"
    detail = (
        f"主机: {host}\n"
        f"待迁移: {', '.join(apps)}\n"
        "逐条补救(install 已装即升级 = 迁移到 release):\n"
        f"{remediation}"
    )
    return _send_owner_message(
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        text=f"{summary}\n{detail}",
        idempotency_key=idempotency_key,
        sender_factory=sender_factory,
    )


def notify_upgrade_failure(
    *,
    hyprial_home: Path,
    state_dir: Path,
    host: str,
    summary: str,
    detail: str,
    idempotency_key: str,
    sender_factory: Callable[[str, str], _OwnerSender] | None = None,
) -> AlertOutcome:
    """The failure-only path, kept for the restart-failed branch."""

    return _send_owner_message(
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        text=alert_text(summary=summary, detail=detail, host=host),
        idempotency_key=idempotency_key,
        sender_factory=sender_factory,
    )


def notify_restore_followup(
    *,
    hyprial_home: Path,
    state_dir: Path,
    host: str,
    recovered: bool,
    pending_connectors: int | None,
    upgrade_detail: str,
    idempotency_key: str,
    sender_factory: Callable[[str, str], _OwnerSender] | None = None,
) -> AlertOutcome:
    """The follow-up the unconfirmed notice owed: settled, or still starting.

    The first notice (``notify_upgrade_outcome`` with UPGRADE_UNCONFIRMED)
    says "not yet confirmed"; card 3c116ad2 requires the same CLI process to
    close that state -- one message when ping reports ``reconciled`` (the
    daemon itself considers restore settled), one message when the
    fleet-derived poll budget runs out first.  Same channel, same
    never-raises contract as every other owner notice here: a follow-up
    that could fail the upgrade report would be worse than no follow-up.

    ``pending_connectors`` is what ps already reports (connectors not
    running); ``None`` when ps cannot answer mid-restore, and then the
    message says so without inventing a count.  No duration is promised in
    either wording -- the budget is derived per machine, not a constant the
    text could state.
    """

    if recovered:
        summary = "✅ hyprial 升级重启的 daemon 已恢复 —— restore 已落定(reconciled)"
        detail = f"主机: {host}\n升级: {upgrade_detail}"
    elif pending_connectors is not None:
        summary = f"⚠️ hyprial 重启后仍在启动:{pending_connectors} 个 connector 未回"
        detail = (
            f"主机: {host}\n"
            f"升级: {upgrade_detail}\n"
            "重启后等待已到按本机规模派生的上限,restore 仍未落定。\n"
            "⇒ 请运行 `hyprial ps` 查看 adapter 状态;升级已安装,不要重复执行升级。"
        )
    else:
        summary = "⚠️ hyprial 重启后仍在启动 —— restore 仍未落定"
        detail = (
            f"主机: {host}\n"
            f"升级: {upgrade_detail}\n"
            "重启后等待已到按本机规模派生的上限,restore 仍未落定。\n"
            "⇒ 请运行 `hyprial ps` 查看 adapter 状态;升级已安装,不要重复执行升级。"
        )
    return _send_owner_message(
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        text=f"{summary}\n{detail}",
        idempotency_key=idempotency_key,
        sender_factory=sender_factory,
    )


def notify_owner(
    *,
    hyprial_home: Path,
    state_dir: Path,
    text: str,
    idempotency_key: str,
    sender_factory: Callable[[str, str], _OwnerSender] | None = None,
) -> AlertOutcome:
    """The shared owner-DM entry point for alerts that are not upgrade news.

    This is the same route ``notify_upgrade_failure`` uses -- owner binding,
    channel-to-gateway resolution, Lark DM, never raises -- exposed as a
    public function so other subsystems (provider-auth relogin alerts being
    the first) reuse the path instead of growing a second copy of the
    routing.  Callers compose their own text; this module owns the delivery.
    """

    return _send_owner_message(
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        text=text,
        idempotency_key=idempotency_key,
        sender_factory=sender_factory,
    )


def _send_owner_message(
    *,
    hyprial_home: Path,
    state_dir: Path,
    text: str,
    idempotency_key: str,
    sender_factory: Callable[[str, str], _OwnerSender] | None = None,
) -> AlertOutcome:
    """Try to DM the owner. Returns what happened; never raises.

    `sender_factory` takes (app_id, app_secret) so tests can substitute a
    recorder without reaching the network. Production passes None and gets the
    real Lark gateway.
    """

    try:
        store = PersistentConfigStore(hyprial_home, state_dir)
        binding = _owner_binding(store)
        if binding is None:
            # Not an error: a machine with no owner bound has nobody to tell.
            # Said out loud rather than returning a bare False, because
            # "nobody to tell" and "sending failed" need different fixes.
            return AlertOutcome(
                attempted=False,
                delivered=False,
                reason="no owner openId is bound; nobody to notify",
            )
        channel, open_id = binding
        gateway = _gateway_for_channel(store, channel)
        if gateway is None:
            return AlertOutcome(
                attempted=False,
                delivered=False,
                reason=f"owner is bound to channel {channel!r}, which is not configured",
                open_id=open_id,
            )
        secret = store.lark_app_secret(gateway.credential_ref)
        if sender_factory is None:  # pragma: no cover - exercised in the drill
            from ..adapters.lark.sdk import LarkSdkGateway

            sender: _OwnerSender = LarkSdkGateway.from_credentials(
                gateway.app_id, secret, gateway_name=gateway.name
            )
        else:
            sender = sender_factory(gateway.app_id, secret)
        message_id = sender.send_owner_dm(
            open_id,
            text,
            idempotency_key=idempotency_key,
        )
        return AlertOutcome(
            attempted=True,
            delivered=True,
            message_id=message_id,
            open_id=open_id,
        )
    except Exception as error:  # noqa: BLE001 -- see the module docstring
        # Deliberately broad. Anything raised here would otherwise surface
        # instead of UPGRADE_RESTART_FAILED, and a swallowed upgrade error is
        # strictly worse than an undelivered alert about it.
        return AlertOutcome(
            attempted=True,
            delivered=False,
            reason=f"{type(error).__name__}: {error}",
        )


@dataclass(frozen=True)
class SelfCheckResult:
    """Did the build we just installed actually work?"""

    ok: bool
    detail: str

    def to_json(self) -> dict[str, object]:
        return {"ok": self.ok, "detail": self.detail}


def installed_executable() -> Path:
    """The `hyprial` the *next* process will run -- not this one's modules.

    ⚠️ This process holds the pre-upgrade code in memory (`autoupdate` preloads
    it deliberately, so the restart survives the distribution being replaced).
    Checking `import hyprial` from in here would therefore check the **old** build
    and pass no matter how broken the new one is: the check and the thing being
    checked would be different artefacts wearing the same name.
    """

    return Path(sys.executable).with_name("hyprial")


def run_self_check(
    *,
    executable: Path | None = None,
    timeout: float = 30.0,
    runner: Callable[[list[str]], "subprocess.CompletedProcess[str]"] | None = None,
) -> SelfCheckResult:
    """Ask the freshly installed build to answer one question about itself.

    On 2026-08-31 an upgrade installed cleanly and then died with
    `cannot import name 'updates' from 'hyprial'` -- the artefact was on disk and
    unusable, and nothing looked at it. So the check is deliberately the
    shallowest thing that would have caught that: run the installed binary and
    require a well-formed answer.

    ⚠️ It never raises. A self-check that can fail an upgrade would turn "we
    could not verify" into "the upgrade failed", and those are different facts
    with different responses -- the upgrade did complete, and saying otherwise
    would send someone to re-run an install that already worked.
    """

    binary = installed_executable() if executable is None else executable
    command = [str(binary), "version", "--json"]
    try:
        completed = (
            subprocess.run(
                command, text=True, capture_output=True, timeout=timeout, check=False
            )
            if runner is None
            else runner(command)
        )
    except Exception as error:  # noqa: BLE001 -- see the docstring
        return SelfCheckResult(False, f"could not run {binary}: {error}")
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-400:]
        return SelfCheckResult(
            False, f"`hyprial version` exited {completed.returncode}: {tail}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return SelfCheckResult(False, f"`hyprial version` printed non-JSON: {error}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return SelfCheckResult(False, f"`hyprial version` answered {completed.stdout[:200]}")
    version = payload.get("packageVersion")
    if not isinstance(version, str) or not version:
        return SelfCheckResult(False, "`hyprial version` reported no packageVersion")
    return SelfCheckResult(True, f"hyprial {version} answers `version --json`")


def notify_upgrade_outcome(
    *,
    hyprial_home: Path,
    state_dir: Path,
    host: str,
    action: str,
    upgrade_detail: str,
    check: SelfCheckResult,
    idempotency_key: str,
    sender_factory: Callable[[str, str], _OwnerSender] | None = None,
) -> AlertOutcome:
    """Tell the owner what happened -- on every upgrade, not only the bad ones.

    ⭐ Sending only on failure looks thrifty and is the reason this channel had
    never delivered a single message: the one path that would exercise it is the
    path where the machine is already in trouble. Twice on 2026-08-31 it was
    needed and twice nothing arrived -- once because the process died before
    reaching it, once because the owner's channel lookup had never been run and
    was wrong. Both were invisible for the same reason: **nothing used this
    code when things were fine.**

    So it runs every time. A healthy run sends one short line; anything else
    sends the detail. The short line is not noise -- it is the evidence that the
    channel still works, arriving at the only moment we can afford to discover
    that it does not.

    ⚠️ `action` is four states, not a bool, and the reason is a bug this
    signature used to have. The parameter was `upgraded: bool`, and the caller
    passed a hardcoded `True` on the whole success path -- so "already current,
    installed nothing" reported `✅ 升级完成`. `hyprial upgrade` runs on a timer and
    most runs install nothing, so the common case said something false.

    ⛔ And the obvious repair -- forward the result's real `upgraded` -- is
    worse: `not upgraded` fell to `⚠️ hyprial 升级失败`, which would have alarmed the
    owner on every routine no-op until the channel became noise. That is the
    outcome this whole module exists to prevent.

    🔑 The root cause is that one boolean carried two questions -- "did anything
    install?" and "did this succeed?" -- and the second is already carried by
    whether the caller raised. Four named states keep them apart, and they give
    the declined downgrade the one thing it lacked: somewhere to be said.
    """

    if action == UPGRADE_DECLINED_DOWNGRADE:
        # ⭐ A refused rollback is not a failure and is not routine. `#323`
        # added the guard with the complaint that "a guard leaving no trace is
        # indistinguishable from a rollback that never happened" -- this is the
        # trace. Reporting it as ✅ hides a real event behind a healthy tick;
        # reporting it as 失败 sends someone to fix a machine that is fine.
        summary = "⚠️ hyprial 拒绝降级 —— 上游轨道指向了更旧的版本"
        detail = (
            f"主机: {host}\n"
            f"未安装: {upgrade_detail}\n"
            f"自检: {'通过' if check.ok else '未通过'} — {check.detail}"
        )
    elif action == UPGRADE_AWAITING_RESTART:
        # Installed; the running daemon is still the old code until a person
        # (or an agent on their behalf) confirms.  Not "完成": nothing new is
        # running yet.  Not "失败": nothing went wrong.
        summary = f"hyprial 新版本已安装,等待确认后重启 · {upgrade_detail}"
        detail = (
            f"主机: {host}\n"
            "daemon 仍在旧版本上运行,没有重启。\n"
            "确认切换:运行 `hyprial autoupdate restart`(可自己运行,或让任一 agent 代为运行)。"
            "重启期间消息会中断,恢复完成会再通知。\n"
            f"自检: {'通过' if check.ok else '未通过'} — {check.detail}"
        )
    elif check.ok and action == UPGRADE_INSTALLED:
        summary = f"✅ hyprial 升级完成 · {check.detail}"
        detail = f"主机: {host}"
    elif action == UPGRADE_UNCONFIRMED:
        # ⭐ Installed fine; the daemon had not reported ready before the launch
        # budget ran out. It is alive and still restoring adapters, so this is
        # neither "完成" nor "失败" -- and both of those would state something
        # nobody has checked.
        #
        # ⚠️ It carries ⚠️ rather than ✅ on purpose: nothing settles this state
        # automatically yet, so the only thing that closes it is a person
        # looking. A ✅ would ask nobody to look.
        # ⚠️ Every clause below is something that was measured, and an earlier
        # draft of it was wrong in a way worth recording. It said "the daemon is
        # still starting" and "you can run `hyprial ps` right now", both inferred
        # from `cli._wait_for_daemon`'s comment: the socket is published before
        # adapter restore, and `ps` never reads `daemon.json`. The inference was
        # that IPC therefore answers throughout. It does not -- measured
        # 2026-08-31 19:43: `hyprial ps` failed twice during restore and succeeded
        # at 19:44:25, the moment the daemon reported ready. The failures were
        # not DAEMON_UNAVAILABLE (that returns a payload carrying a `daemon`
        # key; these had none), so the daemon holds the socket and does not
        # answer while it is busy restoring.
        #
        # ⭐ Telling someone to run a command that will time out is worse than
        # saying nothing: a timeout reads exactly like "my daemon is dead",
        # which is the conclusion this whole message exists to prevent.
        summary = "⚠️ hyprial 已升级;daemon 进程已启动,而尚未确认完成 adapter 恢复"
        detail = (
            f"主机: {host}\n"
            f"升级: {upgrade_detail}\n"
            f"自检: {'通过' if check.ok else '未通过'} — {check.detail}\n"
            "恢复期间 daemon 可能暂时答不上 `hyprial ps`"
            "(Lark adapter restore 可能要一分钟以上)—— 那不代表它死了。\n"
            "⇒ 过几分钟再跑一次 `hyprial ps`:\n"
            # ⭐ "daemon 已恢复服务", never "恢复完成". The two are not the same
            # state, and the second one may never arrive: restore is best
            # effort, and an adapter can stay down indefinitely while the daemon
            # runs perfectly well -- `hyprial-kanban` on this very machine has
            # been `quarantined` with `processRunning=False` for a long time,
            # answering `hyprial ps` throughout.
            #
            # ⛔ The earlier wording said "恢复完成", which does not merely
            # misname things: it tells the reader to stop looking at the one
            # moment something may still be wrong. That quarantined adapter was
            # found only because someone kept looking after `ps` started
            # answering.
            "   · 能答上来 ⇒ daemon 已恢复服务,无需处理\n"
            "     (adapter 是否全部起来,另看 `hyprial ps` 的 adapter 列 ——\n"
            "      恢复是 best effort,不是每次都会全起来)\n"
            f"   · {_UNCONFIRMED_ESCALATE_AFTER}后仍答不上来 ⇒ 那才是故障,"
            "把 `hyprial ps` 的输出发出来"
        )
    elif check.ok and action == UPGRADE_ALREADY_CURRENT:
        # The heartbeat. Nothing was installed and nothing is wrong -- and this
        # is the line that proves, on an ordinary day, that the channel through
        # which bad news would arrive is still open.
        summary = f"✅ hyprial 已是最新,未安装任何东西 · {check.detail}"
        detail = f"主机: {host}"
    else:
        summary = (
            "⚠️ hyprial 升级后自检未通过"
            if action == UPGRADE_INSTALLED
            else "⚠️ hyprial 升级失败"
        )
        detail = (
            f"主机: {host}\n"
            f"升级: {upgrade_detail}\n"
            f"自检: {'通过' if check.ok else '未通过'} — {check.detail}"
        )
    return _send_owner_message(
        hyprial_home=hyprial_home,
        state_dir=state_dir,
        text=f"{summary}\n{detail}",
        idempotency_key=idempotency_key,
        sender_factory=sender_factory,
    )
