#!/usr/bin/env python3
"""E2E-004: attended Feishu user -> adapter -> Harness -> adapter roundtrip.

The approved test App (name + app id) is the anchor.  The default contract
target is the App's configured test GROUP chat -- the shared, reproducible
target any developer can join -- resolved from the gateway configuration
(single source of truth), with HYPRIAL_E2E_LARK_CHAT_ID as an explicit override.
A p2p DM with the bot is a per-developer private conversation; its id must
never be committed, so a p2p run always comes from the override.  The
runner adapts to the resolved chat's mode: assertions compare chat_type
against the actual mode, and in group mode user messages carry the bot's
@mention (the platform only delivers group messages that mention the bot).
Missing configuration or an inaccessible chat fails loudly at preflight.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lark_oapi as lark

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
)

from feishu.settings import (
    ACTOR as ACTOR,
    APP_ID as APP_ID,
    APP_ID_ENV_VAR as APP_ID_ENV_VAR,
    APP_NAME as APP_NAME,
    APP_SECRET_ENV_VAR as APP_SECRET_ENV_VAR,
    ASSERTIONS as ASSERTIONS,
    ATTACHMENT_FILE_NAME as ATTACHMENT_FILE_NAME,
    CHAT_ID_ENV_VAR as CHAT_ID_ENV_VAR,
    EVENT_TIMEOUT_SECONDS as EVENT_TIMEOUT_SECONDS,
    OUTBOUND_SENDER as OUTBOUND_SENDER,
    POLL_SECONDS as POLL_SECONDS,
    REACTION_TIMEOUT_SECONDS as REACTION_TIMEOUT_SECONDS,
    TEST_ROUTE_NAME as TEST_ROUTE_NAME,
    TINY_PNG as TINY_PNG,
)
from feishu import evidence as _evidence
from feishu.evidence import (
    ContractFailure as ContractFailure,
    ContractBlocked as ContractBlocked,
    utc_now as utc_now,
    epoch_ms as epoch_ms,
    text_digest as text_digest,
    native_id_label as native_id_label,
    redact_native_ids as redact_native_ids,
    _sanitize_output_value as _sanitize_output_value,
    assert_sanitized_evidence as assert_sanitized_evidence,
    load_json as load_json,
    safe_cli_error as safe_cli_error,
    EvidenceLog as EvidenceLog,
    read_evidence as read_evidence,
    _FailurePreservingTempDir as _FailurePreservingTempDir,
)
from feishu import content as _content
from feishu.content import (
    find_first_string as find_first_string,
    _user_content_args as _user_content_args,
    expected_post_text as expected_post_text,
    post_content as post_content,
    media_key_from_content as media_key_from_content,
    _lark_message_content as _lark_message_content,
)
from feishu.transports import messages as _transports_messages
from feishu.transports import outbound as _transports_outbound
from feishu.transports.outbound import (
    hyprial_cli as hyprial_cli,
)
from feishu.transports import gateway as _transports_gateway
from feishu.transports.gateway import (
    ChildHarness as ChildHarness,
    stop_gateway as stop_gateway,
    write_command as write_command,
)
from feishu.scenarios import probes as _scenarios_probes
from feishu.scenarios.probes import (
    sdk_event as sdk_event,
    _RecordingHarness as _RecordingHarness,
    _SilentLark as _SilentLark,
)
from feishu import flow as _flow
from feishu import identity as _identity
from feishu.identity import (
    _chat_mode as _chat_mode,
    _bot_open_id as _bot_open_id,
    _identity_criteria as _identity_criteria,
)

# The contract target is the approved App's configured test GROUP chat: it
# is the shared, reproducible target any developer can join.  A p2p DM with
# the bot is a per-developer private conversation, so no DM chat id may ever
# be committed here; a developer running against their own DM pins it via
# HYPRIAL_E2E_LARK_CHAT_ID.  The runner asserts chat_type against the RESOLVED
# chat's actual mode (group or p2p), never a hard-coded one.  In group mode
# the platform only delivers messages that @mention the bot, so user sends
# carry the bot's @mention (resolved live, never hard-coded).
#: CI credential injection (e2e-macos-credentialed.yml).  When both are set the
#: runner takes the App credential from the environment and NEVER reads the
#: production ~/.hyprial config or secret files; a wrong injected id is a loud
#: preflight refusal, not a silent run against a production App.
CHANNELS_PATH = Path.home() / ".hyprial" / "channels.json"
SECRET_PATH = Path.home() / ".hyprial" / "secrets" / "lark-allenwoods-hyprial-e2e.json"
LARK_CLI = Path("/opt/homebrew/bin/lark-cli")
# Registered sender persona for the outbound CLI legs (Allen b).


def load_approved_gateway() -> dict[str, Any]:
    channels = load_json(CHANNELS_PATH)
    matches = [
        gateway
        for gateway in channels.get("gateways", [])
        if gateway.get("name") == APP_NAME and gateway.get("provider") == "lark"
    ]
    if len(matches) != 1 or matches[0].get("appId") != APP_ID:
        raise ContractBlocked("approved E2E App identity did not match local config")
    return matches[0]


def resolve_chat_id(gateway: Mapping[str, Any]) -> str:
    """Resolve the contract target chat from the approved App's gateway config.

    The App (name + app id) is the anchor and its configured ``py-test`` route
    is the single source of truth: the shared test group every developer can
    join. HYPRIAL_E2E_LARK_CHAT_ID overrides resolution explicitly (e.g. a
    developer's own DM with the bot -- a private conversation whose id must
    never be committed).  Missing or ambiguous configuration fails loudly
    instead of silently running against the wrong conversation.
    """

    override = os.environ.get(CHAT_ID_ENV_VAR, "").strip()
    if override:
        return override
    routes = [
        str(route["nativeId"])
        for route in gateway.get("routes", [])
        if isinstance(route, dict)
        and route.get("name") == TEST_ROUTE_NAME
        and route.get("type") == "direct"
        and isinstance(route.get("nativeId"), str)
        and route.get("nativeId")
    ]
    if len(routes) != 1:
        raise ContractBlocked(
            f"approved E2E App gateway must configure exactly one "
            f"{TEST_ROUTE_NAME!r} direct route (found {len(routes)}); "
            f"fix the gateway config or set "
            f"{CHAT_ID_ENV_VAR} to pin the target chat explicitly"
        )
    return routes[0]


@dataclass(frozen=True, slots=True)
class ApprovedTarget:
    app_id: str
    app_secret: str
    chat_id: str
    chat_mode: str  # "group" (shared test chat) | "p2p" (developer's own DM)
    # The bot's @mention, required in group mode: the platform only delivers
    # group messages that mention the bot.  None in p2p mode.
    mention: str | None
    bot_open_id: str | None
    # The caller's own p2p DM with the approved App's bot, resolved live from
    # the bot's open_id.  Non-text assertions (A11-A13) run here because the
    # group cannot deliver a message that carries no @mention.  Never
    # committed.
    dm_chat_id: str | None = None


def injected_app_credentials() -> tuple[str, str] | None:
    """App credential injected via the environment (CI controlled gate).

    Both variables must be set together, and the injected id must be the
    approved E2E test App: a missing partner variable or a foreign App id
    (a production App, above all) is a loud preflight refusal, never a
    silent fallback to another credential source.  This check runs before
    any file or network access.
    """

    app_id = os.environ.get(APP_ID_ENV_VAR, "").strip()
    app_secret = os.environ.get(APP_SECRET_ENV_VAR, "").strip()
    if not app_id and not app_secret:
        return None
    if not app_id or not app_secret:
        raise ContractBlocked(
            f"{APP_ID_ENV_VAR} and {APP_SECRET_ENV_VAR} must be set together"
        )
    if app_id != APP_ID:
        raise ContractBlocked(
            f"injected {APP_ID_ENV_VAR} does not match the approved E2E test App"
        )
    return APP_ID, app_secret


def load_approved_target(*, need_identity: bool) -> ApprovedTarget:
    injected = injected_app_credentials()
    if injected is not None:
        app_secret = injected[1]
        # Env mode never reads the production ~/.hyprial config: the contract
        # target chat must be pinned explicitly.
        chat_id = os.environ.get(CHAT_ID_ENV_VAR, "").strip()
        if not chat_id:
            raise ContractBlocked(
                f"env-injected credentials require {CHAT_ID_ENV_VAR}; the "
                "production gateway config is not read in this mode"
            )
    else:
        gateway = load_approved_gateway()
        secret = load_json(SECRET_PATH)
        app_secret = secret.get("appSecret")
        if not isinstance(app_secret, str) or not app_secret:
            raise ContractBlocked("approved E2E App secret is unavailable")
        chat_id = resolve_chat_id(gateway)
    # need_identity=False for the gateway child: it only needs the chat id,
    # and the parent has already run the full preflight before spawning it.
    if not need_identity:
        return ApprovedTarget(APP_ID, app_secret, chat_id, "", None, None)
    client = lark.Client.builder().app_id(APP_ID).app_secret(app_secret).build()
    mode = _chat_mode(client, chat_id)
    if mode is None:
        raise ContractBlocked(
            "the configured test route is not accessible to the approved E2E App"
        )
    if mode not in {"group", "p2p"}:
        raise ContractBlocked(
            f"the configured test route has unsupported chat_mode={mode!r}; "
            f"expected the shared test group or a developer-owned p2p DM"
        )
    mention = None
    bot_open_id = None
    if mode == "group":
        bot_open_id = _bot_open_id(APP_ID, app_secret)
        mention = f'<at user_id="{bot_open_id}">{APP_NAME}</at>'
    return ApprovedTarget(APP_ID, app_secret, chat_id, mode, mention, bot_open_id)


def _approved_app_secret() -> str:
    """The approved E2E App secret, from the existing credential sources.

    Env injection first, then the local secret file -- the same resolution
    order and refusal shapes `load_approved_target` uses.
    """

    injected = injected_app_credentials()
    if injected is not None:
        return injected[1]
    secret = load_json(SECRET_PATH)
    app_secret = secret.get("appSecret")
    if not isinstance(app_secret, str) or not app_secret:
        raise ContractBlocked("approved E2E App secret is unavailable")
    return app_secret


def resolve_bot_dm_chat_id() -> str:
    """Resolve the caller's own p2p DM with the approved App's bot.

    Non-text assertions cannot run in the shared test group: the platform only
    delivers group messages that @mention the bot, and `image`, `file` and
    `merge_forward` messages cannot carry a mention, so they never arrive.  The
    DM delivers every message type.  The id is resolved live by identity, not
    title -- a p2p entry's `p2p_target_id` is the peer's open_id, so the DM is
    the chat whose `p2p_target_id` equals `_bot_open_id(...)`; the bot's
    display name is arbitrary and never consulted -- and the id is a
    per-developer private conversation whose value must never be committed.
    The resolution proves itself: an event for this chat can only reach *this*
    App's gateway if the DM really is with this App's bot.
    """

    bot_open_id = _bot_open_id(APP_ID, _approved_app_secret())
    chats: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(10):
        arguments = [
            "im",
            "+chat-list",
            "--as",
            "user",
            "--types",
            "p2p",
            "--page-size",
            "100",
            "--json",
        ]
        if page_token:
            arguments.extend(["--page-token", page_token])
        data = lark_cli(*arguments).get("data")
        if not isinstance(data, dict):
            break
        page = data.get("chats") or data.get("items") or []
        chats.extend(item for item in page if isinstance(item, dict))
        page_token = data.get("page_token") if data.get("has_more") else None
        if not page_token:
            break
    # The bot's display name ("Harness Bridge feishu-e2e") is arbitrary and
    # Feishu may truncate it, so the peer identity in `p2p_target_id` is the
    # only reliable match key; require exactly one candidate.
    matches = [
        chat["chat_id"]
        for chat in chats
        if chat.get("chat_mode") == "p2p"
        and chat.get("p2p_target_id") == bot_open_id
        and isinstance(chat.get("chat_id"), str)
        and chat["chat_id"]
    ]
    if len(matches) != 1:
        raise ContractBlocked(
            f"non-text coverage needs exactly one p2p DM with {APP_NAME!r} "
            f"(found {len(matches)} among {len(chats)} p2p chats); open a DM "
            f"with the App's bot once from Feishu, then re-run"
        )
    return matches[0]


def verify_user_identity() -> dict[str, Any]:
    """Verify the lark-cli user identity, retrying transient verification.

    `auth status --verify` makes a live call to Feishu, so a single network
    hiccup or rate-limit must not be reported as "this machine is not set up":
    that misreading is why this scenario kept getting skipped.  Retry with
    backoff, and when it still fails, report the *actual* criteria that did not
    hold instead of one generic sentence.  Token material is never included.
    """

    attempts: list[str] = []
    for attempt, delay in enumerate((1.0, 2.0, 4.0, None), start=1):
        try:
            auth = lark_cli("auth", "status", "--json", "--verify")
        except ContractBlocked as error:
            attempts.append(f"attempt{attempt}=cli-error({error})")
        else:
            criteria = _identity_criteria(auth)
            if (
                criteria["user.present"]
                and criteria["user.status"] == "ready"
                and criteria["user.tokenStatus"] == "valid"
            ):
                return criteria
            attempts.append(
                f"attempt{attempt}="
                + ",".join(
                    f"{key}={value!r}"
                    for key, value in criteria.items()
                    if key != "user.present"
                )
            )
        if delay is None:
            break
        time.sleep(delay)
    raise ContractBlocked(
        "lark-cli user identity did not verify after "
        f"{len(attempts)} attempts (expected user.status='ready', "
        f"user.tokenStatus='valid'); observed: "
        + "; ".join(attempts)
        + "; "
        + _lark_reauth_check_summary()
    )


def _lark_reauth_check_summary() -> str:
    return _identity._lark_reauth_check_summary(_ports=_identity_ports())


_e2e_lark_profile_name: str | None = None


def e2e_lark_profile() -> str:
    """The lark-cli profile bound to the approved E2E App.

    lark-cli has no --app-id flag: the App identity rides on named profiles,
    and without an explicit --profile every call would run as whatever
    profile happens to be the local default (which is not the E2E App).  The
    name is resolved from `lark-cli profile list` once per run and cached;
    `profile list` itself never takes --profile.
    """

    global _e2e_lark_profile_name
    if _e2e_lark_profile_name is None:
        _e2e_lark_profile_name = _lookup_e2e_lark_profile()
    return _e2e_lark_profile_name


def _lookup_e2e_lark_profile() -> str:
    return _identity._lookup_e2e_lark_profile(_ports=_identity_ports())


def lark_cli(*arguments: str, cwd: Path | None = None) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update(
        {
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
    )
    result = subprocess.run(
        [str(LARK_CLI), "--profile", e2e_lark_profile(), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
    )
    if result.returncode != 0:
        raise ContractBlocked(
            f"lark-cli {' '.join(arguments[:2])} failed ({result.returncode}): "
            f"{safe_cli_error(result.stderr, result.stdout)}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ContractFailure("lark-cli returned non-JSON success output") from error
    if not isinstance(value, dict):
        raise ContractFailure("lark-cli JSON output must be an object")
    if value.get("ok") is False:
        raise ContractBlocked(
            f"lark-cli returned failure: {safe_cli_error('', result.stdout)}"
        )
    return value


def outbound_daemon_environment(home: Path, state: Path) -> dict[str, str]:
    """The second isolated daemon's environment, built the way _daemon_runner
    builds IsolatedDaemon's: the isolation defines the identity.

    #361 made the node's owner mandatory and U5 (#366) removed `init
    --owner`; an isolated home has no settings.json, so without HYPRIAL_OWNER
    `hyprial init` refuses to start ("user identity is not set" -- E2E-004 A16 on
    controlled #4971, the first run to reach A16 after #399).  A host shell's
    HYPRIAL_OWNER must not leak onto a throwaway node either, so the value is
    fixed here, not inherited (card 88fa866a).
    """
    environment = dict(os.environ)
    environment.update(
        {
            "HYPRIAL_OWNER": "e2e",
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NODE_ID": "e2e004-outbound-file",
        }
    )
    for name in (
        "HARNESS_SOCKET_PATH",
        "HYPRIAL_PYTHON",
        "HYPRIAL_ZENOH_CONNECT",
        "HYPRIAL_ZENOH_LISTEN",
        "HYPRIAL_WORKER_ACTOR",
        "HYPRIAL_WORKER_SESSION_REF",
        "HYPRIAL_MANAGED_WORKER",
    ):
        environment.pop(name, None)
    return environment


def stop_outbound_daemon(
    executable: Path | None, environment: Mapping[str, str] | None
) -> None:
    return _transports_outbound.stop_outbound_daemon(
        executable, environment, _ports=_transports_outbound_ports()
    )


def prepare_outbound_daemon(
    root: Path, *, chat_id: str
) -> tuple[Path, dict[str, str], dict[str, Any]]:
    return _transports_outbound.prepare_outbound_daemon(
        root, chat_id=chat_id, _ports=_transports_outbound_ports()
    )


def user_send(
    chat_id: str, text: str, idempotency_key: str, *, mention: str | None = None
) -> str:
    return _transports_messages.user_send(
        chat_id,
        text,
        idempotency_key,
        mention=mention,
        _ports=_transports_messages_ports(),
    )


def user_reply(
    message_id: str, text: str, idempotency_key: str, *, mention: str | None = None
) -> str:
    return _transports_messages.user_reply(
        message_id,
        text,
        idempotency_key,
        mention=mention,
        _ports=_transports_messages_ports(),
    )


#: An `image` message carries no text at all, so the adapter must synthesize a
#: readable stand-in.  Asserting the exact stand-in proves the agent receives
#: something intelligible rather than an empty body silently dropped later.
#: Since the 2026-08-14 ruling the stand-in also carries the
#: ``ref:<message_id>/<key>`` retrieval handle, so the expected text is
#: computed from the sent message's own key rather than a constant.


def sent_media_key(message_id: str, field: str) -> str:
    return _content.sent_media_key(message_id, field, _ports=_content_ports())


def expected_image_text(message_id: str) -> str:
    return _content.expected_image_text(message_id, _ports=_content_ports())


def expected_file_text(message_id: str) -> str:
    return _content.expected_file_text(message_id, _ports=_content_ports())


#: A 1x1 transparent PNG; the smallest artifact the platform accepts.


def user_send_content(
    chat_id: str, message_type: str, content: Mapping[str, Any], idempotency_key: str
) -> str:
    return _transports_messages.user_send_content(
        chat_id,
        message_type,
        content,
        idempotency_key,
        _ports=_transports_messages_ports(),
    )


def user_send_attachment(
    chat_id: str, flag: str, path: Path, idempotency_key: str
) -> str:
    return _transports_messages.user_send_attachment(
        chat_id, flag, path, idempotency_key, _ports=_transports_messages_ports()
    )


def get_message(message_id: str) -> dict[str, Any]:
    return _transports_messages.get_message(
        message_id, _ports=_transports_messages_ports()
    )


def get_onit_reactions(message_id: str) -> list[dict[str, Any]]:
    return _transports_messages.get_onit_reactions(
        message_id, _ports=_transports_messages_ports()
    )


def wait_for_evidence(
    path: Path,
    kind: str,
    predicate: Callable[[Mapping[str, Any]], bool] | None = None,
    *,
    timeout: float = EVENT_TIMEOUT_SECONDS,
    process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    return _evidence.wait_for_evidence(
        path,
        kind,
        predicate,
        timeout=timeout,
        process=process,
        _ports=_evidence_ports(),
    )


class ChildRoutes:
    def __init__(self, chat_id: str, dm_chat_id: str | None = None) -> None:
        self.chat_id = chat_id
        # The non-text assertions run in the bot's p2p DM, which routes by pin
        # rather than by mention; without it every DM message would resolve to
        # "no-target" and be dead-lettered for a reason of our own making.
        self.dm_chat_id = dm_chat_id

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return ACTOR if actor_key == ACTOR.actor_key else None

    def resolve_mention(self, name: str) -> ActorTarget | None:
        # The isolated test chat only ever mentions the test App's bot (group
        # mode requires the @mention for the platform to deliver at all), and
        # the contract addresses exactly one actor, so any mention resolves
        # to it.  Production _Routes maps mention names to configured agent
        # routes instead; this double is deliberately narrower.
        return ACTOR

    def routed_actor(self, conversation_id: str) -> ActorTarget | None:
        expected = {
            f"lark:{chat_id}:{chat_id}"
            for chat_id in (self.chat_id, self.dm_chat_id)
            if chat_id
        }
        return ACTOR if conversation_id in expected else None


def gateway_child(arguments: argparse.Namespace) -> int:
    return _transports_gateway.gateway_child(
        arguments, _ports=_transports_gateway_ports()
    )


class Reporter:
    def __init__(self) -> None:
        self.reported: set[str] = set()
        self.passed: set[str] = set()

    def pass_(self, assertion: str, evidence: str) -> None:
        self.reported.add(assertion)
        self.passed.add(assertion)
        print(
            f"ASSERT PASS E2E-004.{assertion} {redact_native_ids(evidence)}",
            flush=True,
        )

    def fail(self, assertion: str, error: str) -> None:
        self.reported.add(assertion)
        print(
            f"ASSERT FAIL E2E-004.{assertion} {redact_native_ids(error)}",
            flush=True,
        )

    def blocked(self, assertion: str, reason: str) -> None:
        self.reported.add(assertion)
        # BLOCKED goes to stderr: a quiet BLOCKED on stdout reads like a normal
        # finish, which is exactly how this scenario kept getting skipped.
        print(
            f"ASSERT BLOCKED E2E-004.{assertion} {redact_native_ids(reason)}",
            file=sys.stderr,
            flush=True,
        )

    def block_remaining(self, reason: str) -> None:
        for assertion in ASSERTIONS:
            if assertion not in self.reported:
                self.blocked(assertion, reason)

    def announce_blocked(self, reason: str) -> None:
        """Make a blocked run impossible to mistake for a passing run."""

        executed = len(self.passed)
        total = len(ASSERTIONS)
        headline = (
            f"NO ASSERTIONS WERE EXECUTED ({executed}/{total} verified) -- "
            "this run did NOT test anything"
            if executed == 0
            else f"RUN INCOMPLETE: only {executed}/{total} assertions verified"
        )
        for line in (
            "",
            "!" * 72,
            f"E2E-004 feishu-roundtrip BLOCKED -- {headline}",
            f"cause: {redact_native_ids(reason)}",
            "BLOCKED is not PASS: fix the cause above and re-run before "
            "treating this scenario as covered.",
            "!" * 72,
            "",
        ):
            print(line, file=sys.stderr, flush=True)


def start_gateway(
    root: Path,
    state_path: Path,
    evidence_path: Path,
    command_dir: Path,
    nonce: str,
    generation: int,
    dm_chat_id: str | None = None,
) -> tuple[subprocess.Popen[str], Any, Any]:
    return _transports_gateway.start_gateway(
        root,
        state_path,
        evidence_path,
        command_dir,
        nonce,
        generation,
        dm_chat_id,
        _ports=_transports_gateway_ports(),
    )


def wait_for_onit(message_id: str) -> list[dict[str, Any]]:
    return _transports_messages.wait_for_onit(
        message_id, _ports=_transports_messages_ports()
    )


def wait_for_no_onit(message_id: str) -> None:
    return _transports_messages.wait_for_no_onit(
        message_id, _ports=_transports_messages_ports()
    )


def issue_delivery(
    command_dir: Path, harness_message_id: str, text: str, suffix: str
) -> tuple[str, str]:
    return _transports_gateway.issue_delivery(
        command_dir,
        harness_message_id,
        text,
        suffix,
        _ports=_transports_gateway_ports(),
    )


def dump_dead_letters(
    command_dir: Path, evidence_path: Path, process: subprocess.Popen[str] | None
) -> list[dict[str, Any]]:
    return _transports_gateway.dump_dead_letters(
        command_dir, evidence_path, process, _ports=_transports_gateway_ports()
    )


def nontext_roundtrip(
    *,
    label: str,
    message_id: str,
    expected_message_type: str,
    expected_text: str | None,
    minimum_text_length: int,
    chat_id: str,
    chat_type: str,
    nonce: str,
    command_dir: Path,
    evidence_path: Path,
    process: subprocess.Popen[str] | None,
) -> dict[str, Any]:
    return _scenarios_probes.nontext_roundtrip(
        label=label,
        message_id=message_id,
        expected_message_type=expected_message_type,
        expected_text=expected_text,
        minimum_text_length=minimum_text_length,
        chat_id=chat_id,
        chat_type=chat_type,
        nonce=nonce,
        command_dir=command_dir,
        evidence_path=evidence_path,
        process=process,
        _ports=_scenarios_probes_ports(),
    )


def assert_provider_failure(root: Path, chat_id: str) -> dict[str, Any]:
    return _scenarios_probes.assert_provider_failure(
        root, chat_id, _ports=_scenarios_probes_ports()
    )


def _probe_adapter(state_path: Path, chat_id: str) -> tuple[Any, Any, Any]:
    return _scenarios_probes._probe_adapter(
        state_path, chat_id, _ports=_scenarios_probes_ports()
    )


def assert_merge_forward_not_dropped(root: Path, chat_id: str) -> dict[str, Any]:
    return _scenarios_probes.assert_merge_forward_not_dropped(
        root, chat_id, _ports=_scenarios_probes_ports()
    )


def assert_unsupported_type_is_loud(root: Path, chat_id: str) -> dict[str, Any]:
    return _scenarios_probes.assert_unsupported_type_is_loud(
        root, chat_id, _ports=_scenarios_probes_ports()
    )


def assert_live_unsupported_alarm(
    root: Path, *, app_secret: str, chat_id: str, original_message_id: str
) -> dict[str, Any]:
    return _scenarios_probes.assert_live_unsupported_alarm(
        root,
        app_secret=app_secret,
        chat_id=chat_id,
        original_message_id=original_message_id,
        _ports=_scenarios_probes_ports(),
    )


def run_contract() -> int:
    return _flow.run_contract(_ports=_flow_ports())


def _identity_ports() -> _identity.IdentityPorts:
    return _identity.IdentityPorts(
        APP_ID=APP_ID, LARK_CLI=LARK_CLI, entrypoint=__file__
    )


def _evidence_ports() -> _evidence.EvidencePorts:
    return _evidence.EvidencePorts(
        POLL_SECONDS=POLL_SECONDS,
    )


def _content_ports() -> _content.ContentPorts:
    return _content.ContentPorts(
        ATTACHMENT_FILE_NAME=ATTACHMENT_FILE_NAME,
        get_message=get_message,
    )


def _transports_messages_ports() -> _transports_messages.TransportsMessagesPorts:
    return _transports_messages.TransportsMessagesPorts(
        POLL_SECONDS=POLL_SECONDS,
        REACTION_TIMEOUT_SECONDS=REACTION_TIMEOUT_SECONDS,
        lark_cli=lark_cli,
        get_onit_reactions=get_onit_reactions,
    )


def _transports_outbound_ports() -> _transports_outbound.TransportsOutboundPorts:
    return _transports_outbound.TransportsOutboundPorts(
        APP_ID=APP_ID,
        APP_NAME=APP_NAME,
        OUTBOUND_SENDER=OUTBOUND_SENDER,
        SECRET_PATH=SECRET_PATH,
        entrypoint=__file__,
        injected_app_credentials=injected_app_credentials,
        outbound_daemon_environment=outbound_daemon_environment,
        hyprial_cli=hyprial_cli,
    )


def _transports_gateway_ports() -> _transports_gateway.TransportsGatewayPorts:
    return _transports_gateway.TransportsGatewayPorts(
        ACTOR=ACTOR,
        APP_ID=APP_ID,
        APP_NAME=APP_NAME,
        ChildRoutes=ChildRoutes,
        POLL_SECONDS=POLL_SECONDS,
        entrypoint=__file__,
        load_approved_target=load_approved_target,
        wait_for_evidence=wait_for_evidence,
        write_command=write_command,
    )


def _scenarios_probes_ports() -> _scenarios_probes.ScenariosProbesPorts:
    return _scenarios_probes.ScenariosProbesPorts(
        ACTOR=ACTOR,
        APP_ID=APP_ID,
        APP_NAME=APP_NAME,
        ChildRoutes=ChildRoutes,
        dump_dead_letters=dump_dead_letters,
        get_message=get_message,
        issue_delivery=issue_delivery,
        wait_for_evidence=wait_for_evidence,
    )


def _flow_ports() -> _flow.FlowPorts:
    return _flow.FlowPorts(
        ACTOR=ACTOR,
        APP_ID=APP_ID,
        APP_NAME=APP_NAME,
        ASSERTIONS=ASSERTIONS,
        ATTACHMENT_FILE_NAME=ATTACHMENT_FILE_NAME,
        LARK_CLI=LARK_CLI,
        OUTBOUND_SENDER=OUTBOUND_SENDER,
        Reporter=Reporter,
        TEST_ROUTE_NAME=TEST_ROUTE_NAME,
        TINY_PNG=TINY_PNG,
        assert_live_unsupported_alarm=assert_live_unsupported_alarm,
        assert_merge_forward_not_dropped=assert_merge_forward_not_dropped,
        assert_provider_failure=assert_provider_failure,
        assert_unsupported_type_is_loud=assert_unsupported_type_is_loud,
        expected_file_text=expected_file_text,
        expected_image_text=expected_image_text,
        get_message=get_message,
        hyprial_cli=hyprial_cli,
        issue_delivery=issue_delivery,
        lark_cli=lark_cli,
        load_approved_target=load_approved_target,
        nontext_roundtrip=nontext_roundtrip,
        prepare_outbound_daemon=prepare_outbound_daemon,
        resolve_bot_dm_chat_id=resolve_bot_dm_chat_id,
        start_gateway=start_gateway,
        stop_gateway=stop_gateway,
        stop_outbound_daemon=stop_outbound_daemon,
        user_reply=user_reply,
        user_send=user_send,
        user_send_attachment=user_send_attachment,
        user_send_content=user_send_content,
        verify_user_identity=verify_user_identity,
        wait_for_evidence=wait_for_evidence,
        wait_for_no_onit=wait_for_no_onit,
        wait_for_onit=wait_for_onit,
        write_command=write_command,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-child", action="store_true")
    parser.add_argument("--state")
    parser.add_argument("--evidence")
    parser.add_argument("--commands")
    parser.add_argument("--nonce")
    # Passed from the parent so the child does not repeat the live DM lookup;
    # the parent already resolved and preflighted it.
    parser.add_argument("--dm-chat")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.gateway_child:
        required = (
            arguments.state,
            arguments.evidence,
            arguments.commands,
            arguments.nonce,
        )
        if any(value is None for value in required):
            raise SystemExit(
                "gateway child requires state, evidence, commands, and nonce"
            )
        return gateway_child(arguments)
    return run_contract()


if __name__ == "__main__":
    raise SystemExit(main())
