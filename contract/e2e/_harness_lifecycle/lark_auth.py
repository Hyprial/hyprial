"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def _lookup_e2e_lark_profile(_entry: dict[str, Any], /):
    environment = dict(_entry['os'].environ)
    environment.update(
        {
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
    )
    result = _entry['subprocess'].run(
        [str(_entry['LARK_CLI']), "profile", "list"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = _entry['clip'](_entry['redact_native_ids'](_entry['one_line'](result.stderr or result.stdout)))
        raise AssertionError(
            f"lark-cli profile list failed ({result.returncode}): {detail}"
        )
    try:
        profiles = _entry['json'].loads(result.stdout)
    except _entry['json'].JSONDecodeError as error:
        raise AssertionError(
            "lark-cli returned non-JSON success output"
        ) from error
    for profile in profiles if isinstance(profiles, list) else []:
        if isinstance(profile, dict) and profile.get("appId") == _entry['LARK_APP_ID']:
            name = profile.get("name")
            if isinstance(name, str) and name:
                return name
    raise AssertionError(
        f"lark-cli has no profile for E2E app {_entry['LARK_APP_ID']}"
    )


def lark_cli(_entry: dict[str, Any], /, *arguments, cwd):
    environment = dict(_entry['os'].environ)
    environment.update(
        {
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
    )
    result = _entry['subprocess'].run(
        [str(_entry['LARK_CLI']), "--profile", _entry['e2e_lark_profile'](), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
    )
    if result.returncode != 0:
        detail = _entry['clip'](_entry['redact_native_ids'](_entry['one_line'](result.stderr or result.stdout)))
        raise AssertionError(
            f"lark-cli {' '.join(arguments[:2])} failed "
            f"({result.returncode}): {detail}"
        )
    try:
        value = _entry['json'].loads(result.stdout)
    except _entry['json'].JSONDecodeError as error:
        raise AssertionError("lark-cli returned non-JSON success output") from error
    if not isinstance(value, dict):
        raise AssertionError("lark-cli JSON output must be an object")
    if value.get("ok") is False:
        raise AssertionError(
            "lark-cli returned failure: "
            f"{_entry['clip'](_entry['redact_native_ids'](_entry['one_line'](result.stdout)))}"
        )
    return value


def _lark_user_identity(_entry: dict[str, Any], /):
    auth = _entry['lark_cli']("auth", "status", "--json", "--verify")
    identities = auth.get("identities")
    user = identities.get("user") if isinstance(identities, dict) else None
    return auth, (user if isinstance(user, dict) else {})


def _lark_user_valid(_entry: dict[str, Any], /, auth, user):
    return user.get("status") == "ready" and user.get("tokenStatus") == "valid"


def _lark_reauth_check_summary(_entry: dict[str, Any], /):
    """Best-effort: walk the lark-cli device flow to 'one click away' and
    push the link to the configured route (hyprial adapter user-auth check).  Never
    raises; the summary it returns goes into the BLOCKED message so a red
    gate says '凭据问题,链接已推给 X' instead of looking like a product
    failure."""

    executable = _entry['os'].environ.get(
        "HYPRIAL_BIN", str(_entry['Path'](_entry['__file__']).resolve().parents[2] / ".venv/bin/hyprial")
    )
    try:
        result = _entry['subprocess'].run(
            [executable, "adapter", "user-auth", "check", "--json"],
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        payload = _entry['json'].loads(result.stdout) if result.stdout.strip() else {}
    except Exception as error:  # noqa: BLE001 - preflight aid, never fatal
        return f"adapter user-auth check unavailable ({type(error).__name__})"
    if not isinstance(payload, dict):
        return "adapter user-auth check returned an unparseable result"
    if payload.get("ok") is False:
        return (
            "adapter user-auth check failed "
            f"({payload.get('code')}): {payload.get('error')}"
        )
    status = payload.get("status")
    if status == "awaiting_user":
        if payload.get("notified"):
            return f"re-authorization link pushed to {payload.get('notifyRoute')}"
        return (
            "re-authorization link generated but NOT pushed "
            f"(no route/sender configured); link: {payload.get('verificationUrl')}"
        )
    return f"adapter user-auth check status={status}"


def verify_lark_user(_entry: dict[str, Any], /):
    auth, user = _entry['_lark_user_identity']()
    if _entry['_lark_user_valid'](auth, user):
        return
    if user.get("tokenStatus") == "needs_refresh":
        # lark-cli refreshes its user token LAZILY: needs_refresh means "the
        # next API call must refresh first", not "the credential is dead" —
        # run 977 blocked all ten Lark legs on a token that recovered on its
        # own minutes later.  Trigger the lazy refresh with one
        # side-effect-free read, then re-verify once.  This wraps no action
        # under test: the preflight IS the observation, and a still-invalid
        # status after the refresh lands in the same loud BLOCKED as before.
        _entry['lark_cli'](
            "im", "+chat-list", "--as", "user", "--page-size", "1", "--json"
        )
        auth, user = _entry['_lark_user_identity']()
        if _entry['_lark_user_valid'](auth, user):
            return
    raise AssertionError(
        "lark-cli user identity is not ready "
        f"(status={user.get('status')!r} "
        f"tokenStatus={user.get('tokenStatus')!r}); "
        + _entry['_lark_reauth_check_summary']()
    )


def _bot_open_id(_entry: dict[str, Any], /, app_id, app_secret):
    """The test App bot's own open_id, the p2p DM match key (E2E-004 parity)."""

    import urllib.request

    token_request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=_entry['json'].dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(token_request, timeout=15) as response:
        token = _entry['json'].loads(response.read().decode(), strict=False).get(
            "tenant_access_token"
        )
    if not token:
        raise AssertionError("approved E2E App could not mint a tenant token")
    info_request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(info_request, timeout=15) as response:
        bot = _entry['json'].loads(response.read().decode(), strict=False).get("bot") or {}
    open_id = bot.get("open_id")
    if not isinstance(open_id, str) or not open_id:
        raise AssertionError("approved E2E App bot open_id is unavailable")
    return open_id


def resolve_bot_dm_chat_id(_entry: dict[str, Any], /):
    """The caller's own p2p DM with the test App's bot, resolved by open_id.

    Same contract as E2E-004: a DM chat id is a per-developer private value
    and is never committed; a p2p entry's `p2p_target_id` is the peer's
    open_id, so the DM is the chat whose `p2p_target_id` equals the test
    App bot's open_id -- the bot's display name is arbitrary and never
    consulted -- and exactly one candidate must remain.
    """

    app_secret = _entry['load_test_app_secret']()
    if not app_secret:
        raise AssertionError(
            f"approved test-App credential is unavailable: {_entry['LARK_SECRET_PATH']}"
        )
    bot_open_id = _entry['_bot_open_id'](_entry['LARK_APP_ID'], app_secret)
    chats: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(10):
        arguments = [
            "im", "+chat-list", "--as", "user", "--types", "p2p",
            "--page-size", "100", "--json",
        ]
        if page_token:
            arguments.extend(["--page-token", page_token])
        data = _entry['lark_cli'](*arguments).get("data")
        if not isinstance(data, dict):
            break
        page = data.get("chats") or data.get("items") or []
        chats.extend(item for item in page if isinstance(item, dict))
        page_token = data.get("page_token") if data.get("has_more") else None
        if not page_token:
            break
    matches = [
        chat["chat_id"]
        for chat in chats
        if chat.get("chat_mode") == "p2p"
        and chat.get("p2p_target_id") == bot_open_id
        and isinstance(chat.get("chat_id"), str)
        and chat["chat_id"]
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"need exactly one p2p DM with {_entry['LARK_APP_NAME']!r} "
            f"(found {len(matches)} among {len(chats)} p2p chats); open a DM "
            f"with the App's bot once from Feishu, then re-run"
        )
    return matches[0]


def load_test_app_secret(_entry: dict[str, Any], /):
    """Test-App secret: env injection (CI) first, production file second.

    A partially-set or foreign injected id raises ValueError so the caller
    reports a loud BLOCKED instead of falling back to another credential
    source; the check runs before any file access.
    """

    app_id = _entry['os'].environ.get(_entry['LARK_APP_ID_ENV_VAR'], "").strip()
    app_secret = _entry['os'].environ.get(_entry['LARK_APP_SECRET_ENV_VAR'], "").strip()
    if app_id or app_secret:
        if not app_id or not app_secret:
            raise ValueError(
                f"{_entry['LARK_APP_ID_ENV_VAR']} and {_entry['LARK_APP_SECRET_ENV_VAR']} "
                "must be set together"
            )
        if app_id != _entry['LARK_APP_ID']:
            raise ValueError(
                f"injected {_entry['LARK_APP_ID_ENV_VAR']} does not match the approved "
                "E2E test App"
            )
        return app_secret
    try:
        value = _entry['json'].loads(_entry['LARK_SECRET_PATH'].read_text(encoding="utf-8"))
    except (OSError, _entry['json'].JSONDecodeError):
        return None
    secret = value.get("appSecret") if isinstance(value, dict) else None
    return secret if isinstance(secret, str) and secret else None


def preflight_lark(_entry: dict[str, Any], /, report):
    """Return (test-App secret, live p2p chat id), else None."""

    if not _entry['LARK_CLI'].is_file() or not _entry['os'].access(_entry['LARK_CLI'], _entry['os'].X_OK):
        report.add(
            "BLOCKED", "preflight.lark", f"lark-cli is missing: {_entry['LARK_CLI']}"
        )
        return None
    secret: str | None
    try:
        secret = _entry['load_test_app_secret']()
    except ValueError as error:
        report.add("BLOCKED", "preflight.lark", str(error))
        return None
    if secret is None:
        report.add(
            "BLOCKED",
            "preflight.lark",
            f"approved test-App credential is unavailable: {_entry['LARK_SECRET_PATH']}",
        )
        return None
    try:
        _entry['verify_lark_user']()
        chat_id = _entry['resolve_bot_dm_chat_id']()
    except (AssertionError, OSError, _entry['subprocess'].TimeoutExpired) as error:
        report.add("BLOCKED", "preflight.lark", str(error))
        return None
    report.add(
        "PASS",
        "preflight.lark",
        f"user identity verified; test App {_entry['LARK_APP_NAME']} credential present; "
        f"p2p route resolved to {chat_id}",
    )
    return secret, chat_id
