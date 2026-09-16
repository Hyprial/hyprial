"""lark-cli user-credential watchdog: detect, escalate to "one click away", notify.

This module owns the *user OAuth token* held by ``lark-cli`` (its own profile
store), which is a different credential from the hyprial Lark adapter's App
credential (``app_id``/``app_secret`` in ``~/.hyprial/secrets``).  The adapter's
device-authorization onboarding (:mod:`hyprial.adapters.lark.onboarding`) cannot
help here; lark-cli carries its own device flow, and this module drives its
split form:

1. ``auth status --json --verify`` classifies the user identity;
2. ``needs_refresh`` with a live refresh token is recoverable *unattended* --
   lark-cli refreshes lazily, so one side-effect-free read triggers it
   (the #88 pattern); nobody is notified;
3. only a dead/missing refresh path escalates to
   ``auth login --no-wait --json``, which mints a ``verification_url`` +
   ``device_code`` without blocking -- the "就差你点一下" handoff.  The URL is
   pushed to a human over an existing hyprial route; ``auth login
   --device-code`` completes after the human clicks.

Red lines encoded here:

* automation stops at the human's click -- nothing here approves, mints, or
  forges a credential;
* no secret material (``app_secret``, tokens, ``device_code``) is ever
  formatted into results, messages, or logs; the ``device_code`` lives only in
  the 0600 state file so :func:`complete` can finish the flow;
* the honest readiness criterion is ``user.status == "ready"`` AND
  ``user.tokenStatus == "valid"``.  The top-level ``verified`` field is
  deliberately ignored: it only appears with ``--verify`` and reflects the
  default/auto identity (a ready *bot* makes it ``True`` while the user
  identity is ``missing`` -- field-level defect #141).

The chicken-and-egg question ("the notification channel dies with the
credential") does not hold on the main path: notification rides the daemon's
*App* credential (bot/tenant token), a different app and store than the
expired user token.  It holds only at the edges -- daemon/adapter unhealthy,
or the URL expired before the human saw it -- so every notification failure
degrades to the local state file plus an honest result, and an expired link
is regenerated on the next check after the cooldown, never reused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

STATE_FILENAME = "lark-reauth.json"
ENV_NOTIFY_ROUTE = "HYPRIAL_LARK_REAUTH_NOTIFY_ROUTE"
ENV_SENDER = "HYPRIAL_LARK_REAUTH_FROM"

# Per-appId notification cooldown: the same dead credential must not re-push
# a link on every timer tick.
DEFAULT_COOLDOWN_SECONDS = 3600.0

# Domains requested when none are configured.  This is a *default*, not a
# law: whether a fresh ``auth login`` after refresh-token expiry restores the
# previously granted scopes is unverified (the CLI documents incremental
# grants, not restoration), so the safe default is to re-request the broad
# set the E2E gates and lark-* skills actually use.
DEFAULT_DOMAINS: tuple[str, ...] = ("all",)

_STATUS_TIMEOUT_SECONDS = 30.0
_COMPLETE_MAX_SECONDS = 900.0

# lark-cli lookup fallbacks beyond PATH (the E2E runners pin the same one).
_LARK_CLI_FALLBACKS: tuple[str, ...] = ("/opt/homebrew/bin/lark-cli",)

IdentityClass = Literal["ok", "refreshable", "needs_human"]

CheckStatus = Literal[
    "unavailable", "ok", "recovered", "awaiting_user", "no_pending", "authorized"
]


class ReauthError(RuntimeError):
    """A lark-cli invocation or its payload was unusable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CliRunner(Protocol):
    """Runs lark-cli argv (without the leading executable) and returns the
    parsed success payload.  Injected so the branch structure is testable
    without the binary or the network."""

    def __call__(self, arguments: Sequence[str], *, timeout: float) -> dict[str, Any]: ...


# Sender injected by the caller; raises on delivery failure.
NotifySender = Callable[[str, str, str], None]


# ---------------------------------------------------------------------------
# classification (pure)


