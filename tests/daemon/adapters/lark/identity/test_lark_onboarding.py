"""Tests for Lark App onboarding -- credential resolution and App creation.

Mirrors the TypeScript ``src/channels/lark-onboarding.ts`` branch structure:
environment short-circuit, structured non-interactive refusal, and the official
device-authorization flow.  ``register_app`` is injected, so every branch is
exercised without a network call and without creating a real App.

The one thing that is *not* faked is the SDK's own ``addons`` encoder: the
declaration this module builds is pushed through
``lark_oapi.scene.registration._encode_addons`` and decoded again, so a scope or
event that the platform would reject fails here rather than in production.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

from hyprial.daemon.impl.adapters.lark.credentials.onboarding import (
    APP_ID_ENV_VAR,
    APP_SECRET_ENV_VAR,
    LARK_ONBOARDING_REQUIRED_EVENTS,
    LARK_ONBOARDING_REQUIRED_SCOPES,
    LarkOnboardingError,
    UserActionRequiredError,
    VerificationPrompt,
    authorization_addons,
    onboarding_addons,
    reauthorize_lark_app,
    required_capability_scopes_are_covered,
    resolve_lark_app_credential,
)
from hyprial.daemon.impl.adapters.lark.credentials.scopes import (
    LARK_GATEWAY_CAPABILITIES,
    OPTIONAL_CAPABILITY_IDS,
    SENSITIVE_CAPABILITY_NOTES,
    capability_scopes,
)
ROOT = Path(__file__).resolve().parents[5]

CREATED_APP_ID = "cli_created_app_1"
CREATED_SECRET = "created-secret-must-never-be-printed-7a3e"
ENV_APP_ID = "cli_env_app_1"
ENV_SECRET = "env-secret-must-never-be-printed-4b91"
VERIFICATION_URL = "https://accounts.example.invalid/open-apis/authen?device=abc"


class RegisterAppSpy:
    """Stand-in for ``lark_oapi.register_app`` recording exactly one call."""

    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        qr_payload: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = (
            {"client_id": CREATED_APP_ID, "client_secret": CREATED_SECRET}
            if result is None
            else result
        )
        self._qr_payload = (
            {"url": VERIFICATION_URL, "expire_in": 600}
            if qr_payload is None
            else qr_payload
        )

    def __call__(self, on_qr_code: Any, **options: Any) -> Any:
        self.calls.append(options)
        on_qr_code(self._qr_payload)
        return self._result

    @property
    def options(self) -> dict[str, Any]:
        assert len(self.calls) == 1, f"expected exactly one call, got {self.calls}"
        return self.calls[0]


def collect_prompts() -> tuple[list[VerificationPrompt], Any]:
    prompts: list[VerificationPrompt] = []
    return prompts, prompts.append


def resolve(**overrides: Any) -> Any:
    prompts, sink = collect_prompts()
    register = overrides.pop("register_app", None) or RegisterAppSpy()
    arguments: dict[str, Any] = {
        "name": "squire",
        "register_app": register,
        "on_verification_url": sink,
        "non_interactive": False,
        "env": {},
    }
    arguments.update(overrides)
    credential = resolve_lark_app_credential(**arguments)
    return credential, register, prompts


# --------------------------------------------------------------------------
# Branch 1: environment short-circuit
# --------------------------------------------------------------------------


def test_environment_credentials_short_circuit_without_touching_the_network() -> None:
    register = RegisterAppSpy()
    credential, _, prompts = resolve(
        env={APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET},
        register_app=register,
    )

    assert credential.app_id == ENV_APP_ID
    assert credential.app_secret == ENV_SECRET
    # The whole point of the short-circuit: no registration flow, no prompt.
    assert register.calls == []
    assert prompts == []


def test_environment_short_circuit_wins_even_when_non_interactive() -> None:
    """Env credentials are checked before the interactivity refusal."""

    register = RegisterAppSpy()
    credential, _, _ = resolve(
        env={APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET},
        non_interactive=True,
        register_app=register,
    )

    assert credential.app_id == ENV_APP_ID
    assert register.calls == []


@pytest.mark.parametrize(
    "environment",
    [
        {APP_ID_ENV_VAR: ENV_APP_ID},
        {APP_SECRET_ENV_VAR: ENV_SECRET},
        {},
    ],
    ids=["id-only", "secret-only", "neither"],
)
def test_partial_environment_credentials_do_not_short_circuit(
    environment: dict[str, str],
) -> None:
    credential, register, _ = resolve(env=environment)

    # A half-configured environment must never be silently combined with a
    # created App; it falls through to the real flow.
    assert credential.app_id == CREATED_APP_ID
    assert len(register.calls) == 1


def test_create_only_bypasses_environment_credentials() -> None:
    """'--new' means a new App, not the ambient one."""

    credential, register, _ = resolve(
        env={APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET},
        create_only=True,
    )

    assert credential.app_id == CREATED_APP_ID
    assert register.options["create_only"] is True


def test_environment_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_ID_ENV_VAR, ENV_APP_ID)
    monkeypatch.setenv(APP_SECRET_ENV_VAR, ENV_SECRET)
    register = RegisterAppSpy()

    credential = resolve_lark_app_credential(
        name="squire",
        register_app=register,
        on_verification_url=lambda prompt: None,
        non_interactive=False,
    )

    assert credential.app_id == ENV_APP_ID
    assert register.calls == []


# --------------------------------------------------------------------------
# Branch 2: structured non-interactive refusal
# --------------------------------------------------------------------------


def test_non_interactive_raises_structured_user_action_required() -> None:
    register = RegisterAppSpy()

    with pytest.raises(UserActionRequiredError) as excinfo:
        resolve(non_interactive=True, register_app=register, name="kanban")

    error = excinfo.value
    assert error.code == "USER_ACTION_REQUIRED"
    action = error.action
    # The recovery must name the command that actually exists in this CLI.
    assert action["command"] == "hyprial adapter add kanban"
    assert action["environment"] == [APP_ID_ENV_VAR, APP_SECRET_ENV_VAR]
    assert action["type"] == "rerun_interactively_or_set_environment"
    assert "human" in action["reason"]
    # Refusal must happen before any registration attempt.
    assert register.calls == []


def test_non_interactive_action_is_json_serializable() -> None:
    with pytest.raises(UserActionRequiredError) as excinfo:
        resolve(non_interactive=True)

    assert json.loads(json.dumps(excinfo.value.action))["command"].startswith("hyprial ")


# --------------------------------------------------------------------------
# Branch 3: device-authorization flow
# --------------------------------------------------------------------------


def test_created_app_declares_required_scopes_and_the_inbound_event() -> None:
    _, register, _ = resolve(name="squire")
    addons = register.options["addons"]

    assert addons == {
        "scopes": {"tenant": ["im:message", "im:message.group_at_msg:readonly"]},
        "events": {"items": {"tenant": ["im.message.receive_v1"]}},
    }


def test_created_app_preset_names_the_gateway() -> None:
    _, register, _ = resolve(name="kanban")

    assert register.options["app_preset"] == {
        "name": "Harness Bridge kanban",
        "desc": "Harness Bridge Feishu gateway",
    }


def test_verification_url_is_handed_to_the_caller_with_expiry() -> None:
    _, _, prompts = resolve(
        register_app=RegisterAppSpy(
            qr_payload={"url": VERIFICATION_URL, "expire_in": 420}
        )
    )

    assert len(prompts) == 1
    # The Python SDK spells this snake_case; TypeScript spells it expireIn.
    assert prompts[0] == VerificationPrompt(url=VERIFICATION_URL, expire_in=420)


def test_missing_expiry_degrades_to_zero_rather_than_failing() -> None:
    _, _, prompts = resolve(
        register_app=RegisterAppSpy(qr_payload={"url": VERIFICATION_URL})
    )

    assert prompts[0].expire_in == 0
    assert prompts[0].url == VERIFICATION_URL


def test_registration_without_a_verification_url_fails_loudly() -> None:
    with pytest.raises(LarkOnboardingError, match="no verification URL"):
        resolve(register_app=RegisterAppSpy(qr_payload={"expire_in": 600}))


def test_existing_app_id_is_forwarded_and_no_new_app_is_requested() -> None:
    _, register, _ = resolve(app_id="cli_existing_app")

    assert register.options["app_id"] == "cli_existing_app"
    assert "create_only" not in register.options


def test_transport_overrides_are_omitted_unless_set() -> None:
    _, register, _ = resolve()

    # The SDK keeps ownership of its Feishu/Lark defaults when we pass nothing.
    assert "domain" not in register.options
    assert "lark_domain" not in register.options


def test_transport_overrides_are_forwarded_when_set() -> None:
    _, register, _ = resolve(
        domain="http://127.0.0.1:9/", lark_domain="http://127.0.0.1:9/"
    )

    assert register.options["domain"] == "http://127.0.0.1:9/"
    assert register.options["lark_domain"] == "http://127.0.0.1:9/"


@pytest.mark.parametrize(
    "result",
    [
        {"client_id": CREATED_APP_ID},
        {"client_secret": CREATED_SECRET},
        {"client_id": "", "client_secret": CREATED_SECRET},
        {"client_id": CREATED_APP_ID, "client_secret": ""},
        {},
    ],
    ids=["no-secret", "no-id", "empty-id", "empty-secret", "empty"],
)
def test_incomplete_registration_result_is_rejected(result: dict[str, Any]) -> None:
    with pytest.raises(LarkOnboardingError):
        resolve(register_app=RegisterAppSpy(result=result))


def test_registration_error_message_never_contains_the_secret() -> None:
    with pytest.raises(LarkOnboardingError) as excinfo:
        resolve(register_app=RegisterAppSpy(result={"client_secret": CREATED_SECRET}))

    assert CREATED_SECRET not in str(excinfo.value)


def test_addons_are_rebuilt_per_call_so_a_mutating_caller_cannot_leak() -> None:
    first = onboarding_addons()
    first["scopes"]["tenant"].append("contact:user.email:readonly")

    assert onboarding_addons()["scopes"]["tenant"] == list(
        LARK_ONBOARDING_REQUIRED_SCOPES
    )


# --------------------------------------------------------------------------
# The declaration must survive the real SDK encoder
# --------------------------------------------------------------------------


def decode_addons(encoded: str) -> dict[str, Any]:
    """Reverse the SDK's urlsafe-base64(gzip(json)) addons parameter."""

    padded = encoded + "=" * (-len(encoded) % 4)
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(padded)))


