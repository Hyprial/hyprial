"""The typed result of a daemon launch — one shape, three readers (PR #332 F2).

Before this type existed, ``_launch_daemon_process`` returned a flat dict
(``{ok, started, alreadyRunning, **ping}``) while ``hyprial upgrade`` still dug
for the nested ps shape ``launched["daemon"]`` it used to return.  Every
upgrade therefore ended in INVALID_RESPONSE ⇒ UPGRADE_RESTART_FAILED with a
false owner alert — and the suite stayed green because the test stub
hand-wrote the old nested shape the reader expected
(``tests/test_cli_upgrade_restart.py``).  A stub that can invent its own
form can paper over any misread; closing that is this module's whole job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _ping_block(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """See through the legacy ps nest; this build's ping answer is flat.

    ``_daemon_probe`` falls back to ``ps`` for daemons older than the ping
    contract, and ``ps`` nests its daemon block.  The named fields below
    read through that nest so a legacy probe cannot turn them into silent
    ``None``\\s; the JSON form keeps the probe answer verbatim either way.
    """

    daemon = status.get("daemon")
    return daemon if isinstance(daemon, dict) else status


@dataclass(frozen=True, slots=True)
class DaemonLaunchResult:
    """What ``_launch_daemon_process`` returns — typed once for every reader.

    Exactly three readers, and the type is defined for all of them (F2
    adjudication, 2026-09-04):

    1. ``hyprial init`` — emits ``to_payload()`` (plus its warnings) as the
       command's JSON output, and re-wraps the already-running branch;
    2. ``hyprial upgrade`` — reads ``running``/``pid`` for the fresh-pid check
       after the post-install restart;
    3. the e2e contract runner
       (``contract/e2e-scenarios/_daemon_runner.py``) — parses ``hyprial init
       --json`` output, i.e. ``to_payload()`` after it crosses the process
       boundary, reading ``ok``/``running`` flat at the top level.

    The JSON form is FLAT: ``{ok, started, alreadyRunning, **ping}`` where
    ``ping`` is the daemon's readiness answer (``running``/``pid``/
    ``epoch``/``phase``/``nodeId``/``owner``/``socket``/``restorePending``/
    ``zenoh``…, produced by the daemon's ping handler).  It is not the
    nested ps shape ``{"daemon": {...}}`` — confusing the two is F2.  Test
    stubs must be built by this type (``DaemonLaunchResult.launched(ping)``
    / ``.existing(ping)``) or by the real ``_launch_daemon_process``
    over a fake ping; hand-written dicts are what masked the bug.
    """

    ok: bool
    started: bool
    already_running: bool
    running: bool
    pid: int | None
    epoch: str | None
    phase: str | None
    # The probe answer, verbatim.  ``to_payload()`` spreads it flat; the
    # daemon may grow ping fields without this contract noticing, because
    # only fields a reader consumes by name are promoted to fields above.
    # Always built through the ``launched``/``already_running`` constructors.
    status: Mapping[str, Any]

    @classmethod
    def launched(cls, status: Mapping[str, Any]) -> DaemonLaunchResult:
        """A fresh generation was started and answered its readiness ping."""

        return cls._build(started=True, already_running=False, status=status)

    @classmethod
    def existing(cls, status: Mapping[str, Any]) -> DaemonLaunchResult:
        """A probe found a daemon already serving; nothing was started.

        Named ``existing`` rather than ``already_running`` because a
        classmethod sharing a field's name becomes that field's default
        value under ``@dataclass`` — the collision is silent until
        construction, so the names must simply differ.
        """

        return cls._build(started=False, already_running=True, status=status)

    @classmethod
    def _build(
        cls, *, started: bool, already_running: bool, status: Mapping[str, Any]
    ) -> DaemonLaunchResult:
        ping = _ping_block(status)
        pid = ping.get("pid")
        epoch = ping.get("epoch")
        phase = ping.get("phase")
        return cls(
            ok=True,
            started=started,
            already_running=already_running,
            running=ping.get("running") is True,
            pid=pid if isinstance(pid, int) else None,
            epoch=epoch if isinstance(epoch, str) else None,
            phase=phase if isinstance(phase, str) else None,
            status=dict(status),
        )

    def to_payload(self) -> dict[str, Any]:
        """The flat JSON form: launch verdict first, ping answer spread flat.

        Byte-for-byte the shape ``_launch_daemon_process`` used to build by
        hand, so ``hyprial init --json`` output (and the runner parsing it) is
        unchanged.  A legacy ps probe answer keeps its nested ``daemon``
        block here exactly as the hand-built dict did.
        """

        return {
            "ok": self.ok,
            "started": self.started,
            "alreadyRunning": self.already_running,
            **self.status,
        }
