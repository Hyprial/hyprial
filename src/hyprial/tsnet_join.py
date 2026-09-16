"""``hyprial login`` — the network join stage (login U3b): drive hyprial-tsnet to
``ready`` over protocol v1 (rev. 8).

After the CLI's optional sidecar acquisition, the identity stage (``login.py``)
lands ``settings.owner`` + ``secrets/login.json``; this module is the **second
half** of ``hyprial login`` and is called through exactly one entry
(:func:`run_join`).
One ``hyprial login`` = one sidecar process over stdio JSON lines (protocol
``notes/hyprial-tsnet-sidecar-protocol-v1-2026-09-04.md`` — the only interface):

    hyprial → sidecar (stdin):  {"v":1,"op":"up",...} / {"v":1,"op":"down"}
    sidecar → hyprial (stdout): hello → state* → [browse_to_url] → ready | error
                            → exited
    sidecar stderr:         human logs; only appended to
                            ``$HYPRIAL_HOME/state/logs/tsnet.log``, never parsed

Semantics pinned by spec U3b (each has a test):

- **Sidecar verification** (T9): before every start the installed
  ``$HYPRIAL_HOME/bin/hyprial-tsnet`` is re-checked against the pinned sha256, and
  the first stdout line must be ``hello`` with ``v == 1`` and
  ``sidecar == SIDECAR_VERSION``.  Missing/mismatched ⇒
  ``network.status=failed`` with reason ``SIDECAR_MISSING``/
  ``SIDECAR_MISMATCH`` and a pointer to
  ``hyprial login --install-sidecar``.  Acquisition, when requested or
  confirmed, happens in the CLI before identity; this join stage never downloads.
- **kind × join** (T3/T4): ``controlUrl`` is ``""`` for
  ``kind=tailscale`` and ``profile.control_plane_url`` for headscale,
  passed through verbatim; ``join`` always comes from the profile;
  ``interactive`` sends ``authKey: null`` and rejects a supplied
  ``--preauthkey-file`` (sidecar's ``AUTHKEY_UNEXPECTED``); ``preauthkey``
  requires the file (``AUTHKEY_MISSING``).  Both are checked **before the
  process is started**.
- **Two-layer join timeout** (T14–T21, U3d/U3e): the ``up`` request carries
  ``timeoutSeconds = max(1, ⌊join_timeout⌋ − margin)`` so the sidecar's
  own timer expires first (边车先到期) and it gets to classify by
  ``lastState`` — a rejected auth key surfaces as ``AUTHKEY_INVALID``
  with the sidecar removing its own ``.pending-*`` state — while hyprial's
  ``--join-timeout`` deadline is only the fallback (兜底) for a sidecar
  that never fires (old binary, stuck process) and reports ``TIMEOUT``;
  every failure path, the fallback included, asserts no run residue.
  Since tsnet-v0.1.3 the sidecar emits ``error`` **before** its ≈5 s
  blocking Close (card 6cdcc406), which is why the margin is 5 again and
  the post-``error`` exit grace is 10 (the Close still has to finish).
- **hostname** (T1): the ``up.hostname`` is the short label (before the
  first ``.``) of the existing machine id (``HYPRIAL_NODE_ID`` >
  ``socket.gethostname()`` — the same rule the daemon's node id uses);
  after ``ready``, the FQDN's short label must equal it or the join fails
  with ``HOSTNAME_MISMATCH`` and nothing is written.
- **preauthkey hygiene** (T2): the key is read from a file or stdin,
  stripped, held in one local, and written to exactly one place — the
  ``up`` request line on the sidecar's stdin pipe.  Never argv, never env,
  never disk, never a log line, never an emit event.
- **owner is never touched** (T8, D12): ``ready.user`` is *recorded* —
  under preauthkey it is a tag identity and will routinely differ from the
  Casdoor username — a difference is a ``network.warnings`` entry and
  nothing else.  ⭐ T8 anchor: **this module must not import
  ``write_settings_owner``**; the join stage has no owner write path at
  all.  A failed join never rolls the identity stage back: ``settings.json``
  and ``secrets/login.json`` keep their bytes (D12), and a re-run retries
  only the join.
- **daemon boundary** (T11): nothing under ``hyprial/daemon/`` may import this
  module or ``tsnet_sidecar``, and the daemon never reads
  ``state/tsnet/node.json`` — it is display state for ``hyprial login --json``
  / ``hyprial profile list`` only.

The environment handed to the sidecar strips every ``TS_*`` variable and
every proxy variable (upper and lower case): the sidecar refuses ambient
credentials by design and hyprial does not want this process's proxies shaping
the control-plane connection (protocol §1/§4).

The join stage reports, it does not fail the command: identity success +
join failure is a successful ``hyprial login`` with
``network.status="failed"`` and ``reason`` set (D12 — re-run retries the
join alone).  ⛔ The ``network`` section never contains the authKey or any
secret beyond public URLs.
"""

