"""O2c/A2: login asks per invite, and a machine joins nothing (D6 deleted).

The shell's part of the slice is a transport adapter: read the account with
the credential the shell alone holds, and let the daemon decide and join.
These tests drive the *login* composition with a fake Casdoor account and a
fake daemon that answers the documented ``org.invites.*`` contract, so the
assertions are about the composition — how many prompts, how many joins,
what the login result may carry — not about the daemon rules, which
``tests/daemon/org_membership/invites`` owns.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity import (
    DeviceRecord,
    InviteLink,
    bootstrap_yaml,
    default_bootstrap,
    encode_invite,
    invite_views,
    select_invites,
    write_device_record,
)
from hyprial.shell.impl.cli.commands.login import aftercare as login_aftercare
from hyprial.shell.impl.cli.commands.login.invites import prompts as prompts_module
from hyprial.shell.impl.invites import pending as pending_module

_DEVICE_ID = "node-a"
_REFRESH_TOKEN = "refresh-token-secret"


@pytest.mark.parametrize("field", ["pending", "failed"])
def test_partial_invite_human_output_names_missing_step(field):
    from hyprial.shell.impl.cli.commands.org.member.render import render_pending

    text = render_pending({field: [{"org": "group-acme", "inviter": "bob",
        "code": "no-holder-online", "status": "partial", "missingStep": "acl-applied"}]})
    assert "partial" in text
    assert "missing step: acl-applied" in text


def _payload(org: str, *, inviter: str = "bob") -> str:
    workflow = bootstrap_yaml(default_bootstrap())
    link = InviteLink(
        org=org,
        space_id=f"space-{org}",
        acl_space_id=f"space-{org}-acl",
        inviter=inviter,
        inviter_device="node-b",
        inviter_address="tcp://10.0.0.1:7447#psk",
        token="t" * 32,
        workflow_yaml=workflow,
        workflow_sha256=hashlib.sha256(workflow.encode()).hexdigest(),
        expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat(),
        tailnet={
            "version": 2,
            "tailnetId": f"space-{org}",
            "inviteId": f"invite-{org}",
            "token": "t" * 43,
            "owner": "alice",
            "expiresAtMs": int(
                (datetime.now(UTC) + timedelta(hours=24)).timestamp() * 1000
            ),
            "authority": {
                "owner": inviter,
                "deviceId": "node-b",
                "masterPublic": "nodekey:" + "a" * 64,
                "serverPublic": "nodekey:" + "b" * 64,
                "address": "tcp://10.0.0.1:7447#psk",
                "controlPort": 7448,
            },
        },
    )
    return encode_invite(link).removeprefix("hyprial-invite:v2:")


def _row(org: str, *, inviter: str = "bob", payload: str | None = None) -> dict:
    return {
        "payload": payload if payload is not None else _payload(org, inviter=inviter),
        "acceptedAt": "2026-10-31T00:00:00Z",
    }


class _AccountClient:
    """The Casdoor account read: one snapshot, no writes."""

    def __init__(self, issuer: str, token: str, rows: list[dict]) -> None:
        self.issuer = issuer
        self.token = token
        self._rows = rows

    def get_invites(self) -> list[dict]:
        return list(self._rows)


class _Daemon:
    """The documented ``org.invites.*`` contract over the shared pure rules."""

    def __init__(
        self,
        *,
        held: tuple[str, ...] = (),
        left: tuple[str, ...] = (),
        accept_failure: BaseException | None = None,
    ) -> None:
        self.held = held
        self.left = left
        self.accept_failure = accept_failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.accepted: list[str] = []
        self.timeouts: dict[str, float | None] = {}

    def request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        self.timeouts[method] = timeout
        if method == "org.invites.list":
            views = invite_views(params["invites"])
            selection = select_invites(
                views, held_spaces=self.held, left_orgs=self.left
            )
            return {
                "ok": True,
                "pending": selection.pending_rows(),
                "settled": len(selection.settled),
                "duplicates": len(selection.duplicates),
                "warnings": [],
            }
        if method == "org.invites.accept":
            if self.accept_failure is not None:
                raise self.accept_failure
            self.accepted.append(params["link"])
            return {
                "ok": True,
                "inviteId": params["inviteId"],
                "org": "group-acme",
                "spaceId": "space-group-acme",
                "joined": True,
                "alreadyMember": False,
                "steps": [],
            }
        raise AssertionError(f"unexpected daemon method {method}")

    @property
    def accepts(self) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if call[0] == "org.invites.accept"]


class _Services:
    def __init__(self, daemon: _Daemon, *, isatty: bool) -> None:
        self.runtime = daemon
        self._isatty = isatty

    def _stdin_isatty(self) -> bool:
        return self._isatty


class _CodedError(RuntimeError):
    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(message)
        self.code = code


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    write_device_record(
        home,
        DeviceRecord(
            device_id=_DEVICE_ID,
            owner="alice",
            server_public="nodekey:server-a",
            client_public="nodekey:client-a",
        ),
    )
    secrets = home / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "login.json").write_text(
        json.dumps(
            {
                "version": 1,
                "issuer": "https://issuer.example",
                "refreshToken": _REFRESH_TOKEN,
                "obtainedAt": "2026-10-31T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return home


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict],
    daemon: _Daemon,
    isatty: bool,
    answers: list[bool] | None = None,
) -> list[str]:
    """Fake the account read, the token refresh and the CLI service bundle."""

    asked: list[str] = []

    def fake_ask(question: str) -> bool:
        asked.append(question)
        if answers is None:
            raise AssertionError("no prompt may be asked here")
        return answers.pop(0)

    monkeypatch.setattr(prompts_module, "ask", fake_ask)
    monkeypatch.setattr(
        pending_module,
        "CasdoorAccountClient",
        lambda issuer, token: _AccountClient(issuer, token, rows),
    )
    monkeypatch.setattr(
        pending_module, "_access_token", lambda home, issuer, now: "access-token"
    )
    monkeypatch.setattr(
        login_aftercare, "get_services", lambda: _Services(daemon, isatty=isatty)
    )
    return asked


def _pickup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rows: list[dict],
    daemon: _Daemon,
    isatty: bool,
    json_output: bool = False,
    answers: list[bool] | None = None,
) -> tuple[dict[str, Any], list[str], _Daemon]:
    home = _home(tmp_path)
    asked = _wire(
        monkeypatch, rows=rows, daemon=daemon, isatty=isatty, answers=answers
    )
    outcome = login_aftercare._pickup_org_invites_after_login(
        home, json_output=json_output
    )
    return outcome, asked, daemon


def test_a_tty_answers_each_invite_and_only_yes_joins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon()
    rows = [_row("group-acme"), _row("group-beta", inviter="carol")]

    outcome, asked, daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=rows,
        daemon=daemon,
        isatty=True,
        answers=[True, False],
    )

    assert outcome["status"] == "ok"
    assert [row["org"] for row in outcome["joined"]] == ["group-acme"]
    assert [row["org"] for row in outcome["declined"]] == ["group-beta"]
    assert outcome["pending"] == []
    assert len(asked) == 2 and "group-acme" in asked[0] and "carol" in asked[1]
    assert len(daemon.accepted) == 1
    assert daemon.accepted[0].startswith("v2.")
    assert daemon.timeouts["org.invites.accept"] == 120.0
    assert daemon.timeouts["org.invites.list"] is None
    # The join is the daemon's invite-accept, not a second join surface.
    assert [method for method, _params in daemon.calls] == [
        "org.invites.list",
        "org.invites.accept",
    ]


def test_a_non_tty_run_joins_nothing_and_only_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("group-acme"), _row("group-beta")]

    outcome, asked, daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=rows,
        daemon=_Daemon(),
        isatty=False,
    )

    assert asked == []
    assert daemon.accepted == []
    assert [row["org"] for row in outcome["pending"]] == ["group-acme", "group-beta"]
    assert outcome["joined"] == [] and outcome["declined"] == []


def test_json_output_never_prompts_even_on_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("group-acme")]

    outcome, asked, daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=rows,
        daemon=_Daemon(),
        isatty=True,
        json_output=True,
        answers=None,
    )

    assert asked == []
    assert daemon.accepted == []
    assert [row["org"] for row in outcome["pending"]] == ["group-acme"]


def test_two_rows_for_one_space_are_joined_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload("group-acme")
    rows = [_row("group-acme", payload=payload), _row("group-acme", payload=payload)]

    _outcome, _asked, daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=rows,
        daemon=_Daemon(),
        isatty=True,
        answers=[True],
    )

    assert len(daemon.accepted) == 1


def test_an_org_this_node_left_is_not_joined_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("group-left")]

    outcome, _asked, daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=rows,
        daemon=_Daemon(left=("group-left",)),
        isatty=True,
        answers=None,
    )

    assert daemon.accepted == []
    assert outcome["pending"] == []
    assert outcome["skipped"] == 1


def test_one_invalid_invite_fails_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("group-acme")]

    outcome, _asked, _daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=rows,
        daemon=_Daemon(accept_failure=_CodedError("INVITE_EXPIRED")),
        isatty=True,
        answers=[True],
    )

    assert outcome["status"] == "ok"
    assert outcome["joined"] == []
    assert [row["code"] for row in outcome["failed"]] == ["INVITE_EXPIRED"]
    assert outcome["failed"][0]["org"] == "group-acme"


def test_the_pickup_result_carries_no_payload_and_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload("group-acme")

    outcome, _asked, _daemon = _pickup(
        monkeypatch,
        tmp_path,
        rows=[_row("group-acme", payload=payload)],
        daemon=_Daemon(),
        isatty=True,
        answers=[True],
    )

    rendered = json.dumps(outcome)
    assert payload not in rendered
    assert _REFRESH_TOKEN not in rendered
    assert "access-token" not in rendered


def test_an_unreachable_daemon_never_fails_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Broken(_Daemon):
        def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise _CodedError("DAEMON_NOT_READY")

    home = _home(tmp_path)
    _wire(monkeypatch, rows=[_row("group-acme")], daemon=_Broken(), isatty=False)

    outcome = login_aftercare._pickup_org_invites_after_login(home, json_output=True)

    assert outcome["status"] == "failed"
    assert outcome["code"] == "DAEMON_NOT_READY"


def test_accept_all_is_still_available_but_only_when_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--accept-all`` is the one explicit bulk join (``org pending``)."""
    daemon = _Daemon()
    home = _home(tmp_path)
    _wire(
        monkeypatch, rows=[_row("group-acme"), _row("group-beta")], daemon=daemon,
        isatty=False,
    )

    outcome = pending_module.run_pending(
        home,
        accept_all=True,
        confirm=None,
        request=daemon.request,
    )

    assert [row["org"] for row in outcome["joined"]] == ["group-acme", "group-beta"]
    assert len(daemon.accepted) == 2


def test_d6_is_deleted_from_the_login_path() -> None:
    """No silent accept-everything default survives in the login flow."""

    source = Path(login_aftercare.__file__).read_text(encoding="utf-8")
    assert "accept_all=True" not in source
    assert "org.join" not in source