def test_addons_round_trip_through_the_official_sdk_encoder() -> None:
    from lark_oapi.scene.registration import _encode_addons

    # _encode_addons runs the SDK's own validation; an unsupported key or an
    # empty declaration raises here rather than at the platform.
    assert decode_addons(_encode_addons(onboarding_addons())) == {
        "scopes": {"tenant": list(LARK_ONBOARDING_REQUIRED_SCOPES)},
        "events": {"items": {"tenant": list(LARK_ONBOARDING_REQUIRED_EVENTS)}},
    }


# --------------------------------------------------------------------------
# Scope selection invariant
# --------------------------------------------------------------------------


def test_onboarding_scopes_satisfy_every_required_gateway_capability() -> None:
    """The created App can do everything the gateway declares as required.

    Asserted rather than assumed: ``LARK_GATEWAY_CAPABILITIES`` and the
    onboarding scope set live in different modules and would otherwise drift.
    """

    selected = set(LARK_ONBOARDING_REQUIRED_SCOPES)
    unsatisfied = [
        capability.id
        for capability in LARK_GATEWAY_CAPABILITIES
        if capability.requirement == "required"
        and not all(selected.intersection(group) for group in capability.scope_groups)
    ]

    assert unsatisfied == []
    assert required_capability_scopes_are_covered() is True