from __future__ import annotations

import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.network_profile import TSNET_STATE_DIRNAME, NetworkProfile
from hyprial.home import configured_hyprial_home
from hyprial.persistent_config import atomic_json_write
from hyprial.tsnet_sidecar import (
    SIDECAR_VERSION,
    verify_installed_sidecar,
)

__all__ = [
    "JoinOutcome",
    "PROTOCOL_VERSION",
    "default_open_url",
    "run_join",
]

PROTOCOL_VERSION = 1
"""The wire ``v`` of protocol v1 (rev. 8) this driver speaks."""

_SKIP_REASON = "skipped"
_EXIT_GRACE_S = 10.0
"""How long to wait for the sidecar to exit after ``down`` before killing.

The sidecar exits on its own after ``down``/``error`` (it self-``down``s and
emits ``exited``); this is only the reluctant kill deadline.  Since
tsnet-v0.1.3 the ``error`` event is emitted **before** the ≈5 s blocking
``backend.Close()`` (card 6cdcc406), so by the time hyprial has received
``error`` and sent ``down``, the sidecar still has that whole Close ahead
of it before it can write ``exited`` and exit — a 5 s grace (the pre-U3e
value) tied that latency exactly and degenerated into a kill (the state is
already clean by then, so a kill leaves no residue, but it costs the
``exited`` handshake line and one extra process kill); 10 s gives the
measured Close one full factor of headroom and keeps the kill what it is
meant to be: the last resort (T21 anchors the value).  This is NOT the
join timeout — that one is a CLI parameter (``--join-timeout``), never
a scattered constant."""

SIDECAR_TIMEOUT_MARGIN_SECONDS = 5
"""Subtracted from ``--join-timeout`` to get ``up.timeoutSeconds`` (U3e).

The sidecar must expire first (边车先到期) so that it — not hyprial — gets to
classify the failure by ``lastState`` (a rejected auth key ⇒
``AUTHKEY_INVALID``) and to remove its own ``.pending-*`` state; hyprial's
``--join-timeout`` deadline is only the fallback (兜底).

Why 5 is enough again (U3e, tsnet-v0.1.3, card 6cdcc406): the sidecar now
emits ``error`` the moment its own timer expires — measured 2026-09-06 on
the released v0.1.3, ``error`` landed +0.02 s after expiry — and only then
runs the ≈5 s blocking ``backend.Close()``.  The margin therefore only has
to cover the wire round-trip plus the classification, not the sidecar's
shutdown: 5 s does that with room to spare.  U3d's 10 existed to cover
tsnet-v0.1.2's Close-**before**-error ordering (measured: ``error`` landed
≈5 s after the sidecar's expiry), which pinned a shutdown latency of that
superseded build into hyprial's product timeout; with the fix in the binary
the margin comes back down.  Note the gap is this constant regardless of
``--join-timeout`` — the default 300 s path (295 s vs 300 s) behaves the
same way, which is why the fix is the margin and not a bigger timeout.
The margin is deliberately not a CLI parameter (no user needs it; T18
anchors the value)."""

_SIDECAR_LOG = "state/logs/tsnet.log"
"""Where the sidecar's stderr is appended verbatim (protocol §1)."""

_SIDECAR_ARGS = ("serve",)
"""The sidecar is a subcommand binary (``hyprial-tsnet serve|version``); ``serve`` is
the protocol-v1 stdio mode (``cmd/hyprial-tsnet/main.go`` — the fake test fixture
needs no argument and tests always pass their own argv seam)."""

_PROXY_ENV_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)

EmitFn = Callable[[str, dict[str, Any]], None]


