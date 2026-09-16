"""Lark App credential resolution and official device-authorization onboarding.

This is the Python port of the TypeScript ``src/channels/lark-onboarding.ts``
seam.  It answers one question -- *which App credential should this gateway
use?* -- with three ordered outcomes:

1. ``HYPRIAL_LARK_APP_ID``/``HYPRIAL_LARK_APP_SECRET`` are both set (and the caller did
   not demand a brand-new App): use them verbatim and touch no network.
2. The caller cannot prompt a human (``--json`` or a non-TTY stdin): raise
   :class:`UserActionRequiredError` carrying the exact command to rerun and the
   exact environment variables to set.  The device-authorization flow *requires*
   a person, so failing with instructions is the only honest answer.
3. Otherwise run the official SDK's device-authorization flow, which mints a
   verification URL/QR code that a human must open, and returns the created
   App's ``client_id``/``client_secret``.

The App is declared with the minimal scope set the gateway needs plus the
``im.message.receive_v1`` event, so a freshly created App can receive and reply
to messages without a second console visit.  Nothing here logs, returns, or
formats the App secret; it flows straight to the caller for storage at 0600.

:func:`reauthorize_lark_app` re-runs the same flow against an App that already
exists.  Because the ``addons`` declaration rides along with the authorization
request, one human confirmation both *declares* and *authorizes* the listed
scopes -- the only path that reaches a scope the App never declared, which the
``scopes/apply`` endpoint structurally cannot request.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .scopes import LARK_GATEWAY_CAPABILITIES, capability_scopes

APP_ID_ENV_VAR = "HYPRIAL_LARK_APP_ID"
APP_SECRET_ENV_VAR = "HYPRIAL_LARK_APP_SECRET"

# Minimal tenant scopes requested when the onboarding flow creates an App.
# Every ``requirement == "required"`` capability in
# :data:`hyprial.adapters.lark.scopes.LARK_GATEWAY_CAPABILITIES` must be satisfiable
# by this set; ``tests/test_lark_onboarding.py`` asserts that invariant rather
# than trusting the two lists to drift together.
LARK_ONBOARDING_REQUIRED_SCOPES: tuple[str, ...] = (
    "im:message",
    "im:message.group_at_msg:readonly",
)

# The single event the inbound worker subscribes to over the SDK websocket
# (see :class:`hyprial.adapters.lark.sdk.LarkEventStream`).
LARK_ONBOARDING_REQUIRED_EVENTS: tuple[str, ...] = ("im.message.receive_v1",)

APP_PRESET_DESCRIPTION = "Harness Bridge Feishu gateway"


class LarkOnboardingError(RuntimeError):
    """The official registration flow returned an unusable result."""


class UserActionRequiredError(RuntimeError):
    """A human must act before onboarding can proceed.

    ``action`` is a JSON-serializable description of *what* the human should do,
    so a machine caller (``--json``) can surface a concrete next command instead
    of a prose dead end.
    """

    code = "USER_ACTION_REQUIRED"

    def __init__(self, message: str, action: dict[str, Any]) -> None:
        super().__init__(message)
        self.action = action


@dataclass(frozen=True, slots=True)
class VerificationPrompt:
    """The device-authorization handoff a human must complete.

    ``expire_in`` is seconds, and mirrors the official SDK's snake_case payload
    key (the TypeScript SDK spells the same field ``expireIn``).
    """

    url: str
    expire_in: int


@dataclass(frozen=True, slots=True)
class LarkAppCredential:
    app_id: str
    app_secret: str


def onboarding_addons() -> dict[str, Any]:
    """The scope/event declaration handed to the official registration flow.

    Returned fresh each call so a caller mutating the dict cannot corrupt the
    next registration.
    """

    return {
        "scopes": {"tenant": list(LARK_ONBOARDING_REQUIRED_SCOPES)},
        "events": {"items": {"tenant": list(LARK_ONBOARDING_REQUIRED_EVENTS)}},
    }


def authorization_addons(capabilities: Iterable[str] = ()) -> dict[str, Any]:
    """The scope/event declaration for re-authorizing an *existing* App.

    Same shape as :func:`onboarding_addons`, but the scope list is derived from
    gateway capabilities so a caller can request more than the App-creation
    minimum -- that is the whole point of running the flow a second time.  The
    event declaration is unchanged: the inbound worker subscribes to exactly
    one event whether the App is new or old.
    """

    return {
        "scopes": {"tenant": list(capability_scopes(capabilities))},
        "events": {"items": {"tenant": list(LARK_ONBOARDING_REQUIRED_EVENTS)}},
    }


def onboarding_app_preset(name: str) -> dict[str, str]:
    """Pre-fill values for the web App-creation page (never authoritative)."""

    return {"name": f"Harness Bridge {name}", "desc": APP_PRESET_DESCRIPTION}


def required_capability_scopes_are_covered() -> bool:
    """Every required gateway capability is satisfiable by the onboarding set."""

    selected = set(LARK_ONBOARDING_REQUIRED_SCOPES)
    for capability in LARK_GATEWAY_CAPABILITIES:
        if capability.requirement != "required":
            continue
        if not all(
            selected.intersection(group) for group in capability.scope_groups
        ):
            return False
    return True


def rerun_interactively_action(name: str) -> dict[str, Any]:
    """The structured recovery published when no human can be prompted."""

    return {
        "type": "rerun_interactively_or_set_environment",
        "command": f"hyprial adapter onboard {name}",
        "environment": [APP_ID_ENV_VAR, APP_SECRET_ENV_VAR],
        "reason": (
            "Creating a Lark App uses the official device-authorization flow, "
            "which requires a human to open a verification URL or scan a QR "
            "code; it cannot run unattended."
        ),
    }


def _verification_callback(
    on_verification_url: Callable[[VerificationPrompt], None],
) -> Callable[[Mapping[str, Any]], None]:
    """Adapt the SDK's raw ``on_qr_code`` payload to :class:`VerificationPrompt`."""

    def on_qr_code(info: Mapping[str, Any]) -> None:
        raw_expire = info.get("expire_in")
        try:
            expire_in = int(raw_expire)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            expire_in = 0
        url = info.get("url")
        if not isinstance(url, str) or not url:
            raise LarkOnboardingError(
                "the registration flow produced no verification URL"
            )
        on_verification_url(VerificationPrompt(url=url, expire_in=expire_in))

    return on_qr_code