def test_required_capability_coverage_helper_detects_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant helper is a real check, not a constant ``True``."""

    monkeypatch.setattr(
        "hyprial.daemon.impl.adapters.lark.credentials.onboarding.LARK_ONBOARDING_REQUIRED_SCOPES",
        ("im:message",),
    )

    # Dropping the group-mention scope must be detected.
    assert required_capability_scopes_are_covered() is False


# --------------------------------------------------------------------------
# Capability-derived scope selection (the interactive authorize path)
# --------------------------------------------------------------------------


def test_capability_scopes_default_is_the_onboarding_set() -> None:
    """The drift guard between the two ways of naming the same minimum.

    ``LARK_ONBOARDING_REQUIRED_SCOPES`` is hand-written for App creation;
    ``capability_scopes`` derives the same list from the capability table. If
    they ever disagree, one of the two flows is declaring the wrong thing.
    """

    assert capability_scopes() == LARK_ONBOARDING_REQUIRED_SCOPES
    assert capability_scopes(()) == LARK_ONBOARDING_REQUIRED_SCOPES


def test_capability_scopes_add_group_history_only_when_asked() -> None:
    default = capability_scopes()
    widened = capability_scopes(["group-history"])

    # im:message.group_msg lets the App read every group message it can see, so
    # it must be absent unless the caller named the capability.
    assert "im:message.group_msg" not in default
    # Exactly one new scope: group-history's second scope group is already
    # satisfied by im:message, so an opt-in must not widen anything else.
    assert widened == (*default, "im:message.group_msg")


def test_capability_scopes_are_stable_and_deduplicated() -> None:
    once = capability_scopes(["group-history"])

    assert capability_scopes(["group-history", "group-history"]) == once
    # Ordering follows the capability table, not the caller's argument order.
    assert capability_scopes(["quote-fetch", "group-history"]) == capability_scopes(
        ["group-history", "quote-fetch"]
    )
    assert len(set(once)) == len(once)


def test_capability_scopes_cover_every_named_capability() -> None:
    selected = set(capability_scopes(OPTIONAL_CAPABILITY_IDS))
    unsatisfied = [
        capability.id
        for capability in LARK_GATEWAY_CAPABILITIES
        if not all(selected.intersection(group) for group in capability.scope_groups)
    ]

    assert unsatisfied == []


def test_capability_scopes_reject_an_unknown_capability() -> None:
    with pytest.raises(ValueError, match="unknown Lark gateway capability"):
        capability_scopes(["group-hisotry"])


def test_every_sensitive_note_names_a_real_optional_capability() -> None:
    """A note that names nothing would silently stop warning anyone."""

    assert set(SENSITIVE_CAPABILITY_NOTES) <= set(OPTIONAL_CAPABILITY_IDS)
    assert "im:message.group_msg" in SENSITIVE_CAPABILITY_NOTES["group-history"]


def test_authorization_addons_round_trip_through_the_official_sdk_encoder() -> None:
    from lark_oapi.scene.registration import _encode_addons

    assert decode_addons(_encode_addons(authorization_addons(["group-history"]))) == {
        "scopes": {
            "tenant": [
                "im:message",
                "im:message.group_at_msg:readonly",
                "im:message.group_msg",
            ]
        },
        "events": {"items": {"tenant": list(LARK_ONBOARDING_REQUIRED_EVENTS)}},
    }


# --------------------------------------------------------------------------
# Re-authorizing an App that already exists
# --------------------------------------------------------------------------

EXISTING_APP_ID = "cli_existing_app_1"


def reauthorize(**overrides: Any) -> Any:
    prompts, sink = collect_prompts()
    register = overrides.pop("register_app", None) or RegisterAppSpy(
        result={"client_id": EXISTING_APP_ID, "client_secret": CREATED_SECRET}
    )
    arguments: dict[str, Any] = {
        "app_id": EXISTING_APP_ID,
        "register_app": register,
        "on_verification_url": sink,
    }
    arguments.update(overrides)
    authorized = reauthorize_lark_app(**arguments)
    return authorized, register, prompts


def test_reauthorize_targets_the_existing_app_and_can_never_create_one() -> None:
    authorized, register, prompts = reauthorize()

    assert authorized == EXISTING_APP_ID
    assert register.options["app_id"] == EXISTING_APP_ID
    # Creating an App is the one thing this path must be structurally unable to
    # do: no create_only, and no preset that a creation page would consume.
    assert "create_only" not in register.options
    assert "app_preset" not in register.options
    assert prompts == [VerificationPrompt(url=VERIFICATION_URL, expire_in=600)]


def test_reauthorize_ignores_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient credential cannot stand in for a named App.

    ``resolve_lark_app_credential`` short-circuits on the environment because it
    only needs *a* credential. Here the caller asked for a specific App to be
    authorized, so skipping the flow would skip the whole point of the call.
    """

    monkeypatch.setenv(APP_ID_ENV_VAR, ENV_APP_ID)
    monkeypatch.setenv(APP_SECRET_ENV_VAR, ENV_SECRET)

    _, register, prompts = reauthorize()

    assert len(register.calls) == 1
    assert prompts