def default_open_url(url: str) -> bool:
    """Open a browser tab (the identity stage's degraded-to-print rule:
    any webbrowser failure just leaves the printed URL)."""

    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - any webbrowser failure degrades to print
        return False


@dataclass(slots=True)
class JoinOutcome:
    """Everything the ``network`` section of ``hyprial login --json`` reports."""

    status: str
    kind: str
    control_url: str
    join: str
    hostname: str
    ip4: str | None = None
    user: str | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    node_json_path: Path | None = None

    def as_network(self) -> dict[str, Any]:
        """The exact ``network`` section shape (spec §1.2 item 7):

        ``{status, kind, controlUrl, join, hostname, ip4, user, reason,
        warnings}`` — nine keys, no authKey, no secret beyond public URLs.
        """

        return {
            "status": self.status,
            "kind": self.kind,
            "controlUrl": self.control_url,
            "join": self.join,
            "hostname": self.hostname,
            "ip4": self.ip4,
            "user": self.user,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


# -- helpers -------------------------------------------------------------------


def _home_dir(environ: Mapping[str, str], hyprial_home: Path | None) -> Path:
    """The same home rule as login.py's identity stage (one home for all
    landing spots: explicit ``hyprial_home`` > ``HYPRIAL_HOME`` > ``~/.hyprial``)."""

    if hyprial_home is not None:
        return Path(hyprial_home)
    return configured_hyprial_home(environ)[0]


def _machine_label(environ: Mapping[str, str], machine: str | None) -> str:
    """The existing machine id rule (``HYPRIAL_NODE_ID`` > hostname), short label.

    This is the daemon's own node-id rule (``HYPRIAL_NODE_ID`` else
    ``socket.gethostname()``, as ``cli.py``/``adapter_registration.py``
    already spell it) — not a new invention; the tsnet hostname is its
    first-dot short label (protocol §2: ``hyprial-hq.orkhon-bee.ts.net``
    would make a strange Hostname verbatim).
    """

    value = machine if machine is not None else (
        (environ.get("HYPRIAL_NODE_ID") or "").strip() or socket.gethostname().strip()
    )
    return value.split(".", 1)[0].strip()


def _short_label(fqdn: str) -> str:
    return fqdn.split(".", 1)[0].strip()


def _control_url(profile: NetworkProfile) -> str:
    """``""`` for the official control plane, else the profile URL verbatim."""

    return "" if profile.control_plane_kind == "tailscale" else profile.control_plane_url


def _sidecar_env(environ: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``environ`` without ``TS_*`` or proxy variables.

    The sidecar ignores ambient credentials by design; passing a clean env
    is hyprial's half of that contract (protocol §1) and keeps this process's
    proxies away from the control-plane connection (protocol §4)."""

    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("TS_") and key not in _PROXY_ENV_KEYS
    }


def _settings_owner_readonly(home: Path) -> str | None:
    """Best-effort read of ``settings.owner`` for the T8 warning — read-only.

    ⛔ This is the ONLY thing this module ever does with settings.json.
    The identity stage owns the file; the join stage compares and warns.
    """

    try:
        record = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    owner = record.get("owner")
    return owner if isinstance(owner, str) and owner.strip() else None


def _read_preauthkey(
    source: str, *, read_stdin: Callable[[], str] | None
) -> str:
    """Read the preauthkey from a file path or ``-`` (stdin); strip it.

    The value is returned to exactly one caller and flows to exactly one
    destination: the ``up`` request line on the sidecar's stdin.  Nothing
    here logs, emits, or persists it.
    """

    if source == "-":
        reader = sys.stdin.read if read_stdin is None else read_stdin
        return reader().strip()
    try:
        return Path(source).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise _KeySourceError(
            f"cannot read --preauthkey-file {source}: {error}"
        ) from error


class _KeySourceError(Exception):
    """Internal: an unreadable preauthkey source (never carries the key)."""


def _tsnet_dir(home: Path) -> Path:
    return home / TSNET_STATE_DIRNAME


def _sidecar_timeout_seconds(join_timeout: float) -> int:
    """The sidecar's own join timer, handed over as ``up.timeoutSeconds``.

    Always a positive integer — the wire (``proto/message.go``) rejects
    ``timeoutSeconds <= 0`` — so a ``--join-timeout`` smaller than the
    margin clamps to 1 s instead of going negative.
    """

    return max(1, math.floor(join_timeout) - SIDECAR_TIMEOUT_MARGIN_SECONDS)


# -- the sidecar driver ----------------------------------------------------------


class _Sidecar:
    """One sidecar process: spawn, hello, event stream, shutdown.

    Stdout lines are read on a thread into a queue (the event loop owns the
    deadline); stderr is appended verbatim to ``state/logs/tsnet.log`` by a
    second thread so the pipe can never fill and block the sidecar.
    """

    def __init__(self, argv: list[str], env: Mapping[str, str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._log = open(log_path, "ab")  # closed in close()
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
        )
        self._events: queue.Queue[bytes | None] = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="tsnet-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="tsnet-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._events.put(line)
        finally:
            self._events.put(None)

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            for chunk in iter(lambda: self.process.stderr.read(4096), b""):  # type: ignore[union-attr]
                self._log.write(chunk)
                self._log.flush()
        except (OSError, ValueError):
            pass

    def next_event(self, timeout: float) -> dict[str, Any] | None:
        """The next parsed stdout event, or None on EOF/timeout."""

        try:
            line = self._events.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None
        if line is None:
            return None
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return self.next_event(timeout) if timeout > 0 else None
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return {"v": PROTOCOL_VERSION, "event": "unparseable", "raw": text}
        return event if isinstance(event, dict) else {"v": PROTOCOL_VERSION, "event": "unparseable", "raw": text}

    def send(self, payload: Mapping[str, Any]) -> None:
        """One JSON request line to the sidecar's stdin (best effort).

        The sidecar exits on its own after ``error``/``down``; a write to a
        closed pipe is expected and ignored.
        """

        try:
            assert self.process.stdin is not None
            self.process.stdin.write(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            )
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def shutdown(self) -> None:
        """``down``, wait for exit, kill after the grace deadline."""

        self.send({"v": PROTOCOL_VERSION, "op": "down"})
        try:
            self.process.wait(timeout=_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.close()

    def close(self) -> None:
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._log.close()


def _tsnet_state_snapshot(tsnet: Path) -> set[str]:
    try:
        return {entry.name for entry in tsnet.iterdir()}
    except OSError:
        return set()


def _assert_no_residue(
    outcome: JoinOutcome, tsnet: Path, before: set[str]
) -> None:
    """After a failed join the state dir must hold no run residue.

    The sidecar guarantees no half state before ``Running`` (pending dir is
    atomically promoted or cleaned); hyprial asserts it.  ``node.json`` that
    predates this run describes a *previous* successful join and is kept —
    deleting it would misreport the tailnet.  Only files that appeared
    during this failed run are residue and surface as a warning.
    """

    try:
        after = {entry.name for entry in tsnet.iterdir()}
    except OSError:
        return
    residue = sorted(
        name
        for name in after - before
        if name.startswith(".pending-") or name == "node.json"
    )
    if residue:
        outcome.warnings.append(
            f"sidecar left unexpected state after failure: {residue}"
        )


# -- the one entry point ----------------------------------------------------------


def run_join(
    profile: NetworkProfile,
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
    preauthkey_file: str | None = None,
    join_timeout: float = 300.0,
    skip_join: bool = False,
    open_browser: bool = True,
    emit: EmitFn | None = None,
    open_url: Callable[[str], bool] | None = None,
    machine: str | None = None,
    sidecar_argv: list[str] | None = None,
    read_stdin: Callable[[], str] | None = None,
    now: Callable[[], datetime] | None = None,
    clock: Callable[[], float] | None = None,
) -> JoinOutcome:
    """Drive the join stage; never raises for a join failure (D12).

    The CLI passes the resolved profile and the parsed flags; everything a
    test needs to script (machine id, sidecar argv, stdin reader, clock) is
    a seam.  ``join_timeout``'s *default* lives in the CLI parameter
    (``--join-timeout``, 300 s — five minutes of headroom over the OIDC
    device-code lifetime, the same budget the identity stage polls);
    this signature default only mirrors it so direct callers stay honest.
    """

    env = dict(os.environ if environ is None else environ)
    home = _home_dir(env, hyprial_home)
    now = now or (lambda: datetime.now(UTC))
    clock = time.monotonic if clock is None else clock

    def notify(kind: str, data: dict[str, Any]) -> None:
        if emit is not None:
            emit(kind, data)

    hostname = _machine_label(env, machine)
    outcome = JoinOutcome(
        status="failed",
        kind=profile.control_plane_kind,
        control_url=_control_url(profile),
        join=profile.join,
        hostname=hostname,
        reason=None,
    )

    if skip_join:
        outcome.status = "not-attempted"
        outcome.reason = _SKIP_REASON
        notify("skipped", {"reason": _SKIP_REASON})
        return outcome

    # -- T4: join/key consistency, before any process is started.
    if profile.join == "interactive" and preauthkey_file is not None:
        outcome.reason = "AUTHKEY_UNEXPECTED"
        notify(
            "failed",
            {
                "reason": outcome.reason,
                "message": "join is interactive but --preauthkey-file was "
                "given; drop the flag or switch the profile's join",
            },
        )
        return outcome
    auth_key: str | None = None
    if profile.join == "preauthkey":
        if preauthkey_file is None:
            outcome.reason = "AUTHKEY_MISSING"
            notify(
                "failed",
                {
                    "reason": outcome.reason,
                    "message": "join is preauthkey but no --preauthkey-file "
                    "was given",
                },
            )
            return outcome
        try:
            auth_key = _read_preauthkey(preauthkey_file, read_stdin=read_stdin)
        except _KeySourceError as error:
            outcome.reason = "AUTHKEY_MISSING"
            notify("failed", {"reason": outcome.reason, "message": str(error)})
            return outcome
        if not auth_key:
            auth_key = None
            outcome.reason = "AUTHKEY_MISSING"
            notify(
                "failed",
                {
                    "reason": outcome.reason,
                    "message": "--preauthkey-file is empty",
                },
            )
            return outcome

    # -- T9: the installed sidecar is verified before every start.  The same
    # path/platform/pin gate is also used by login before offering acquisition;
    # keep one implementation so the two callers cannot drift again.
    sidecar_path, sidecar_reason = verify_installed_sidecar(home)
    if sidecar_reason == "SIDECAR_MISSING":
        outcome.reason = sidecar_reason
        outcome.warnings.append(
            "sidecar binary not found; run: hyprial login --install-sidecar"
        )
        notify(
            "failed",
            {"reason": outcome.reason, "hint": "hyprial login --install-sidecar"},
        )
        return outcome
    if sidecar_reason is not None:
        # Preserve run_join's public two-reason contract: an unsupported
        # platform cannot match a shipped pin and is reported as mismatch.
        outcome.reason = "SIDECAR_MISMATCH"
        outcome.warnings.append(
            "sidecar binary does not match the pinned sha256; "
            "run: hyprial login --install-sidecar"
        )
        notify(
            "failed",
            {"reason": outcome.reason, "hint": "hyprial login --install-sidecar"},
        )
        return outcome
    assert sidecar_path is not None

    tsnet = _tsnet_dir(home)
    tsnet.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_before = _tsnet_state_snapshot(tsnet)

    up_request: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "op": "up",
        "controlUrl": outcome.control_url,
        "hostname": hostname,
        "dir": str(tsnet),
        "ephemeral": False,
        "join": profile.join,
        "authKey": auth_key,
        "timeoutSeconds": _sidecar_timeout_seconds(join_timeout),
    }

    sidecar = _Sidecar(
        list(sidecar_argv) if sidecar_argv is not None else [str(sidecar_path), *_SIDECAR_ARGS],
        _sidecar_env(env),
        home / _SIDECAR_LOG,
    )
    deadline = clock() + max(join_timeout, 0.0)

    def remaining() -> float:
        return deadline - clock()

    try:
        # -- hello handshake (protocol §5): v and sidecar version must match
        # the pin exactly; anything else points at the login acquisition flag.
        hello = sidecar.next_event(remaining())
        if (
            not isinstance(hello, dict)
            or hello.get("event") != "hello"
            or hello.get("v") != PROTOCOL_VERSION
            or hello.get("sidecar") != SIDECAR_VERSION
        ):
            outcome.reason = "SIDECAR_MISMATCH"
            outcome.warnings.append(
                "sidecar hello does not match the pinned protocol/version; "
                "run: hyprial login --install-sidecar"
            )
            sidecar.shutdown()
            notify(
                "failed",
                {
                    "reason": outcome.reason,
                    "hint": "hyprial login --install-sidecar",
                },
            )
            return outcome

        sidecar.send(up_request)

        while True:
            wait = remaining()
            if wait <= 0:
                outcome.reason = "TIMEOUT"
                sidecar.shutdown()
                # Same residue assertion as the other failure branches:
                # the fallback path must not be the one path nobody looks
                # at (U3d — the deadline is a backstop, not the classifier).
                _assert_no_residue(outcome, tsnet, state_before)
                notify("failed", {"reason": outcome.reason})
                return outcome
            event = sidecar.next_event(wait)
            if event is None:
                # None is either the deadline expiring mid-wait or the
                # sidecar's stdout closing without ready/error — the clock
                # tells them apart.
                if remaining() <= 0:
                    outcome.reason = "TIMEOUT"
                else:
                    outcome.reason = outcome.reason or "PROTOCOL"
                sidecar.shutdown()
                _assert_no_residue(outcome, tsnet, state_before)
                notify("failed", {"reason": outcome.reason})
                return outcome
            kind = event.get("event")
            if kind == "state":
                notify("state", {"state": event.get("state")})
                continue
            if kind == "browse_to_url":
                url = str(event.get("url") or "")
                notify("browse_to_url", {"url": url})
                if open_browser:
                    opener = default_open_url if open_url is None else open_url
                    opener(url)
                continue
            if kind == "exited":
                outcome.reason = outcome.reason or "PROTOCOL"
                sidecar.close()
                _assert_no_residue(outcome, tsnet, state_before)
                notify("failed", {"reason": outcome.reason})
                return outcome
            if kind == "error":
                outcome.reason = str(event.get("code") or "PROTOCOL")
                outcome.warnings.append(
                    f"sidecar error: {event.get('message') or outcome.reason}"
                )
                sidecar.shutdown()
                _assert_no_residue(outcome, tsnet, state_before)
                notify("failed", {"reason": outcome.reason})
                return outcome
            if kind == "ready":
                break
            # Unknown events are tolerated (forward-compat): v1's set is
            # closed but an additive future field must not kill a join.

        ready = event
        ready_hostname = str(ready.get("hostname") or "")
        if _short_label(ready_hostname) != hostname:
            # T1: the tailnet must know this node under the machine's short
            # label; anything else means the join landed on a different
            # identity — fail, write nothing.
            outcome.reason = "HOSTNAME_MISMATCH"
            outcome.warnings.append(
                f"ready hostname {ready_hostname!r} does not start with the "
                f"machine label {hostname!r}"
            )
            sidecar.shutdown()
            notify("failed", {"reason": outcome.reason})
            return outcome

        ready_user = ready.get("user")
        user = ready_user if isinstance(ready_user, str) and ready_user else None
        outcome.user = user
        owner = _settings_owner_readonly(home)
        if user is not None and owner is not None and user != owner:
            # T8 / protocol §2 补条 b: a difference is a warning, never a
            # failure and never an owner write (preauthkey joins are tag
            # identities and routinely differ).
            outcome.warnings.append(
                f"control-plane user {user} != owner {owner}"
            )

        node_record = {
            "ip4": ready.get("ip4"),
            "ip6": ready.get("ip6"),
            "hostname": ready_hostname,
            "user": user,
            "nodeKeyFingerprint": ready.get("nodeKeyFingerprint"),
            "controlUrl": ready.get("controlUrl", outcome.control_url),
            "kind": outcome.kind,
            "joinedAt": now().isoformat(),
        }
        node_path = tsnet / "node.json"
        atomic_json_write(node_path, node_record)
        outcome.status = "joined"
        outcome.ip4 = node_record["ip4"]
        outcome.hostname = ready_hostname
        outcome.node_json_path = node_path
        sidecar.shutdown()
        notify(
            "joined",
            {
                "hostname": ready_hostname,
                "ip4": outcome.ip4,
                "user": user,
                "nodeJson": str(node_path),
            },
        )
        return outcome
    finally:
        # Every path above closes the process; this is the belt for an
        # unexpected exception between states.
        if sidecar.process.poll() is None:
            sidecar.shutdown()
