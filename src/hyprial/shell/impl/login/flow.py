"""``hyprial login`` — the identity stage (login U2): OIDC device authorization
against ``profile.issuer``, landing ``settings.owner``.

Nine steps, in this order (spec §1.2; design §2.2) — the order is the
contract, each step has a test:

1. ``resolve_profile`` happens in the CLI; here discovery is read from
   ``{profile.issuer}/.well-known/openid-configuration`` and discovery's
   ``issuer`` must equal ``profile.issuer`` **byte for byte** — one
   character off (a trailing slash, another host) terminates the whole
   command with nothing written (T1).
2. ``device_authorization_endpoint`` / ``token_endpoint`` /
   ``userinfo_endpoint`` must share the issuer's origin (scheme+host+port)
   (T2).  ``verification_uri`` is deliberately *not* origin-checked: it is
   where the human's browser goes, and Casdoor serves it from the frontend
   origin, which may differ from the API origin the endpoints live on.
3. Device authorization with ``client_id = profile.client_id`` (public, per
   D-U2-1) and ``scope = "openid profile offline_access"`` — no ``email``,
   the owner is never taken from it (D1) (T3).
4. The verification URI + user code are printed (or the browser is opened);
   polling rhythm uses **only** the server's ``interval``/``expires_in`` —
   no local constants shape the wait; ``slow_down`` adds 5s per RFC 8628
   §3.5 (T4).
5. The token endpoint's device-grant error states are each handled:
   ``authorization_pending`` continues, ``slow_down`` widens the interval,
   ``access_denied``/``expired_token``/``invalid_client`` terminate (T5).
6. UserInfo is called with the access token as an opaque bearer (D-U2-2:
   **no JOSE dependency, no signature check** — identity truth is the
   TLS-delivered, issuer-equal, same-origin UserInfo; the accepted risk is
   that forging it requires breaking TLS or the issuer itself); the owner
   candidate is the non-empty ``preferred_username``; ``sub`` is recorded
   but never used as identity (D1) (T6).
7. The candidate passes the owner grammar (non-empty, no ``:``); a
   non-empty **different** ``HYPRIAL_OWNER`` aborts before any write (D7) (T7).
8. A different existing ``settings.owner`` is handled by the account
   switch semantics (U5, D6/D-U5-1): without ``--switch-account`` the
   login is still **detected and refused** with zero writes
   (``OWNER_MISMATCH``).  With the flag, the daemon for this home must
   be **stopped** — checked read-only via the ``.active_daemon``
   heartbeat (``daemon/home_guard.live_daemon_pid``); a fresh heartbeat
   refuses with ``DAEMON_RUNNING`` and names the stop command, because
   login does not orchestrate daemon lifecycle (D-U5-1).  Stopped, a TTY
   asks the operator to type ``yes`` over an explicit ``old -> new``
   line; under ``--json`` the flag itself is the confirmation (T8).
9. Credential first, owner second — the one-way commit of design §2.2
   step 8: ``$HYPRIAL_HOME/secrets/login.json`` (0600, only
   ``{version, issuer, refreshToken, obtainedAt}``, written via the
   validate-then-write + restore-original-bytes pattern of
   ``adapter_registration``), then ``write_settings_owner``.  A failure of
   the owner write leaves the credential committed — ⚠️ deliberately:
   design §2.2 has **no cross-store transaction** and **no recovery state
   machine**; the command errors and a re-run of the same login completes
   idempotently (same-value second run leaves ``settings.json`` bytes
   unchanged) (T9).

Token landing (design §2.4 / §10 U2: ``secrets/``, not an OS keychain):

===============  ==============================  ====================
data             landing spot                    offline daemon reads
===============  ==============================  ====================
settings.owner   settings.json                  yes (unchanged)
refresh token    $HYPRIAL_HOME/secrets/login.json   **no**
access token     this process's memory          no
device code      this command's memory          no
===============  ==============================  ====================

⛔ **T11 anchor — the daemon package must not import this module and must
not read ``secrets/login.json``.**  ``tests/test_login.py`` enforces this
three ways (grep over ``src/hyprial/daemon/**``, this docstring's anchor, and a
settings-only daemon-identity resolution with no network).  Offline, the
daemon's identity source stays exactly ``HYPRIAL_OWNER > settings.owner >
error`` (``daemon/identity.py``); a token is never an identity source and
never a fallback guess (design §2.4).

Other tripwires honored here:

- The default issuer's domain literal never appears in this file — every
  URL comes from ``profile.issuer`` (U1 T9 / U2 T13).
- HTTP is stdlib ``urllib.request`` only; TLS uses the system CA store.
- Nothing is logged.  There is no log sink in this command on purpose:
  the log redaction boundary (``log/__init__.py``) exists for components
  that must log *around* secrets; login simply never emits them (T10
  scans stdout, stderr, and any log files under the home).
- No ``--provider``: network control-plane selection lives in
  ``profile.controlPlane.kind`` (D-U2-3, design §9.6).

This module is deliberately free of Typer/CLI concerns: the CLI (``cli.py``)
resolves the profile, renders output, and maps :class:`LoginError` onto the
structured CLI error surface.
"""