def reauthorize_lark_app(
    *,
    app_id: str,
    register_app: Callable[..., Mapping[str, Any]],
    on_verification_url: Callable[[VerificationPrompt], None],
    capabilities: Iterable[str] = (),
    domain: str | None = None,
    lark_domain: str | None = None,
    cancel_event: Any | None = None,
    source: str | None = None,
) -> str:
    """Re-run the device-authorization flow against an App that already exists.

    This is the "one link the user clicks" path.  Because ``addons`` rides along
    with the authorization request, a single approval both *declares* the listed
    scopes on the App and *authorizes* them for the tenant -- which is why it
    reaches capabilities the ``scopes/apply`` endpoint cannot touch: that
    endpoint can only ask for scopes that are already declared.

    Deliberately unlike :func:`resolve_lark_app_credential`:

    * ``HYPRIAL_LARK_APP_ID``/``HYPRIAL_LARK_APP_SECRET`` are never consulted.  The
      caller named a specific App; an ambient credential cannot stand in for it,
      and short-circuiting would silently skip the authorization the caller
      asked for.
    * ``create_only`` is never sent and ``app_id`` is mandatory, so this call
      cannot create an App.
    * No ``app_preset`` is sent.  Preset values pre-fill the App-creation page
      and have no meaning for an App that exists; withholding them removes any
      chance of a name or description reaching a live App.
    * The returned ``client_secret`` is neither read nor returned.  Authorizing
      an App we already hold a credential for must not rotate or re-persist that
      credential.

    Returns the ``client_id`` the platform confirmed, which must equal
    ``app_id``.
    """

    if not app_id:
        raise ValueError("reauthorize_lark_app requires a non-empty app_id")
    options: dict[str, Any] = {
        "addons": authorization_addons(capabilities),
        "app_id": app_id,
    }
    # Only forward transport overrides when set, so the SDK keeps ownership of
    # its own Feishu/Lark defaults.
    if domain is not None:
        options["domain"] = domain
    if lark_domain is not None:
        options["lark_domain"] = lark_domain
    if cancel_event is not None:
        options["cancel_event"] = cancel_event
    if source is not None:
        options["source"] = source

    result = register_app(_verification_callback(on_verification_url), **options)
    if not isinstance(result, Mapping):
        raise LarkOnboardingError("the authorization flow returned no result")
    authorized = result.get("client_id")
    if not isinstance(authorized, str) or not authorized:
        raise LarkOnboardingError("the authorization flow returned no client_id")
    if authorized != app_id:
        # A different App came back than the one we asked to authorize: the
        # human approved something else. Report it instead of silently
        # accepting an App this adapter has no credential for.
        raise LarkOnboardingError(
            f"the authorization flow returned App {authorized!r}, "
            f"but adapter's App is {app_id!r}; nothing was changed locally"
        )
    return authorized