def test_reauthorize_declares_only_required_scopes_by_default() -> None:
    _, register, _ = reauthorize()

    assert register.options["addons"] == {
        "scopes": {"tenant": list(LARK_ONBOARDING_REQUIRED_SCOPES)},
        "events": {"items": {"tenant": list(LARK_ONBOARDING_REQUIRED_EVENTS)}},
    }


def test_reauthorize_carries_a_requested_optional_capability() -> None:
    _, register, _ = reauthorize(capabilities=["group-history"])

    assert register.options["addons"]["scopes"]["tenant"] == [
        "im:message",
        "im:message.group_at_msg:readonly",
        "im:message.group_msg",
    ]


def test_reauthorize_rejects_a_different_app_than_the_one_requested() -> None:
    spy = RegisterAppSpy(
        result={"client_id": "cli_somebody_elses_app", "client_secret": CREATED_SECRET}
    )

    with pytest.raises(LarkOnboardingError, match="cli_somebody_elses_app"):
        reauthorize(register_app=spy)


@pytest.mark.parametrize(
    "result",
    [{"client_secret": CREATED_SECRET}, {"client_id": ""}, {}],
    ids=["no-id", "empty-id", "empty"],
)
def test_reauthorize_rejects_an_unusable_result(result: dict[str, Any]) -> None:
    with pytest.raises(LarkOnboardingError):
        reauthorize(register_app=RegisterAppSpy(result=result))