from __future__ import annotations

import json
import os
import time
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from hyprial.kernel import ipc_errors
from hyprial.kernel import is_identity_id_segment
from hyprial.daemon import (
    read_settings_identity,
    write_settings_identity,
    write_settings_owner,
)
from hyprial.kernel import configured_hyprial_home
from hyprial.daemon import SECRETS_DIRNAME, NetworkProfile
from hyprial.kernel import atomic_json_write
from hyprial.shell.impl.login.oidc import (
    LOGIN_SCOPE,
    REFRESH_GRANT_TYPE,
    USER_AGENT,
    DeviceAuthorization,
    LoginError,
    OidcEndpoints,
    _fetch_endpoints,
    _fetch_userinfo,
    _poll_device_token,
    _request_device_authorization,
    _request_json,
    _token_error,
)

__all__ = [
    "CREDENTIAL_VERSION",
    "LOGIN_FILENAME",
    "LOGIN_SCOPE",
    "DeviceAuthorization",
    "LoginCredential",
    "LoginError",
    "LoginResult",
    "OidcEndpoints",
    "USER_AGENT",
    "credential_path",
    "read_login_credential",
    "refresh_access_token",
    "run_login",
]

LOGIN_FILENAME = "login.json"
"""Basename of the credential record inside ``$HYPRIAL_HOME/secrets/``."""

CREDENTIAL_VERSION = 1
"""The only login-credential schema version this module reads or writes."""

# LOGIN_SCOPE / USER_AGENT / transport timeouts live in oidc.py with the
# protocol code that owns them; imported above and re-exported via __all__.
# LOGIN_SCOPE: "openid profile offline_access" -- identity + username, no email
# (D1).  USER_AGENT: urllib's default is HTTP-403'd by the issuer's Cloudflare
# edge (U2b).  Polling rhythm follows only the server's interval/expires_in
# (spec §1.2 step 4); RFC 8628 §3.5 slow_down adds 5s.

_CREDENTIAL_KEYS = frozenset({"version", "issuer", "refreshToken", "obtainedAt"})

EmitFn = Callable[[str, dict[str, Any]], None]


# -- records -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginCredential:
    """The whole content of ``secrets/login.json`` — refresh token only.

    ⚠️ The access token never reaches this record (T10: writing it is a
    mutation-red).  ``issuer`` scopes the credential to one profile so a
    later profile switch cannot silently reuse another issuer's token.
    """

    version: int
    issuer: str
    refresh_token: str
    obtained_at: str

    def as_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "issuer": self.issuer,
            "refreshToken": self.refresh_token,
            "obtainedAt": self.obtained_at,
        }