def resolve_lark_app_credential(
    *,
    name: str,
    register_app: Callable[..., Mapping[str, Any]],
    on_verification_url: Callable[[VerificationPrompt], None],
    non_interactive: bool,
    env: Mapping[str, str] | None = None,
    app_id: str | None = None,
    create_only: bool = False,
    domain: str | None = None,
    lark_domain: str | None = None,
    cancel_event: Any | None = None,
    source: str | None = None,
) -> LarkAppCredential:
    """Resolve (or create) the Lark App credential backing a gateway.

    ``register_app`` is injected rather than imported so the branch structure is
    testable without the network; production callers pass
    ``lark_oapi.register_app``.

    ``create_only`` demands a brand-new App and therefore deliberately bypasses
    the environment short-circuit -- otherwise "make me a new App" would
    silently return the ambient one.  ``app_id`` re-runs authorization against an
    existing App instead of creating one.
    """

    environment = os.environ if env is None else env
    environment_app_id = environment.get(APP_ID_ENV_VAR)
    environment_secret = environment.get(APP_SECRET_ENV_VAR)
    if (
        not create_only
        and environment_app_id is not None
        and environment_secret is not None
    ):
        return LarkAppCredential(
            app_id=environment_app_id, app_secret=environment_secret
        )
    if non_interactive:
        raise UserActionRequiredError(
            "Lark App onboarding requires an interactive device-authorization "
            "flow or environment credentials",
            rerun_interactively_action(name),
        )

    on_qr_code = _verification_callback(on_verification_url)

    options: dict[str, Any] = {
        "addons": onboarding_addons(),
        "app_preset": onboarding_app_preset(name),
    }
    if app_id is not None:
        options["app_id"] = app_id
    if create_only:
        options["create_only"] = True
    # Only forward transport overrides when set, so the SDK keeps ownership of
    # its own Feishu/Lark defaults.
    if domain is not None:
        options["domain"] = domain
    if lark_domain is not None:
        options["lark_domain"] = lark_domain
    if cancel_event is not None:
        options["cancel_event"] = cancel_event
    if source is not None:
        options["source"] = source

    result = register_app(on_qr_code, **options)
    if not isinstance(result, Mapping):
        raise LarkOnboardingError("the registration flow returned no credential")
    created_app_id = result.get("client_id")
    created_secret = result.get("client_secret")
    if not isinstance(created_app_id, str) or not created_app_id:
        raise LarkOnboardingError("the registration flow returned no client_id")
    if not isinstance(created_secret, str) or not created_secret:
        # Never echo the value; only report its absence.
        raise LarkOnboardingError("the registration flow returned no client_secret")
    return LarkAppCredential(app_id=created_app_id, app_secret=created_secret)
