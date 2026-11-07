"""Frozen R2h self-withdrawal and explicit publication contract."""

from __future__ import annotations

import base64
import json
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import InteractiveSession
from hyprial.daemon.impl.org.network.ipc import _publish_identity_binding_after_join
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.identity.test_identity_resolver_ipc import _application


OWNER = "owner@example"


def _proof(*, expires_at: float) -> str:
    encode = lambda value: base64.urlsafe_b64encode(  # noqa: E731 - fixture
        json.dumps(value).encode()
    ).rstrip(b"=").decode()
    return f"{encode({'alg': 'RS256'})}.{encode({'exp': expires_at})}.signature"


def _row(*, expires_at: float = 1.0) -> dict[str, str]:
    return {
        "user": OWNER,
        "larkUnionId": "on_owner",
        "proof": _proof(expires_at=expires_at),
        "publishedAt": "2026-10-08T00:00:00+00:00",
    }


def _write_settings(home: Path, row: dict[str, str]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "owner": OWNER,
                "identityMode": "casdoor",
                "identityIssuer": "https://auth.hyprial.com",
                "identityBinding": row,
            }
        ),
        encoding="utf-8",
    )


class _Directory:
    def __init__(self, orgs: tuple[str, ...] = ("group-a",)) -> None:
        self.joined = orgs
        self.rows: dict[str, dict[str, str]] = {}
        self.puts: list[str] = []
        self.removes: list[str] = []

    def orgs(self) -> list[str]:
        return list(self.joined)

    def put_binding(self, org: str, row: dict[str, str]) -> None:
        self.puts.append(org)
        self.rows[org] = dict(row)

    def remove_binding(self, org: str, user: str) -> bool:
        assert user == OWNER
        self.removes.append(org)
        return self.rows.pop(org, None) is not None


class _Projector:
    def __init__(self) -> None:
        self.scheduled: list[Callable[[], None]] = []
        self.retries: list[Callable[[], None]] = []

    def schedule_publish(
        self, _publish_at: float, publisher: Callable[[], None], **_kwargs: object
    ) -> None:
        self.scheduled.append(publisher)

    def retry_publish(
        self, publisher: Callable[[], None], **_kwargs: object
    ) -> None:
        self.retries.append(publisher)

    def refresh(self) -> None:
        return None


def _bare_app(
    tmp_path: Path,
    directory: _Directory,
    *,
    row: dict[str, str],
    projector: Any | None = None,
) -> DaemonApplication:
    home = tmp_path / "home"
    _write_settings(home, row)
    app = object.__new__(DaemonApplication)
    app.state_dir = tmp_path / "state"
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app.owner = OWNER
    app._org_identity_binding_projector = projector
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    return app


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"org": "group-a", "all": True},
        {"all": False},
        {"org": "group-a", "extra": True},
    ],
)
@pytest.mark.parametrize(
    "method", ["identity.binding.withdraw", "identity.binding.publish"]
)
def test_binding_ipc_requires_exactly_one_explicit_scope(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    method: str,
    params: dict[str, object],
) -> None:
    app = _application(request, tmp_path)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(method, params)

    assert refused.value.code == "INVALID_PARAMS"


@pytest.mark.parametrize(
    "method", ["identity.binding.withdraw", "identity.binding.publish"]
)
def test_agent_session_cannot_change_owner_binding_publication(
    request: pytest.FixtureRequest, tmp_path: Path, method: str
) -> None:
    app = _application(request, tmp_path)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            method,
            {"org": "group-a"},
            _trusted_message_origin="worker",
        )

    assert refused.value.code == "CALLER_NOT_AUTHORIZED"
    assert str(refused.value) == (
        "identity binding publication is available only to the local operator"
    )


