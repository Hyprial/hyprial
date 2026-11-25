"""E2E-004/013: the p2p DM resolver matches by open_id, lark_cli pins the App profile.

The controlled runner finds the developer's DM with the test bot by
`p2p_target_id == bot open_id` -- never by the bot's arbitrary display name
("Harness Bridge feishu-e2e" is never a prefix of the App name, so the old
title predicate permanently found 0).  And because lark-cli carries App
identity in named profiles, every lark_cli call must prepend the profile
whose appId is the approved E2E App, resolved from `lark-cli profile list`
at run time (the local default profile is a different App).

Both runners carry their own copy of the resolver and lark_cli
(run-all-harness-lifecycle.py is a copy, not an import), so every test runs
against both modules.  Nothing here calls the real lark-cli or Feishu: only
`lark_cli`, `subprocess.run` and `_bot_open_id` are stubbed, with the
field shapes measured on dev (chat-list p2p entries: chat_id, chat_mode,
chat_status, external, name, p2p_target_id, p2p_target_type, tenant_key;
profile-list entries: name, appId, brand, active, user, tokenStatus).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCENARIOS = ROOT / "contract" / "e2e"
RUN_004 = SCENARIOS / "run-feishu-roundtrip.py"
RUN_013 = SCENARIOS / "run-all-harness-lifecycle.py"

TEST_APP_ID = "cli_aaf9ea19d4b8dbe4"
TEST_APP_NAME = "allenwoods-hyprial-e2e"
# The local default profile is a different (production-side) App.
DEFAULT_PROFILE = {
    "name": "cli_aae3d3fb47799cfd",
    "appId": "cli_aae3d3fb47799cfd",
    "brand": "feishu",
    "active": True,
    "user": "stub-user",
    "tokenStatus": "valid",
}
E2E_PROFILE_NAME = "hyprial-e2e-stub-profile"
E2E_PROFILE = {
    "name": E2E_PROFILE_NAME,
    "appId": TEST_APP_ID,
    "brand": "feishu",
    "active": False,
    "user": "stub-user",
    "tokenStatus": "valid",
}
BOT_OPEN_ID = "ou_00000000000000000000000000000000"

RUNNER_CASES = [
    pytest.param((RUN_004, "APP_ID", "blocked"), id="e2e004"),
    pytest.param((RUN_013, "LARK_APP_ID", "assertion"), id="e2e013"),
]


def load_runner(path: Path, name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def p2p_chat(chat_id: str, name: str, p2p_target_id: str) -> dict[str, Any]:
    """A chat-list p2p entry with the exact field set measured on dev."""

    return {
        "chat_id": chat_id,
        "chat_mode": "p2p",
        "chat_status": "normal",
        "external": False,
        "name": name,
        "p2p_target_id": p2p_target_id,
        "p2p_target_type": "open_id",
        "tenant_key": "stub-tenant",
    }


def chat_page(chats: list[dict[str, Any]]) -> dict[str, Any]:
    """What the real lark_cli returns for one exhausted chat-list page."""

    return {"data": {"chats": chats, "has_more": False}}


@pytest.fixture(params=RUNNER_CASES)
def runner(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    path, app_id_attr, _error_kind = request.param
    module = load_runner(path, f"bot_dm_by_open_id_{request.param_index}")
    # Env-injected credential: the runners' existing controlled path, so the
    # resolver never reads production ~/.hyprial files during the test.
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_ID", TEST_APP_ID)
    monkeypatch.setenv("HYPRIAL_E2E_LARK_APP_SECRET", "stub-secret")
    monkeypatch.setattr(module, "_bot_open_id", lambda *_args: BOT_OPEN_ID)
    return module


def blocked_error(module):  # type: ignore[no-untyped-def]
    # E2E-004 refuses with ContractBlocked; the E2E-013 copy raises plain
    # AssertionError (its own refusal form).
    return getattr(module, "ContractBlocked", AssertionError)


# -- T1/T5: the resolver picks the chat whose p2p_target_id is the bot -------


def test_resolver_matches_p2p_target_id_not_title(runner) -> None:  # type: ignore[no-untyped-def]
    chats = [
        # The bot's real display name -- never a prefix of the App name.
        p2p_chat("oc_dm_with_bot", "Harness Bridge feishu-e2e", BOT_OPEN_ID),
        # A decoy whose title the old predicate would have matched.
        p2p_chat("oc_decoy_by_title", "allenwoods-hyprial", "ou_11111111111111111111111111111111"),
        p2p_chat("oc_unrelated", "某同事", "ou_22222222222222222222222222222222"),
    ]
    runner.lark_cli = lambda *arguments, **_kwargs: chat_page(chats)  # type: ignore[assignment]

    assert runner.resolve_bot_dm_chat_id() == "oc_dm_with_bot"


# -- T2: no p2p_target_id match keeps the existing found-0 refusal ----------


def test_resolver_without_matching_target_keeps_found_zero_refusal(
    runner,
) -> None:  # type: ignore[no-untyped-def]
    chats = [
        p2p_chat("oc_decoy_by_title", "allenwoods-hyprial", "ou_11111111111111111111111111111111"),
        p2p_chat("oc_named_like_app", TEST_APP_NAME, "ou_22222222222222222222222222222222"),
    ]
    runner.lark_cli = lambda *arguments, **_kwargs: chat_page(chats)  # type: ignore[assignment]

    with pytest.raises(blocked_error(runner), match="found 0"):
        runner.resolve_bot_dm_chat_id()


# -- T3/T5: every lark_cli call carries the E2E App profile -----------------


def test_chat_list_argv_pins_the_e2e_app_profile(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    chats = [p2p_chat("oc_dm_with_bot", "Harness Bridge feishu-e2e", BOT_OPEN_ID)]
    seen_argv: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        seen_argv.append(list(argv))
        if argv[1:3] == ["profile", "list"]:
            stdout: str = json.dumps([DEFAULT_PROFILE, E2E_PROFILE])
        else:
            stdout = json.dumps(chat_page(chats))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_e2e_lark_profile_name", None)

    assert runner.resolve_bot_dm_chat_id() == "oc_dm_with_bot"
    chat_list_argv = next(
        argv for argv in seen_argv if "chat-list" in " ".join(argv[1:])
    )
    assert "--profile" in chat_list_argv
    assert chat_list_argv[chat_list_argv.index("--profile") + 1] == E2E_PROFILE_NAME
    assert DEFAULT_PROFILE["name"] not in chat_list_argv
    # profile list itself is looked up without --profile.
    profile_argv = next(
        argv for argv in seen_argv if argv[1:3] == ["profile", "list"]
    )
    assert "--profile" not in profile_argv


# -- T4: a profile list without the E2E App is a loud refusal ---------------


def test_missing_e2e_profile_is_blocked_loudly(
    runner, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        assert argv[1:3] == ["profile", "list"]
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps([DEFAULT_PROFILE]),
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_e2e_lark_profile_name", None)

    with pytest.raises(blocked_error(runner), match=TEST_APP_ID) as error:
        runner.e2e_lark_profile()
    assert "no profile for E2E app" in str(error.value)