def test_reauthorize_never_returns_or_reports_the_app_secret() -> None:
    authorized, _, _ = reauthorize()

    assert CREATED_SECRET not in authorized
    with pytest.raises(LarkOnboardingError) as excinfo:
        reauthorize(
            register_app=RegisterAppSpy(
                result={"client_id": "cli_other", "client_secret": CREATED_SECRET}
            )
        )
    assert CREATED_SECRET not in str(excinfo.value)


def test_reauthorize_requires_an_app_id() -> None:
    with pytest.raises(ValueError, match="non-empty app_id"):
        reauthorize(app_id="")


def test_reauthorize_forwards_transport_overrides_only_when_set() -> None:
    _, plain, _ = reauthorize()
    assert "domain" not in plain.options and "lark_domain" not in plain.options

    _, overridden, _ = reauthorize(
        domain="http://127.0.0.1:9/", lark_domain="http://127.0.0.1:9/"
    )
    assert overridden.options["domain"] == "http://127.0.0.1:9/"
    assert overridden.options["lark_domain"] == "http://127.0.0.1:9/"


# --------------------------------------------------------------------------
# Registration fencing: route-less gateways stay opt-in
# --------------------------------------------------------------------------


def test_add_lark_gateway_still_requires_a_route_by_default(tmp_path: Path) -> None:
    from hyprial.daemon.impl.bootstrap.adapter_registration import add_lark_gateway
    from hyprial.kernel import PersistentConfigError

    with pytest.raises(PersistentConfigError, match="at least one route"):
        add_lark_gateway(
            hyprial_home=tmp_path,
            name="probe",
            app_id="cli_probe",
            app_secret=CREATED_SECRET,
            routes=(),
        )

    assert not (tmp_path / "channels.json").exists()