@pytest.mark.parametrize("identity_field", ["actor", "sessionRef"])
@pytest.mark.parametrize(
    "method", ["identity.binding.withdraw", "identity.binding.publish"]
)
def test_binding_publication_refuses_any_session_identity_field(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    identity_field: str,
) -> None:
    app = _application(request, tmp_path)
    callback = (
        "_withdraw_identity_binding"
        if method.endswith("withdraw")
        else "_publish_identity_binding_explicit"
    )
    monkeypatch.setattr(
        app, callback, lambda **_kwargs: pytest.fail("agent call reached mutation")
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(method, {"org": "group-a", identity_field: "agent-session"})

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


@pytest.mark.parametrize(
    "method", ["identity.binding.withdraw", "identity.binding.publish"]
)
def test_registered_interactive_harness_pid_cannot_change_binding_publication(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    from hyprial.daemon.impl.mcp.channel import ownership

    app = _application(request, tmp_path)
    app.desired_state.register_interactive(
        InteractiveSession(
            actor="agent:allen:node:interactive",
            cwd=str(tmp_path),
            command=("codex",),
            source="codex-tui",
            session_ref="session-interactive",
            process_pid=4242,
            process_identity="birth:interactive",
        )
    )
    app._ipc_peer.pid = 4242
    monkeypatch.setattr(
        ownership,
        "_read_process_identity",
        lambda pid: "birth:interactive" if pid == 4242 else None,
    )
    callback = (
        "_withdraw_identity_binding"
        if method.endswith("withdraw")
        else "_publish_identity_binding_explicit"
    )
    monkeypatch.setattr(
        app, callback, lambda **_kwargs: pytest.fail("agent call reached mutation")
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(method, {"org": "group-a"})

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_operator_binding_ipc_dispatches_each_explicit_scope(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _application(request, tmp_path)
    app.owner = OWNER
    calls: list[tuple[str, str | None, bool]] = []
    monkeypatch.setattr(
        app,
        "_withdraw_identity_binding",
        lambda *, org, all_orgs, **_operator: calls.append(("withdraw", org, all_orgs))
        or {"orgs": [org]},
    )
    monkeypatch.setattr(
        app,
        "_publish_identity_binding_explicit",
        lambda *, org, all_orgs, **_operator: calls.append(("publish", org, all_orgs))
        or {"orgs": []},
    )

    assert app.handle("identity.binding.withdraw", {"org": "group-a"}) == {
        "orgs": ["group-a"]
    }
    assert app.handle("identity.binding.publish", {"all": True}) == {"orgs": []}
    assert calls == [
        ("withdraw", "group-a", False),
        ("publish", None, True),
    ]


def test_binding_mutation_reports_unverified_operator_and_lists_attribution(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _application(request, tmp_path)
    app.owner = OWNER
    directory = _Directory()
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(app, "_org_binding_directory", lambda: directory)
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    app._ipc_peer.pid = 5151

    result = app.handle("identity.binding.withdraw", {"org": "group-a"})
    listed = app.handle("identity.bindings.list", {})

    assert result == {"orgs": ["group-a"], "operatorVerified": False}
    assert (
        "identity.binding.withdrawn",
        {
            "orgs": ["group-a"],
            "operator": "unverified",
            "operatorVerified": False,
            "callerPid": 5151,
        },
    ) in events
    assert listed["publicationSuppression"] == {
        "all": False,
        "except": [],
        "orgs": ["group-a"],
        "setBy": {
            "orgs": {
                "group-a": {
                    "operator": "unverified",
                    "operatorVerified": False,
                    "callerPid": 5151,
                }
            }
        },
    }


def test_withdrawal_is_atomic_0600_and_survives_restart_login_and_join(
    tmp_path: Path,
) -> None:
    row = _row()
    directory = _Directory()
    directory.rows["group-a"] = dict(row)
    app = _bare_app(tmp_path, directory, row=row)

    result = app._withdraw_identity_binding(org="group-a", all_orgs=False)

    assert result == {"orgs": ["group-a"], "operatorVerified": False}
    assert directory.rows == {}
    state_path = app.state_dir / "identity-binding-suppression.json"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "all": False,
        "except": [],
        "orgs": ["group-a"],
        "setBy": {
            "orgs": {
                "group-a": {
                    "operator": "unverified",
                    "operatorVerified": False,
                }
            }
        },
    }

    # A daemon restart and a later plain login both retain the operator's intent.
    restarted = _bare_app(tmp_path, directory, row=row)
    restarted._publish_identity_binding()
    _publish_identity_binding_after_join(restarted)
    assert directory.puts == []

    assert restarted._publish_identity_binding_explicit(
        org="group-a", all_orgs=False
    ) == {"orgs": ["group-a"], "operatorVerified": False}
    assert directory.puts == ["group-a"]
    assert directory.rows["group-a"] == row


def test_withdraw_all_suppresses_future_org_until_explicit_per_org_publish(
    tmp_path: Path,
) -> None:
    row = _row()
    directory = _Directory(("group-a",))
    directory.rows["group-a"] = dict(row)
    app = _bare_app(tmp_path, directory, row=row)

    app._withdraw_identity_binding(org=None, all_orgs=True)
    directory.joined = ("group-a", "group-future")
    app._publish_identity_binding()
    assert directory.puts == []

    app._publish_identity_binding_explicit(org="group-future", all_orgs=False)
    assert directory.puts == ["group-future"]
    app._publish_identity_binding()
    assert directory.puts == ["group-future", "group-future"]


def test_withdrawal_deletes_a_held_unpublished_proof(tmp_path: Path) -> None:
    live = _row(expires_at=time.time() + 3_600)
    directory = _Directory()
    app = _bare_app(tmp_path, directory, row=live)

    app._withdraw_identity_binding(org="group-a", all_orgs=False)

    settings = json.loads(
        (app.hyprial_home / "settings.json").read_text(encoding="utf-8")
    )
    assert "identityBinding" not in settings
    assert stat.S_IMODE((app.hyprial_home / "settings.json").stat().st_mode) == 0o600


def test_withdrawal_fences_already_scheduled_deferred_and_retry_callbacks(
    tmp_path: Path,
) -> None:
    # A deferred callback already owned by the projector must re-check intent.
    directory = _Directory()
    projector = _Projector()
    live = _row(expires_at=time.time() + 3_600)
    app = _bare_app(tmp_path, directory, row=live, projector=projector)
    app._publish_identity_binding()
    assert len(projector.scheduled) == 1
    deferred = projector.scheduled.pop()
    app._withdraw_identity_binding(org="group-a", all_orgs=False)
    deferred()
    assert directory.puts == []

    # The same fence applies to an already queued retry of an expired proof.
    expired = _row()
    _write_settings(app.hyprial_home, expired)
    app._publish_identity_binding_explicit(org="group-a", all_orgs=False)

    class FailingOnce(_Directory):
        def put_binding(self, org: str, row: dict[str, str]) -> None:
            self.puts.append(org)
            if len(self.puts) == 1:
                raise RuntimeError("transient")
            self.rows[org] = dict(row)

    failing = FailingOnce()
    app._org_binding_directory = lambda: failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        app._publish_identity_binding()
    assert len(projector.retries) == 1
    retry = projector.retries.pop()
    app._withdraw_identity_binding(org="group-a", all_orgs=False)
    retry()
    assert failing.puts == ["group-a"]


def test_withdraw_serializes_with_publish_already_inside_put(tmp_path: Path) -> None:
    row = _row()
    entered_put = threading.Event()
    release_put = threading.Event()

    class BlockingDirectory(_Directory):
        def put_binding(self, org: str, value: dict[str, str]) -> None:
            entered_put.set()
            assert release_put.wait(1.0)
            super().put_binding(org, value)

    directory = BlockingDirectory()
    app = _bare_app(tmp_path, directory, row=row)
    publish = threading.Thread(target=app._publish_identity_binding)
    withdraw = threading.Thread(
        target=lambda: app._withdraw_identity_binding(
            org="group-a", all_orgs=False
        )
    )
    publish.start()
    assert entered_put.wait(1.0)
    withdraw.start()
    withdraw.join(0.05)
    serialized = withdraw.is_alive()
    release_put.set()
    publish.join(1.0)
    withdraw.join(1.0)

    assert serialized
    assert not publish.is_alive()
    assert not withdraw.is_alive()
    assert directory.rows == {}


def test_corrupt_settings_refuses_withdraw_without_partial_state(tmp_path: Path) -> None:
    directory = _Directory()
    directory.rows["group-a"] = _row()
    app = _bare_app(tmp_path, directory, row=_row())
    (app.hyprial_home / "settings.json").write_text("{", encoding="utf-8")

    with pytest.raises(DaemonRequestError) as refused:
        app._withdraw_identity_binding(org="group-a", all_orgs=False)

    assert refused.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
    assert directory.rows["group-a"] == _row()
    assert not (app.state_dir / "identity-binding-suppression.json").exists()


@pytest.mark.parametrize("operation", ["withdraw", "publish"])
def test_unknown_org_is_rejected_before_suppression_changes(
    tmp_path: Path, operation: str
) -> None:
    directory = _Directory(("group-a",))
    app = _bare_app(tmp_path, directory, row=_row())
    mutation = (
        app._withdraw_identity_binding
        if operation == "withdraw"
        else app._publish_identity_binding_explicit
    )

    with pytest.raises(DaemonRequestError) as refused:
        mutation(org="group-unknown", all_orgs=False)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "group-unknown" in str(refused.value)
    assert directory.puts == []
    assert directory.removes == []
    assert not (app.state_dir / "identity-binding-suppression.json").exists()


@pytest.mark.parametrize("operation", ["withdraw", "publish"])
def test_corrupt_suppression_refuses_without_partial_state(
    tmp_path: Path, operation: str
) -> None:
    """B1: validation precedes the first deletion; the refusal is a typed error."""

    live = _row(expires_at=time.time() + 3_600)
    directory = _Directory()
    directory.rows["group-a"] = _row()
    app = _bare_app(tmp_path, directory, row=live)
    suppression = app.state_dir / "identity-binding-suppression.json"
    suppression.parent.mkdir(parents=True, exist_ok=True)
    suppression.write_text("{", encoding="utf-8")
    settings_before = (app.hyprial_home / "settings.json").read_text(encoding="utf-8")

    call = (
        app._withdraw_identity_binding
        if operation == "withdraw"
        else app._publish_identity_binding_explicit
    )
    with pytest.raises(DaemonRequestError) as refused:
        call(org="group-a", all_orgs=False)

    assert refused.value.code == ipc_errors.IDENTITY_SOURCE_UNAVAILABLE
    assert (app.hyprial_home / "settings.json").read_text(encoding="utf-8") == settings_before
    assert directory.rows["group-a"] == _row()
    assert directory.puts == []
    assert suppression.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize(
    ("recorded", "observed", "interactive"),
    [
        # One birth, read by two readers with different scheme sets.
        ("proc-starttime:100", "proc-starttime:100;ps-lstart:Thu Oct  8 00:00:00 2026", True),
        # The PID was reused: every shared scheme differs.
        ("proc-starttime:100", "proc-starttime:200", False),
    ],
)
def test_interactive_peer_matches_process_birth_component_wise(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded: str,
    observed: str,
    interactive: bool,
) -> None:
    """M2: the peer's birth is compared per scheme, so PID reuse is not an agent."""

    from hyprial.daemon.impl.mcp.channel import ownership

    app = _application(request, tmp_path)
    app.desired_state.register_interactive(
        InteractiveSession(
            actor="agent:allen:node:interactive",
            cwd=str(tmp_path),
            command=("codex",),
            source="codex-tui",
            session_ref="session-interactive",
            process_pid=4242,
            process_identity=recorded,
        )
    )
    monkeypatch.setattr(
        ownership, "_read_process_identity", lambda pid: observed if pid == 4242 else None
    )

    assert app._interactive_identity_binding_peer(4242) is interactive