@dataclass(frozen=True, slots=True)
class LoginResult:
    """What a successful identity stage established (no secrets).

    ``switched`` is true only when the login replaced a *different* existing
    ``settings.owner`` (``--switch-account``, daemon stopped, confirmed);
    ``previous_owner`` carries the value that was replaced — ``None`` for
    both the first-ever login and the idempotent same-owner re-login.
    """

    owner: str
    subject: str | None
    issuer: str
    profile_source: str
    verification_uri: str
    user_code: str
    settings_path: Path
    credential_path: Path
    switched: bool = False
    previous_owner: str | None = None
    dry_run: bool = False
    migration_preview: dict[str, Any] | None = None


# -- paths and credential I/O -------------------------------------------------


def credential_path(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> Path:
    """``$HYPRIAL_HOME/secrets/login.json`` under the same home rule as
    ``settings.json``/``profile.json`` (explicit home > ``HYPRIAL_HOME`` >
    ``~/.hyprial``) — all three files must always resolve from one home."""

    env = os.environ if environ is None else environ
    if hyprial_home is not None:
        home = Path(hyprial_home)
    else:
        home = configured_hyprial_home(env)[0]
    return home / SECRETS_DIRNAME / LOGIN_FILENAME


def read_login_credential(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> LoginCredential:
    """Load and validate ``secrets/login.json``; loud on every defect.

    A missing credential is its own loud situation (``NOT_LOGGED_IN``): the
    remedy is running ``hyprial login``, not guessing an identity.
    """

    path = credential_path(environ, hyprial_home=hyprial_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise LoginError(
            "NOT_LOGGED_IN",
            f"no login credential at {path}: run hyprial login first",
        ) from error
    except OSError as error:
        raise LoginError(
            "CREDENTIAL_UNREADABLE", f"cannot read {path}: {error}"
        ) from error
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LoginError(
            "CREDENTIAL_UNPARSEABLE", f"cannot parse {path}: {error}"
        ) from error
    if not isinstance(record, dict):
        raise LoginError(
            "CREDENTIAL_UNPARSEABLE", f"{path}: top level is not an object"
        )
    unknown = sorted(set(record) - _CREDENTIAL_KEYS)
    if unknown:
        raise LoginError(
            "CREDENTIAL_UNPARSEABLE",
            f"{path}: unknown key(s) {unknown}; the credential is a small "
            "closed record",
        )
    if record.get("version") != CREDENTIAL_VERSION:
        raise LoginError(
            "CREDENTIAL_UNPARSEABLE",
            f"{path}: version must be {CREDENTIAL_VERSION}; "
            f"got {record.get('version')!r}",
        )
    issuer = record.get("issuer")
    refresh = record.get("refreshToken")
    obtained = record.get("obtainedAt")
    for name, value in (
        ("issuer", issuer),
        ("refreshToken", refresh),
        ("obtainedAt", obtained),
    ):
        if not isinstance(value, str) or not value:
            raise LoginError(
                "CREDENTIAL_UNPARSEABLE",
                f"{path}: {name} must be a non-empty string; got {value!r}",
            )
    return LoginCredential(
        version=CREDENTIAL_VERSION,
        issuer=issuer,
        refresh_token=refresh,
        obtained_at=obtained,
    )


def _write_credential(credential: LoginCredential, path: Path) -> None:
    """Validate-then-write the credential, restoring prior bytes on failure.

    Mirrors ``adapter_registration``: the record is serialized and
    round-trip-parsed **before** the filesystem is touched, and the write is
    verified byte-for-byte afterwards; any failure puts the file back to its
    original bytes (or removes a brand-new one) so a failed write is a no-op
    rather than a half-written credential.  ``atomic_json_write`` gives the
    0600 mode and the temp-file + ``os.replace`` atomicity.
    """

    payload = _record_bytes(credential.as_record())
    try:
        reparsed = json.loads(payload)
    except json.JSONDecodeError as error:  # pragma: no cover - belt
        raise LoginError(
            "CREDENTIAL_INVALID", f"credential record failed round-trip: {error}"
        ) from error
    if reparsed != credential.as_record():  # pragma: no cover - belt
        raise LoginError("CREDENTIAL_INVALID", "credential round-trip mismatch")
    backup = path.read_bytes() if path.exists() else None
    try:
        atomic_json_write(path, reparsed)
        if path.read_bytes() != payload:
            raise LoginError(
                "CREDENTIAL_WRITE_CONFLICT",
                f"credential changed during login write: {path.name}",
            )
    except BaseException:
        current = path.read_bytes() if path.exists() else None
        if current == payload:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(backup)
                os.chmod(path, 0o600)
        raise


def _record_bytes(record: Mapping[str, Any]) -> bytes:
    """Serialize exactly the way ``atomic_json_write`` does (2-space indent,
    sorted keys, trailing newline) so verify-after-write compares equal."""

    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


# -- HTTP (stdlib only) --------------------------------------------------------




# -- owner checks and the one-way commit ------------------------------------------


def _home_dir(environ: Mapping[str, str], hyprial_home: Path | None) -> Path:
    """The home every login landing spot resolves from — one rule for
    ``settings.json``, ``secrets/login.json`` and the ``.active_daemon``
    heartbeat alike (explicit ``hyprial_home`` > ``HYPRIAL_HOME`` > ``~/.hyprial``)."""

    if hyprial_home is not None:
        return Path(hyprial_home)
    env = os.environ if environ is None else environ
    return configured_hyprial_home(env)[0]


def _default_prompt(message: str) -> str:
    """Ask the operator at a TTY; EOF or a closed stdin counts as ``no``."""

    try:
        return input(message)
    except EOFError:
        return ""


def _check_owner_gates(
    owner: str,
    *,
    environ: Mapping[str, str],
    hyprial_home: Path | None,
    switch_account: bool,
    assume_yes: bool = False,
    prompt: Callable[[str], str] | None = None,
    allow_running: bool = False,
    target_mode: str | None = None,
    target_issuer: str | None = None,
) -> tuple[str | None, tuple[str, str | None, str | None] | None]:
    """Steps 7-8: grammar, HYPRIAL_OWNER override, existing-owner gate.

    Returns ``(previous_owner, observed_identity)``.  The observation is
    carried into the transaction callback so a second concurrent login cannot
    commit from a stale pre-lock decision.  Refusal here happens before writes.
    """

    if not owner or ":" in owner or is_identity_id_segment(owner.lower()):
        raise LoginError(
            "OWNER_INVALID",
            "login identity must be non-empty, contain no ':', and not have "
            f"an exact identity id shape; got {owner!r}",
        )
    override = (environ.get("HYPRIAL_OWNER") or "").strip()
    if override and override != owner:
        # D7: the override outranks settings for the daemon, so writing a
        # different owner to settings would change nothing real.
        raise LoginError(
            "OWNER_OVERRIDE_CONFLICT",
            f"HYPRIAL_OWNER is {override!r} but login identity is {owner!r}; "
            "writing settings would not change the daemon's identity — "
            "fix or unset the override first",
            data={"override": override, "loginOwner": owner},
        )
    existing_identity = read_settings_identity(
        environ=environ, hyprial_home=hyprial_home
    )
    existing = existing_identity[0] if existing_identity is not None else None
    if existing is None:
        return None, None
    same_identity = (
        existing == owner
        if target_mode is None
        else existing_identity == (owner, target_mode, target_issuer)
    )
    if same_identity:
        return None, existing_identity
    owner_changes = existing != owner
    if owner_changes and not switch_account:
        # U2 behavior, unchanged: a different account is detected and
        # refused with zero writes; the error names the explicit opt-in.
        raise LoginError(
            "OWNER_MISMATCH",
            f"settings.owner is {existing!r} but login identity is {owner!r} "
            f"({existing} -> {owner}); re-run with --switch-account to "
            "switch accounts (the daemon must be stopped for a switch)",
            data={
                "currentOwner": existing,
                "loginOwner": owner,
                "flag": "--switch-account",
            },
        )
    # A legacy same-owner record can gain mode/issuer without pretending the
    # account changed.  It still may not split a live daemon in --no-daemon
    # mode; coordinated login is allowed to take the transaction and restart.
    from hyprial.daemon import live_daemon_pid

    pid = live_daemon_pid(_home_dir(environ, hyprial_home))
    if pid is not None and not allow_running:
        raise LoginError(
            "DAEMON_RUNNING",
            f"cannot change the persisted identity for {owner!r} while this "
            f"home's daemon is running (pid {pid}): its in-memory identity "
            "would split from disk. Stop it first — `hyprial daemon stop`, "
            "or omit --no-daemon so login can orchestrate the restart",
            data={
                "pid": pid,
                "currentOwner": existing,
                "loginOwner": owner,
            },
        )
    if owner_changes and not assume_yes:
        ask = _default_prompt if prompt is None else prompt
        answer = ask(
            f"switch this home's account: settings.owner {existing!r} -> "
            f"{owner!r}\ntype yes to continue: "
        )
        if answer.strip().lower() != "yes":
            raise LoginError(
                "SWITCH_DECLINED",
                f"switch of settings.owner {existing!r} -> {owner!r} was not "
                "confirmed; nothing was written",
                data={"currentOwner": existing, "loginOwner": owner},
            )
    return (existing if owner_changes else None), existing_identity


def _default_open(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - any webbrowser failure degrades to print
        return False


# -- the identity stage ------------------------------------------------------------


def run_login(
    profile: NetworkProfile,
    *,
    profile_source: str,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
    switch_account: bool = False,
    dry_run: bool = False,
    state_dir: Path | None = None,
    identity_mode: str | None = None,
    identity_issuer: str | None = None,
    orchestrate_daemon: bool = False,
    before_commit: Callable[
        [str | None, str, tuple[str, str | None, str | None] | None], None
    ]
    | None = None,
    assume_yes: bool = False,
    prompt: Callable[[str], str] | None = None,
    open_browser: bool = True,
    emit: EmitFn | None = None,
    open_url: Callable[[str], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    write_owner: Callable[..., Path] | None = None,
) -> LoginResult:
    """Run the nine-step identity stage; returns a :class:`LoginResult`.

    The caller (CLI) resolves the profile — ``profile``/``profile_source``
    come from ``resolve_profile`` there; tests inject an explicit profile so
    the in-repo fake OIDC (plain http) can stand in for an issuer.
    ``emit`` receives progress events (``device``, ``authenticated``,
    ``committed``) carrying **no secrets** — the device code and both tokens
    never leave this function except into the credential file.
    ``assume_yes`` skips the interactive ``old -> new`` confirmation of an
    account switch — the CLI sets it for ``--json``, where the flag itself
    is the confirmation (spec §1.2 step 3); ``prompt`` is the test seam for
    the same question.
    """

    env = dict(os.environ if environ is None else environ)
    if dry_run and not switch_account:
        raise LoginError(
            ipc_errors.INVALID_ARGUMENT,
            "--dry-run is valid only with --switch-account",
            data={"requiredFlag": "--switch-account"},
        )
    sleep = time.sleep if sleep is None else sleep
    clock = time.monotonic if clock is None else clock
    now = now or (lambda: datetime.now(UTC))
    open_url = _default_open if open_url is None else open_url
    if write_owner is None:
        if identity_mode is None:
            write_owner = write_settings_owner
        else:
            def identity_writer(value: str, **kwargs: Any) -> Path:
                return write_settings_identity(
                    value,
                    mode=identity_mode,
                    issuer=identity_issuer,
                    **kwargs,
                )

            write_owner = identity_writer

    def notify(kind: str, data: dict[str, Any]) -> None:
        if emit is not None:
            emit(kind, data)

    # Steps 1-2: discovery, issuer byte-equality, same-origin endpoints.
    endpoints = _fetch_endpoints(profile)

    # Step 3: device authorization (public client id, scope without email).
    authorization = _request_device_authorization(endpoints, profile)

    # Step 4: show the human where to go; --no-open only skips the browser.
    uri = authorization.verification_uri_complete or authorization.verification_uri
    notify(
        "device",
        {
            "verificationUri": uri,
            "userCode": authorization.user_code,
            "interval": authorization.interval,
            "expiresIn": authorization.expires_in,
        },
    )
    if open_browser and not open_url(uri):
        pass  # degraded to the printed URI above — same surface as --no-open

    # Step 5: poll with the server's rhythm only.
    access_token, refresh_token = _poll_device_token(
        endpoints, profile, authorization, sleep=sleep, clock=clock
    )

    # Step 6: userinfo; owner = preferred_username, sub is recorded only.
    owner, subject, lark_union_id = _fetch_userinfo(endpoints, access_token)
    notify("authenticated", {"owner": owner, "issuer": profile.issuer})

    # Steps 7-8: grammar, override conflict, existing-owner gate.  A
    # confirmed switch returns the previous owner; everything else passes
    # with None and the write below is the idempotent same-owner landing.
    previous_owner, observed_identity = _check_owner_gates(
        owner,
        environ=env,
        hyprial_home=hyprial_home,
        switch_account=switch_account,
        assume_yes=assume_yes,
        prompt=prompt,
        allow_running=dry_run or orchestrate_daemon,
        target_mode=identity_mode,
        target_issuer=identity_issuer,
    )

    home = _home_dir(env, hyprial_home)
    if dry_run:
        from hyprial.shell.impl.login.preview import LoginPreviewError, preview_owner_migration

        resolved_state = (
            Path(state_dir)
            if state_dir is not None
            else Path(env["HARNESS_STATE_DIR"])
            if env.get("HARNESS_STATE_DIR")
            else home / "state"
        )
        try:
            preview = preview_owner_migration(
                state_dir=resolved_state,
                hyprial_home=home,
                target_owner=owner,
            )
        except LoginPreviewError as error:
            data = error.data
            if error.code == "PREVIEW_SOURCE_CHANGED":
                # A live daemon's writers can keep changing the migration
                # sources mid-snapshot, so the refusal names it when true.
                # The two S2 remedies stay (retry later; stop it yourself and
                # re-run the dry-run), and now that this surface carries the
                # S3 orchestrated switch, the hint also names it: it stops
                # the old generation, proves exit, re-runs the stopped
                # replay, commits, and starts the verified replacement.
                from hyprial.daemon import live_daemon_pid

                if live_daemon_pid(home) is not None:
                    data = {
                        **error.data,
                        "daemonRunning": True,
                        "nextStep": (
                            "the daemon is running and the migration sources "
                            "keep changing: retry later, stop it first "
                            "(`hyprial daemon stop`) and re-run the dry-run, "
                            "or run `hyprial login --switch-account` to stop "
                            "the daemon, prove its exit, and complete the "
                            "verified switch"
                        ),
                    }
            raise LoginError(error.code, str(error), data=data) from error
        projection = preview.as_dict()
        notify("preview", projection)
        if not preview.ready:
            raise LoginError(
                "MIGRATION_PREVIEW_BLOCKED",
                "migration preview found unclassified owner-bearing values; "
                "identity and daemon were not changed",
                data={"migration": projection},
            )
        return LoginResult(
            owner=owner,
            subject=subject,
            issuer=profile.issuer,
            profile_source=profile_source,
            verification_uri=uri,
            user_code=authorization.user_code,
            settings_path=home / "settings.json",
            credential_path=home / SECRETS_DIRNAME / LOGIN_FILENAME,
            switched=previous_owner is not None,
            previous_owner=previous_owner,
            dry_run=True,
            migration_preview=projection,
        )

    if before_commit is not None:
        before_commit(previous_owner, owner, observed_identity)

    # Step 9: credential first, owner second — one-way commit (design §2.2
    # step 8): if the owner write fails the credential stays and a re-run
    # completes; there is deliberately no rollback of the credential here.
    target = home / SECRETS_DIRNAME / LOGIN_FILENAME
    credential = LoginCredential(
        version=CREDENTIAL_VERSION,
        issuer=profile.issuer,
        refresh_token=refresh_token,
        obtained_at=now().isoformat(),
    )
    _write_credential(credential, target)
    try:
        write_owner(
            owner,
            user_id=subject,
            username=owner,
            lark_union_id=lark_union_id,
            environ=env,
            hyprial_home=home,
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary maps it
        raise LoginError(
            "OWNER_WRITE_FAILED",
            f"credential written to {target} but settings.owner could not "
            f"be written: {error}; re-run hyprial login to complete",
            data={"credentialPath": str(target)},
        ) from error
    settings = home / "settings.json"
    notify(
        "committed",
        {"owner": owner, "settingsPath": str(settings), "credentialPath": str(target)},
    )
    return LoginResult(
        owner=owner,
        subject=subject,
        issuer=profile.issuer,
        profile_source=profile_source,
        verification_uri=uri,
        user_code=authorization.user_code,
        settings_path=settings,
        credential_path=target,
        switched=previous_owner is not None,
        previous_owner=previous_owner,
    )


# -- refresh (U3's enrollment interface) ---------------------------------------------


def refresh_access_token(
    profile: NetworkProfile,
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
) -> str:
    """Exchange the stored refresh token for a fresh access token.

    U3's enrollment interface (spec §1.3): read ``secrets/login.json`` →
    token endpoint with ``grant_type=refresh_token`` → if the server rotated
    the refresh token, atomically replace the credential file.  Any failure
    raises with the remedy "re-run hyprial login" — ⛔ no fallback identity
    guessing, and the file is never touched on failure (T12).
    """

    env = os.environ if environ is None else environ
    now = now or (lambda: datetime.now(UTC))
    credential = read_login_credential(env, hyprial_home=hyprial_home)
    if credential.issuer != profile.issuer:
        raise LoginError(
            "CREDENTIAL_ISSUER_MISMATCH",
            f"the stored credential belongs to issuer {credential.issuer!r} "
            f"but this profile's issuer is {profile.issuer!r}; re-run "
            "hyprial login for this profile",
            data={"credentialIssuer": credential.issuer, "issuer": profile.issuer},
        )
    endpoints = _fetch_endpoints(profile)
    status, payload = _request_json(
        endpoints.token_endpoint,
        data={
            "grant_type": REFRESH_GRANT_TYPE,
            "client_id": profile.client_id,
            "refresh_token": credential.refresh_token,
        },
    )
    error = _token_error(status, payload, where="refresh token")
    if error is not None:
        raise LoginError(
            "REFRESH_FAILED",
            f"refreshing the access token failed ({error}); re-run hyprial login",
            data=error.data,
        )
    if not isinstance(payload, dict):
        raise LoginError("REFRESH_FAILED", "refresh response invalid")
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise LoginError(
            "REFRESH_FAILED",
            "refresh response has no usable access_token; re-run hyprial login",
        )
    rotated = payload.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != credential.refresh_token:
        _write_credential(
            LoginCredential(
                version=CREDENTIAL_VERSION,
                issuer=profile.issuer,
                refresh_token=rotated,
                obtained_at=now().isoformat(),
            ),
            credential_path(env, hyprial_home=hyprial_home),
        )
    return access