def _parse_timestamp(value: object) -> float | None:
    """RFC3339/ISO-8601 with offset -> epoch seconds; anything else -> None."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def user_identity(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    identities = payload.get("identities")
    if not isinstance(identities, Mapping):
        return {}
    user = identities.get("user")
    return user if isinstance(user, Mapping) else {}


def classify_user_identity(
    payload: Mapping[str, Any], *, now: float | None = None
) -> IdentityClass:
    """The honest readiness criterion (#141: top-level ``verified`` is ignored).

    * ``ok`` -- ``user.status == "ready"`` and ``user.tokenStatus == "valid"``;
    * ``refreshable`` -- ``tokenStatus == "needs_refresh"`` while the refresh
      token is still alive (``refreshExpiresAt`` in the future, or absent:
      absence is not proof of death, so give the lazy refresh one chance);
    * ``needs_human`` -- everything else: ``missing``, ``None`` token status,
      or a refresh token whose own expiry has passed.
    """

    user = user_identity(payload)
    status = user.get("status")
    token_status = user.get("tokenStatus")
    if status == "ready" and token_status == "valid":
        return "ok"
    if token_status == "needs_refresh":
        refresh_expires = _parse_timestamp(user.get("refreshExpiresAt"))
        if refresh_expires is not None and now is not None and refresh_expires <= now:
            return "needs_human"
        return "refreshable"
    return "needs_human"


# ---------------------------------------------------------------------------
# state (0600; the only place a device_code may persist)


@dataclass(slots=True)
class ReauthState:
    """One pending device-authorization handoff.

    ``device_code`` is credential-adjacent: it is written only here (0600),
    never logged, never returned in a result payload.
    """

    app_id: str | None = None
    device_code: str | None = None
    verification_url: str | None = None
    expires_at: float | None = None
    notified_at: float | None = None
    notify_route: str | None = None
    sender: str | None = None
    domains: tuple[str, ...] = ()

    def pending_valid(self, now: float) -> bool:
        return (
            self.device_code is not None
            and self.expires_at is not None
            and self.expires_at > now
        )

    def may_notify(self, now: float, cooldown: float = DEFAULT_COOLDOWN_SECONDS) -> bool:
        return self.notified_at is None or (now - self.notified_at) >= cooldown

    def to_json(self) -> dict[str, Any]:
        return {
            "appId": self.app_id,
            "deviceCode": self.device_code,
            "verificationUrl": self.verification_url,
            "expiresAt": self.expires_at,
            "notifiedAt": self.notified_at,
            "notifyRoute": self.notify_route,
            "sender": self.sender,
            "domains": list(self.domains),
        }

    @classmethod
    def from_json(cls, value: object) -> ReauthState | None:
        if not isinstance(value, Mapping):
            return None

        def _str(key: str) -> str | None:
            item = value.get(key)
            return item if isinstance(item, str) and item else None

        def _float(key: str) -> float | None:
            item = value.get(key)
            return float(item) if isinstance(item, (int, float)) else None

        raw_domains = value.get("domains")
        domains = (
            tuple(item for item in raw_domains if isinstance(item, str) and item)
            if isinstance(raw_domains, list)
            else ()
        )
        return cls(
            app_id=_str("appId"),
            device_code=_str("deviceCode"),
            verification_url=_str("verificationUrl"),
            expires_at=_float("expiresAt"),
            notified_at=_float("notifiedAt"),
            notify_route=_str("notifyRoute"),
            sender=_str("sender"),
            domains=domains,
        )


def load_state(path: Path) -> ReauthState | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return ReauthState.from_json(json.loads(raw))
    except json.JSONDecodeError:
        return None


def save_state(path: Path, state: ReauthState) -> None:
    """Atomic 0600 write; the file carries a device_code."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state.to_json(), indent=1))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# lark-cli process boundary


def find_lark_cli(env: Mapping[str, str] | None = None) -> str | None:
    environment = os.environ if env is None else env
    override = environment.get("HYPRIAL_LARK_CLI")
    if override:
        return override
    found = shutil.which("lark-cli")
    if found:
        return found
    for candidate in _LARK_CLI_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    return None


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return env


def make_cli_runner(executable: str) -> CliRunner:
    """The production runner: subprocess + JSON success payload.

    Never raises raw stderr into an exception message unbounded; token
    material is not expected on either stream, and the tail is truncated.
    """

    def run(arguments: Sequence[str], *, timeout: float) -> dict[str, Any]:
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [executable, *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_cli_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReauthError(
                "LARK_CLI_INVOCATION", f"lark-cli {' '.join(arguments[:2])}: {error}"
            ) from error
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip()[-300:]
            raise ReauthError(
                "LARK_CLI_FAILED",
                f"lark-cli {' '.join(arguments[:2])} exited "
                f"{result.returncode}: {tail}",
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReauthError(
                "LARK_CLI_OUTPUT",
                f"lark-cli {' '.join(arguments[:2])} returned non-JSON output",
            ) from error
        if not isinstance(payload, dict):
            raise ReauthError(
                "LARK_CLI_OUTPUT",
                f"lark-cli {' '.join(arguments[:2])} returned a non-object payload",
            )
        # Some auth subcommands wrap data in the ok/data envelope, some
        # (auth status) return the raw object; accept both.
        if payload.get("ok") is False:
            error = payload.get("error")
            message = (
                error.get("message") if isinstance(error, Mapping) else None
            ) or "unknown error"
            raise ReauthError("LARK_CLI_FAILED", f"lark-cli: {message}")
        data = payload.get("data")
        if payload.get("ok") is True and isinstance(data, Mapping):
            return dict(data)
        return payload

    return run


def read_auth_status(runner: CliRunner) -> dict[str, Any]:
    return runner(("auth", "status", "--json", "--verify"), timeout=_STATUS_TIMEOUT_SECONDS)


def trigger_lazy_refresh(runner: CliRunner) -> None:
    """One side-effect-free user-identity read to trigger lark-cli's lazy
    refresh (#88's proven shape).  Best effort: a missing scope on the read
    must not mask the subsequent re-verify, which itself touches the network.
    """

    try:
        runner(
            ("im", "+chat-list", "--as", "user", "--page-size", "1", "--json"),
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except ReauthError:
        pass


def initiate_device_flow(
    runner: CliRunner, domains: Sequence[str]
) -> tuple[str, str, int]:
    """``auth login --no-wait --json``: returns (device_code, url, expire_in).

    The ``device_code`` is returned to the caller exactly once, for the state
    file; it must never reach a log or notification.
    """

    arguments = ["auth", "login", "--no-wait", "--json"]
    for domain in domains:
        arguments.extend(["--domain", domain])
    payload = runner(tuple(arguments), timeout=_STATUS_TIMEOUT_SECONDS)
    device_code = payload.get("device_code") or payload.get("deviceCode")
    url = (
        payload.get("verification_url")
        or payload.get("verification_uri_complete")
        or payload.get("verificationUri")
    )
    raw_expire = (
        payload.get("expire_in")
        or payload.get("expires_in")
        or payload.get("expiresIn")
    )
    if not isinstance(device_code, str) or not device_code:
        raise ReauthError(
            "DEVICE_FLOW_OUTPUT", "auth login --no-wait returned no device_code"
        )
    if not isinstance(url, str) or not url:
        raise ReauthError(
            "DEVICE_FLOW_OUTPUT", "auth login --no-wait returned no verification URL"
        )
    try:
        expire_in = int(raw_expire)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        expire_in = 0
    return device_code, url, expire_in


def complete_device_flow(
    runner: CliRunner, device_code: str, *, remaining_seconds: float
) -> None:
    """Poll ``--device-code`` until the human authorizes or the code expires."""

    timeout = max(1.0, min(remaining_seconds, _COMPLETE_MAX_SECONDS))
    runner(("auth", "login", "--device-code", device_code, "--json"), timeout=timeout)


# ---------------------------------------------------------------------------
# notification anchor guard


def effective_notification_config(
    state_path: Path,
    *,
    notify_route: str | None,
    sender: str | None,
    env: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """Resolve the (route, sender) a check run would notify with."""

    state = load_state(state_path) or ReauthState()
    route = _resolve_config(notify_route, env, ENV_NOTIFY_ROUTE, state.notify_route)
    from_identity = _resolve_config(sender, env, ENV_SENDER, state.sender)
    return route, from_identity


def ensure_notify_anchor_canonical(
    *, state_path: Path, canonical_path: Path, route: str | None, sender: str | None
) -> None:
    """Refuse to notify from a state anchor a default ``complete`` never reads.

    The pending device_code belongs to lark-cli's *machine-global* identity,
    so its state must live at the machine-global anchor.  A check running
    under an isolated HYPRIAL_HOME that nonetheless pushes a link would mint a
    flow whose completion state sits in a throwaway directory: the human
    clicks, and no normally-run ``hyprial lark-auth complete`` can find the
    pending code.  Local-only degradation (no route/sender) is always fine --
    it cannot orphan a flow.  Anything else fails loudly, here, at mint time,
    naming both anchors and the fix, instead of leaving a comment for a
    future editor of E2E env plumbing who will never read it (#142's shape).
    """

    if not route or not sender:
        return
    if state_path.resolve() == canonical_path.resolve():
        return
    raise ReauthError(
        "REAUTH_ANCHOR_ISOLATED",
        "refusing to push an authorization link from a non-canonical state "
        f"anchor: {state_path} (canonical: {canonical_path}). A link pushed "
        "from here mints a device flow whose pending state lives in this "
        "isolated home, while a normally-run `hyprial lark-auth complete` reads "
        "the canonical anchor -- the human would click a link nobody can "
        "complete. Fix one of: run this command without HYPRIAL_HOME / "
        "HARNESS_STATE_DIR overrides, or drop --notify-route / "
        f"${ENV_NOTIFY_ROUTE} so the link degrades to local-only.",
    )


# ---------------------------------------------------------------------------
# notification text


def notification_text(url: str, expire_in: int, app_id: str | None) -> str:
    """The human-facing handoff.  Carries the URL (not a secret) and never
    the device_code or any token."""

    app_part = f"(app {app_id})" if app_id else ""
    expire_part = f"约 {max(1, expire_in // 60)} 分钟后过期" if expire_in > 0 else "会过期"
    return (
        f"[hyprial] lark-cli 用户凭据需要重新授权{app_part} —— 就差你点一下:\n"
        f"{url}\n"
        f"链接{expire_part};点击完成后我会自动收尾。"
        f"若链接已过期,下一次检查会生成新链接,无需理会本条。"
    )


def recovered_text(app_id: str | None) -> str:
    app_part = f"(app {app_id})" if app_id else ""
    return f"[hyprial] lark-cli 用户凭据已恢复{app_part},授权完成。"


# ---------------------------------------------------------------------------
# the check itself


@dataclass(slots=True)
class CheckResult:
    """What one check did.  JSON-serializable; contains no secrets -- the
    ``device_code`` is deliberately absent (it lives only in the state file).
    """

    status: CheckStatus
    app_id: str | None = None
    notified: bool = False
    notify_route: str | None = None
    verification_url: str | None = None
    expires_at: float | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.app_id is not None:
            payload["appId"] = self.app_id
        if self.status == "awaiting_user":
            payload["notified"] = self.notified
            if self.notify_route is not None:
                payload["notifyRoute"] = self.notify_route
            if self.verification_url is not None:
                payload["verificationUrl"] = self.verification_url
            if self.expires_at is not None:
                payload["expiresAt"] = self.expires_at
        if self.error is not None:
            payload["error"] = self.error
        payload.update(self.extra)
        return payload


def _resolve_config(
    value: str | None, env: Mapping[str, str], env_key: str, persisted: str | None
) -> str | None:
    """flag > environment > remembered-from-an-earlier-run."""

    if value:
        return value
    from_env = env.get(env_key)
    if from_env:
        return from_env
    return persisted


def check(
    runner: CliRunner,
    *,
    state_path: Path,
    notify_route: str | None = None,
    sender: str | None = None,
    domains: Sequence[str] = DEFAULT_DOMAINS,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    send: NotifySender | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> CheckResult:
    """One detection -> escalation -> notification pass.

    * ``ok``/``recovered``: nobody is notified, no device flow is started --
      the negative-path guarantee;
    * ``awaiting_user``: a fresh (or still-pending) link exists; a
      notification is sent at most once per ``cooldown`` per appId, and only
      when a route AND sender are configured (flag > env > remembered);
      without them the link still lands in the state file and result.
    """

    environment = os.environ if env is None else env
    import time as _time

    current = _time.time() if now is None else now

    payload = read_auth_status(runner)
    app_id = payload.get("appId") if isinstance(payload.get("appId"), str) else None
    classification = classify_user_identity(payload, now=current)
    if classification == "ok":
        return CheckResult("ok", app_id=app_id)
    if classification == "refreshable":
        trigger_lazy_refresh(runner)
        second = read_auth_status(runner)
        if classify_user_identity(second, now=current) == "ok":
            return CheckResult("recovered", app_id=app_id)
        # A permanently needs_refresh token (run 977's successor case) means
        # the unattended path is exhausted; escalate honestly.
        classification = "needs_human"

    state = load_state(state_path) or ReauthState()
    route = _resolve_config(notify_route, environment, ENV_NOTIFY_ROUTE, state.notify_route)
    from_identity = _resolve_config(sender, environment, ENV_SENDER, state.sender)

    if state.pending_valid(current):
        # A live link is already out there.  Re-notify only past cooldown;
        # never mint a second device flow while one is pending.
        notified = False
        notify_error: str | None = None
        if state.may_notify(current, cooldown) and route and from_identity and send:
            try:
                remaining = int((state.expires_at or current) - current)
                send(from_identity, route, notification_text(
                    state.verification_url or "", remaining, state.app_id
                ))
                state.notified_at = current
                notified = True
            except Exception as error:  # noqa: BLE001 - degrade to local state
                notify_error = f"{type(error).__name__}"
        # Config may have been updated by this call's flag/env.
        if route:
            state.notify_route = route
        if from_identity:
            state.sender = from_identity
        save_state(state_path, state)
        return CheckResult(
            "awaiting_user",
            app_id=state.app_id,
            notified=notified,
            notify_route=route,
            verification_url=state.verification_url,
            expires_at=state.expires_at,
            error=notify_error,
            extra={"pending": True},
        )

    device_code, url, expire_in = initiate_device_flow(runner, domains)
    state = ReauthState(
        app_id=app_id,
        device_code=device_code,
        verification_url=url,
        expires_at=current + expire_in if expire_in > 0 else None,
        notify_route=route,
        sender=from_identity,
        domains=tuple(domains),
    )
    notified = False
    notify_error = None
    if route and from_identity and send:
        try:
            send(from_identity, route, notification_text(url, expire_in, app_id))
            state.notified_at = current
            notified = True
        except Exception as error:  # noqa: BLE001 - degrade to local state
            notify_error = f"{type(error).__name__}"
    # The state file is saved even when notification fails: complete(…) can
    # still finish the flow, and the next check re-attempts notification
    # (notified_at is only set on success).
    save_state(state_path, state)
    return CheckResult(
        "awaiting_user",
        app_id=app_id,
        notified=notified,
        notify_route=route,
        verification_url=url,
        expires_at=state.expires_at,
        error=notify_error,
    )


def complete(
    runner: CliRunner,
    *,
    state_path: Path,
    send: NotifySender | None = None,
    now: float | None = None,
) -> CheckResult:
    """Finish a pending handoff after the human clicked.

    Blocking for at most the code's remaining validity.  On success the state
    is cleared and, when a route+sender were remembered, a single recovery
    notice is sent (one per recovery; it is the all-clear, not a storm).
    """

    import time as _time

    current = _time.time() if now is None else now
    state = load_state(state_path)
    if state is None or state.device_code is None:
        return CheckResult("no_pending")
    if not state.pending_valid(current):
        clear_state(state_path)
        return CheckResult("no_pending", extra={"expired": True})
    remaining = (state.expires_at or current) - current
    complete_device_flow(runner, state.device_code, remaining_seconds=remaining)
    payload = read_auth_status(runner)
    if classify_user_identity(payload, now=current) != "ok":
        raise ReauthError(
            "REAUTH_INCOMPLETE",
            "device flow completed but the user identity still is not ready",
        )
    route, from_identity = state.notify_route, state.sender
    app_id = state.app_id
    clear_state(state_path)
    if route and from_identity and send:
        try:
            send(from_identity, route, recovered_text(app_id))
        except Exception:  # noqa: BLE001 - the recovery itself already happened
            pass
    return CheckResult("authorized", app_id=app_id)