def test_route_less_gateway_loads_through_the_daemon_config_store(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.bootstrap.adapter_registration import add_lark_gateway
    from hyprial.kernel import PersistentConfigStore

    summary = add_lark_gateway(
        hyprial_home=tmp_path,
        name="probe",
        app_id="cli_probe",
        app_secret=CREATED_SECRET,
        routes=(),
        allow_no_routes=True,
    )

    assert summary["adapter"] == {
        "provider": "lark",
        "name": "probe",
        "appId": "cli_probe",
        "credentialRef": "lark-probe",
        "routes": [],
    }
    # The note must name the concrete way out, not leave a dead end.
    assert "hyprial adapter add probe --app-id cli_probe --route" in summary["note"]
    # The note points at a hot reload, never at a full daemon restart.
    assert "hyprial adapter reload" in summary["note"]
    assert "restart the daemon" not in summary["note"].lower()
    assert CREATED_SECRET not in json.dumps(summary)

    # This is the exact path the daemon walks at boot.
    loaded = PersistentConfigStore(tmp_path, tmp_path).load()
    gateway = loaded.channels.gateways[0]
    assert (gateway.name, gateway.app_id, gateway.routes) == ("probe", "cli_probe", ())
    assert gateway.default_route is None
    assert (tmp_path / "secrets" / "lark-probe.json").stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------
# CLI surface (subprocess: real argv/stdout/stderr)
# --------------------------------------------------------------------------


def cli_env(home: Path, **extra: str) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home),
        **extra,
    }
    for name in (APP_ID_ENV_VAR, APP_SECRET_ENV_VAR, "HYPRIAL_ZENOH_LISTEN"):
        if name not in extra:
            environment.pop(name, None)
    return environment


def run_onboard(
    home: Path, *arguments: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "adapter", "add", *arguments],
        cwd=ROOT,
        env=cli_env(home, **extra),
        input="",
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_non_interactive_reports_user_action_required(tmp_path: Path) -> None:
    result = run_onboard(tmp_path / "home", "squire", "--json")

    assert result.returncode == 1
    body = json.loads(result.stdout)
    assert body["ok"] is False
    assert body["code"] == "USER_ACTION_REQUIRED"
    assert body["data"]["command"] == "hyprial adapter add squire"
    assert body["data"]["environment"] == [APP_ID_ENV_VAR, APP_SECRET_ENV_VAR]
    # Nothing may be written when onboarding refuses.
    assert not (tmp_path / "home" / "channels.json").exists()


def test_cli_environment_credentials_register_without_a_browser_step(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    result = run_onboard(
        home,
        "squire",
        "--json",
        **{APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET},
    )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["adapter"]["appId"] == ENV_APP_ID
    assert body["adapter"]["routes"] == []
    assert body["onboarding"] == {"source": "environment", "createdApp": False}
    assert "hyprial adapter scopes authorize squire" in body["nextStep"]
    assert body["daemonReload"] is None
    assert "hyprial adapter reload" not in body["nextStep"]
    assert "restart the daemon" not in body["nextStep"].lower()
    # The secret is persisted, never echoed.
    assert ENV_SECRET not in result.stdout
    assert ENV_SECRET not in result.stderr
    secret_path = home / "secrets" / "lark-squire.json"
    assert json.loads(secret_path.read_text())["appSecret"] == ENV_SECRET
    assert secret_path.stat().st_mode & 0o777 == 0o600


def test_authorize_interactive_next_step_asks_for_reload_not_daemon_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-authorization grants apply on the Lark side: the handoff points at
    'hyprial adapter reload' for local config changes, never at a full daemon
    restart."""

    import hyprial.daemon as onboarding_module
    from hyprial.daemon.impl.bootstrap.adapter_registration import add_lark_gateway
    from hyprial.shell.impl.cli.commands.adapter.authorize import _authorize_interactively

    home = tmp_path / "home"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    add_lark_gateway(
        hyprial_home=home,
        name="squire",
        app_id="cli_existing",
        app_secret=CREATED_SECRET,
        routes=(),
        allow_no_routes=True,
    )

    def fake_reauthorize_lark_app(**kwargs: Any) -> str:
        return str(kwargs["app_id"])

    monkeypatch.setattr(
        onboarding_module, "reauthorize_lark_app", fake_reauthorize_lark_app
    )

    result = _authorize_interactively("squire", [])

    assert result["status"] == "authorized"
    assert "hyprial adapter scopes check squire" in result["nextStep"]
    assert "hyprial adapter reload" in result["nextStep"]
    assert "restart the daemon" not in result["nextStep"].lower()


def test_cli_persists_routes_when_they_are_already_known(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run_onboard(
        home,
        "squire",
        "--route",
        "dm=ou_owner_1",
        "--json",
        **{APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET},
    )

    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["adapter"]["routes"] == [
        {"name": "dm", "type": "direct", "nativeId": "ou_owner_1"}
    ]
    # A single route becomes the default, matching 'adapter add'.
    assert body["adapter"]["defaultRoute"] == "dm"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--new", "--app-id", "cli_x"), "either --new or --app-id"),
        (("--default-route", "dm"), "--default-route requires at least one --route"),
    ],
    ids=["new-and-app-id", "default-route-without-route"],
)
def test_cli_rejects_contradictory_arguments(
    tmp_path: Path, arguments: tuple[str, ...], message: str
) -> None:
    result = run_onboard(
        tmp_path / "home",
        "squire",
        *arguments,
        "--json",
        **{APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET},
    )

    assert result.returncode == 1
    body = json.loads(result.stdout)
    assert body["code"] == "INVALID_ARGUMENT"
    assert message in body["error"]
    assert not (tmp_path / "home" / "channels.json").exists()


# --------------------------------------------------------------------------
# E2E-011 runner safety properties
#
# The runner points the official SDK at a loopback stub. Two of its response
# fields are load-bearing safety properties rather than conveniences, so they
# are pinned here instead of only living in the runner.
# --------------------------------------------------------------------------


def load_runner() -> Any:
    import importlib.util

    scenarios = ROOT / "contract" / "e2e"
    if str(scenarios) not in sys.path:
        sys.path.insert(0, str(scenarios))
    path = scenarios / "run-lark-app-onboarding.py"
    spec = importlib.util.spec_from_file_location("lark_onboarding_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_stub_never_triggers_the_sdk_domain_switch() -> None:
    """A ``tenant_brand`` of ``lark`` would redirect the flow to a public host.

    ``_RegistrationFlow._handle_poll_response`` switches ``_base_url`` to
    ``lark_domain`` when a poll reports that brand.  Both domains are already
    pinned to the stub, but the stub must also never ask for the switch.
    """

    runner = load_runner()
    stub = runner.RegistrationStub()
    payloads = [
        stub.respond("/oauth/v1/app/registration", {"action": [action]})
        for action in ("init", "begin", "poll", "poll")
    ]

    assert all("user_info" not in payload for payload in payloads)
    assert "tenant_brand" not in json.dumps(payloads)


def test_runner_stub_begin_payload_keeps_the_poll_loop_reachable() -> None:
    """``expires_in`` must be positive and ``device_code`` present.

    The SDK reads ``begin_res["device_code"]`` unguarded, and its poll loop is
    bounded by ``expires_in``; a zero deadline would raise AppExpiredError
    without a single poll.
    """

    runner = load_runner()
    begin = runner.RegistrationStub().respond(
        "/oauth/v1/app/registration", {"action": ["begin"]}
    )

    assert begin["device_code"]
    assert begin["expires_in"] > 0
    assert begin["interval"] == 0  # keeps the pending round instant
    assert begin["verification_uri_complete"]


def test_runner_report_redacts_the_stub_secret() -> None:
    runner = load_runner()
    report = runner.Report()

    report.add("PASS", "check", f"leaked {runner.STUB_APP_SECRET} here")

    assert runner.STUB_APP_SECRET not in report.results[0].detail
    assert "<redacted>" in report.results[0].detail


def test_runner_expectation_matches_the_shipped_declaration() -> None:
    """The runner's hardcoded expectation must track the implementation."""

    runner = load_runner()

    assert runner.EXPECTED_ADDONS == onboarding_addons()


def test_runner_protects_the_three_production_apps() -> None:
    runner = load_runner()

    assert set(runner.PROTECTED_APPS) == {
        "hq-adjutant-feishu",
        "hyprial-kanban",
        "mac-studio-feishu",
    }


def test_cli_refuses_to_overwrite_an_existing_adapter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    credentials = {APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET}
    assert run_onboard(home, "squire", "--json", **credentials).returncode == 0

    repeated = run_onboard(home, "squire", "--json", **credentials)

    assert repeated.returncode == 1
    assert json.loads(repeated.stdout)["code"] == "ADAPTER_EXISTS"


# --------------------------------------------------------------------------
# A doomed registration must fail before a human creates a real App
# --------------------------------------------------------------------------


def test_name_clash_is_rejected_before_any_credential_is_obtained(
    tmp_path: Path,
) -> None:
    """The expensive path must not run when the write is already doomed.

    Onboarding pays for its credential with a human approving a real App on a
    real tenant. If that happens and only *then* the name is found to be taken,
    the App exists and nothing references it -- an unrecoverable side effect
    from a preventable error.
    """

    from hyprial.daemon.impl.bootstrap.adapter_registration import (
        AdapterExistsError,
        add_lark_gateway,
        ensure_lark_gateway_addable,
    )
    from hyprial.kernel import PersistentConfigError

    add_lark_gateway(
        hyprial_home=tmp_path,
        name="squire",
        app_id="cli_first",
        app_secret=CREATED_SECRET,
        routes=(),
        allow_no_routes=True,
    )

    with pytest.raises(AdapterExistsError):
        ensure_lark_gateway_addable(hyprial_home=tmp_path, name="squire")
    # --force is exactly what makes an overwrite intentional.
    ensure_lark_gateway_addable(hyprial_home=tmp_path, name="squire", force=True)
    # A malformed name is caught here too, for the same reason.
    with pytest.raises(PersistentConfigError, match="adapter name must match"):
        ensure_lark_gateway_addable(hyprial_home=tmp_path, name="Bad Name")
    ensure_lark_gateway_addable(hyprial_home=tmp_path, name="fresh")


def test_cli_name_clash_never_reaches_the_registration_flow(tmp_path: Path) -> None:
    """--new against a taken name must fail without a device-authorization run.

    Registration is unreachable in this test (no credentials, no accounts
    endpoint), so if the clash were detected only after credential resolution
    the command would fail with USER_ACTION_REQUIRED instead of ADAPTER_EXISTS.
    """

    home = tmp_path / "home"
    credentials = {APP_ID_ENV_VAR: ENV_APP_ID, APP_SECRET_ENV_VAR: ENV_SECRET}
    assert run_onboard(home, "squire", "--json", **credentials).returncode == 0

    clash = run_onboard(home, "squire", "--new", "--json")

    assert clash.returncode == 1
    body = json.loads(clash.stdout)
    assert body["code"] == "ADAPTER_EXISTS"
    assert "--force" in body["error"]


def test_cli_rejects_a_malformed_name_before_registration(tmp_path: Path) -> None:
    result = run_onboard(tmp_path / "home", "Bad Name", "--new", "--json")

    assert result.returncode == 1
    body = json.loads(result.stdout)
    # Not USER_ACTION_REQUIRED: the name is doomed regardless of any human.
    assert body["code"] == "INVALID_ARGUMENT"
    assert "adapter name must match" in body["error"]


@pytest.mark.parametrize("name", ["u.ou_" + "b" * 32, "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9", "d." + "b" * 32, "a." + "a" * 26, "U.OU_" + "B" * 32, "D." + "B" * 32, "A." + "A" * 26])
def test_adapter_names_reject_exact_identity_id_shapes(tmp_path: Path, name: str) -> None:
    from hyprial.daemon.impl.bootstrap.adapter_registration import (
        ensure_lark_gateway_addable,
    )
    from hyprial.kernel import PersistentConfigError

    with pytest.raises(PersistentConfigError, match="identity id shape"):
        ensure_lark_gateway_addable(hyprial_home=tmp_path, name=name)


@pytest.mark.parametrize("name", ["d.lane", "a.smith", "u.zhang"])
def test_adapter_names_accept_id_prefix_lookalikes(tmp_path: Path, name: str) -> None:
    from hyprial.daemon.impl.bootstrap.adapter_registration import ensure_lark_gateway_addable
    ensure_lark_gateway_addable(hyprial_home=tmp_path, name=name)
