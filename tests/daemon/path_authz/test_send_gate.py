"""message.send resourcePaths go through path_authz for every caller kind.

The gap this closes: any socket caller could name any file the daemon can
read (``~/.hyprial/secrets`` included) and have it uploaded to a route.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import os
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.daemon.routing.test_route_outbound import FakeChatGateway, FakeInbox, _home

ROUTE = "route:alicesmith-hyprial-e2e:py-test"
ACTOR_NAME = "worker"
ACTOR = f"agent:alice:sender-mac:{ACTOR_NAME}"
SESSION = "session-worker"
APP_NAME = "kanban"
APP_SESSION = "session-kanban"
OTHER_APP_NAME = "calendar"
OTHER_APP_SESSION = "session-calendar"
IMPOSTOR_NAME = "not-kanban"
IMPOSTOR_SESSION = "session-not-kanban"


@pytest.fixture
def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = Path(os.path.realpath(tmp_path))
    monkeypatch.delenv("HYPRIAL_SENDER_AUTH_MODE", raising=False)  # observe
    home = _home(root)
    repo = root / "repo"
    repo.mkdir()
    outside = root / "outside"
    outside.mkdir()
    gateway = FakeChatGateway()
    state = home / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="sender-mac",
        hyprial_home=home,
        owner="alice",
    **_deep_module_daemon_dependencies())
    app._inbox = FakeInbox()
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        LarkSdkGateway, "from_credentials", lambda app_id, secret, **_: gateway
    )
    app.handle("agent.create", {"name": ACTOR_NAME})
    app.handle(
        "session.register",
        {
            "actor": ACTOR_NAME,
            "sessionRef": SESSION,
            "cwd": str(repo),
            "command": ["test-carrier"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )
    app_homes: dict[str, Path] = {}
    for name, session_ref, app_dir in (
        (APP_NAME, APP_SESSION, APP_NAME),
        (OTHER_APP_NAME, OTHER_APP_SESSION, OTHER_APP_NAME),
        (IMPOSTOR_NAME, IMPOSTOR_SESSION, APP_NAME),
    ):
        agent_home = home / "apps" / app_dir / "agent-home"
        reports = home / "apps" / app_dir / "reports"
        agent_home.mkdir(parents=True, exist_ok=True)
        reports.mkdir(parents=True, exist_ok=True)
        app.handle("agent.create", {"name": name})
        app.handle(
            "session.register",
            {
                "actor": name,
                "sessionRef": session_ref,
                "cwd": str(agent_home),
                "command": ["test-app-carrier"],
                "source": "claude-channel",
                "runtime": "claude_interactive",
            },
        )
        app_homes[name] = agent_home
    workspace = home / "agents" / ACTOR_NAME / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "app": app,
        "gateway": gateway,
        "home": home,
        "repo": repo,
        "outside": outside,
        "workspace": workspace,
        "app_homes": app_homes,
        "app_reports": home / "apps" / APP_NAME / "reports",
        "other_app_reports": home / "apps" / OTHER_APP_NAME / "reports",
    }


def _ipc_send(app: DaemonApplication, sender: dict[str, str], path: Path):
    return app.handle(
        "message.send",
        {
            **sender,
            "to": [ROUTE],
            "summary": "see attachment",
            "message": "see attachment",
            "idempotencyKey": f"op-{path.name}",
            "resourcePaths": [{"path": str(path), "kind": "file"}],
        },
        _trusted_message_origin="ipc",
    )


_SESSION = {"from": ACTOR_NAME, "sessionRef": SESSION}
_APP_SESSION = {"from": APP_NAME, "sessionRef": APP_SESSION}
_OTHER_APP_SESSION = {"from": OTHER_APP_NAME, "sessionRef": OTHER_APP_SESSION}
_IMPOSTOR_SESSION = {"from": IMPOSTOR_NAME, "sessionRef": IMPOSTOR_SESSION}
_OPERATOR = {"from": "user:alice"}


def _refused(gated, sender: dict[str, str], path: Path) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as failure:
        _ipc_send(gated["app"], sender, path)
    assert failure.value.code == "ATTACHMENT_PATH_REFUSED"
    assert gated["gateway"].calls == []
    assert gated["gateway"].file_calls == []
    return failure.value


def test_session_attaches_from_its_cwd_and_workspace(gated) -> None:
    (gated["repo"] / "diff.patch").write_text("cwd bytes")
    (gated["workspace"] / "report.txt").write_text("workspace bytes")

    _ipc_send(gated["app"], _SESSION, gated["repo"] / "diff.patch")
    _ipc_send(gated["app"], _SESSION, gated["workspace"] / "report.txt")

    sent = [(name, data) for _chat, name, data, _type, _key in gated["gateway"].file_calls]
    assert sent == [("diff.patch", b"cwd bytes"), ("report.txt", b"workspace bytes")]


def test_session_cannot_attach_outside_its_roots(gated) -> None:
    (gated["outside"] / "private.txt").write_text("not yours")

    error = _refused(gated, _SESSION, gated["outside"] / "private.txt")
    assert error.data["reason"] == "outside-allowed-roots"


def test_session_symlink_to_hyprial_secrets_is_refused_without_the_path(gated) -> None:
    link = gated["repo"] / "creds.txt"
    link.symlink_to(gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json")

    error = _refused(gated, _SESSION, link)
    assert error.data["reason"] == "hyprial-home"
    assert "path" not in error.data
    assert "secrets" not in str(error)


def test_unauthenticated_claim_may_attach_nothing(gated) -> None:
    # Observe mode lets an unauthenticated agent claim send text; it must not
    # let that claim make the daemon read files.
    (gated["repo"] / "diff.patch").write_text("cwd bytes")

    error = _refused(gated, {"from": ACTOR_NAME}, gated["repo"] / "diff.patch")
    assert error.data["reason"] == "caller-not-authenticated"


def test_operator_attaches_anywhere_but_the_hyprial_home(gated) -> None:
    (gated["outside"] / "notes.txt").write_text("operator bytes")

    _ipc_send(gated["app"], _OPERATOR, gated["outside"] / "notes.txt")
    assert gated["gateway"].file_calls[0][1:3] == ("notes.txt", b"operator bytes")

    gated["gateway"].calls.clear()
    gated["gateway"].file_calls.clear()
    error = _refused(
        gated, _OPERATOR, gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json"
    )
    assert error.data["reason"] == "hyprial-home"


def _assert_home_refusal_without_path(
    gated, sender: dict[str, str], path: Path
) -> None:
    error = _refused(gated, sender, path)
    assert error.data["reason"] == "hyprial-home"
    assert "path" not in error.data
    assert str(path) not in str(error)


def test_app_actor_attaches_from_its_reports_directory(gated) -> None:
    report = gated["app_reports"] / "daily.html"
    report.write_text("<h1>daily</h1>")

    _ipc_send(gated["app"], _APP_SESSION, report)

    assert gated["gateway"].file_calls[0][1:3] == (
        "daily.html",
        b"<h1>daily</h1>",
    )


@pytest.mark.parametrize("sender", [_OTHER_APP_SESSION, _SESSION, _IMPOSTOR_SESSION])
def test_other_actor_cannot_attach_an_apps_report(gated, sender) -> None:
    report = gated["app_reports"] / "daily.html"
    report.write_text("private report")

    _assert_home_refusal_without_path(gated, sender, report)


@pytest.mark.parametrize("target_kind", ["secrets", "other-app"])
def test_app_reports_symlink_escape_is_refused_without_the_path(
    gated, target_kind: str
) -> None:
    if target_kind == "secrets":
        target = gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json"
    else:
        target = gated["other_app_reports"] / "private.html"
        target.write_text("other app")
    link = gated["app_reports"] / "daily.html"
    link.symlink_to(target)

    _assert_home_refusal_without_path(gated, _APP_SESSION, link)


@pytest.mark.parametrize("sibling", ["agent-home", "other"])
def test_app_actor_cannot_attach_an_app_sibling(gated, sibling: str) -> None:
    path = gated["home"] / "apps" / APP_NAME / sibling / "private.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a report")

    _assert_home_refusal_without_path(gated, _APP_SESSION, path)


def test_app_actor_cannot_attach_another_apps_report(gated) -> None:
    report = gated["other_app_reports"] / "private.html"
    report.write_text("other app")

    _assert_home_refusal_without_path(gated, _APP_SESSION, report)


def test_case_variant_reports_path_with_different_identity_is_refused(gated) -> None:
    variant = gated["home"] / "apps" / APP_NAME.upper() / "reports"
    variant.mkdir(parents=True, exist_ok=True)
    if os.path.samestat(os.stat(variant), os.stat(gated["app_reports"])):
        pytest.skip("case variant is the same directory on this filesystem")
    report = variant / "daily.html"
    report.write_text("different directory")

    _assert_home_refusal_without_path(gated, _APP_SESSION, report)


def test_managed_worker_cwd_comes_from_its_harness_spec(
    gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A PAC worker has no interactive session record; its root is the cwd
    # its launch spec registered.
    from dataclasses import replace

    from hyprial.kernel import HarnessLaunchSpec

    app = gated["app"]
    spec = HarnessLaunchSpec(
        harness="codex", name="wf-node", headless=True, cwd=str(gated["repo"])
    )
    with_worker = replace(app.desired_state.load(), harnesses=(spec,))
    monkeypatch.setattr(app.desired_state, "load", lambda: with_worker)
    worker = app._canonical_harness_uri(spec.name, spec)

    assert app._registered_session_cwd(worker) == str(gated["repo"])
    assert app._registered_session_cwd("agent:alice:sender-mac:nobody") is None


def test_app_reports_require_the_exact_home_layout_cwd(tmp_path: Path) -> None:
    """An actor named like the app gains no reports root from a cwd that only
    LOOKS like the layout (elsewhere on disk) or reaches it through a symlink
    from outside; only ``<home>/apps/<app>/agent-home`` itself proves it."""

    from hyprial.daemon.impl.path_authz import session_attachment_policy

    home = (tmp_path / "home").resolve()
    real_cwd = home / "apps" / "kanban" / "agent-home"
    real_cwd.mkdir(parents=True)
    missing = session_attachment_policy(
        home, actor_name="kanban", workspace=None, cwd=real_cwd
    )
    assert missing.own_app_reports is None  # no real reports dir, no root
    (home / "apps" / "kanban" / "reports").mkdir()
    lookalike = (tmp_path / "elsewhere" / "apps" / "kanban" / "agent-home").resolve()
    lookalike.mkdir(parents=True)

    exact = session_attachment_policy(
        home, actor_name="kanban", workspace=None, cwd=real_cwd
    )
    assert exact.own_app_reports == home / "apps" / "kanban" / "reports"

    elsewhere = session_attachment_policy(
        home, actor_name="kanban", workspace=None, cwd=lookalike
    )
    assert elsewhere.own_app_reports is None


@pytest.mark.parametrize("target_kind", ["secrets", "other-agent-workspace", "app-dir"])
def test_reports_root_replaced_by_a_symlink_is_not_an_attachment_root(
    gated, target_kind: str
) -> None:
    """The reports ROOT must be the real ``<home>/apps/<app>/reports``
    directory.  Replaced by a symlink (to ``secrets/``, another agent's
    workspace, or the app dir itself), it is dropped, as ``_own_workspace``
    drops a symlinked workspace; containment must not follow the link."""

    import shutil

    home = gated["home"]
    reports = gated["app_reports"]
    if target_kind == "secrets":
        target_dir = home / "secrets"
        target_file = target_dir / "lark-alicesmith-hyprial-e2e.json"
    elif target_kind == "other-agent-workspace":
        target_dir = home / "agents" / "victim" / "workspace"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "private.txt"
        target_file.write_text("victim data")
    else:
        target_dir = reports.parent
        target_file = target_dir / "agent-home" / "CLAUDE.md"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("app identity")
    assert target_file.exists()
    shutil.rmtree(reports)
    reports.symlink_to(target_dir, target_is_directory=True)

    path = reports / target_file.relative_to(target_dir)
    error = _refused(gated, _APP_SESSION, path)
    assert error.data["reason"] in {"hyprial-home", "other-agent-home"}
    assert "path" not in error.data
    assert str(path) not in str(error)


@pytest.mark.parametrize("root_kind", ["reports", "workspace"])
def test_root_swapped_for_a_symlink_after_the_policy_is_built_is_not_followed(
    tmp_path: Path, root_kind: str
) -> None:
    """Race: the root passes validation when the policy is built and is then
    replaced by a symlink to ``secrets/``.  Containment must judge the root
    itself (lstat), never follow it, so the secret stays refused."""

    import shutil

    from hyprial.daemon.impl.path_authz import (
        PathRefused,
        read_authorized_file,
        session_attachment_policy,
    )

    home = (tmp_path / "home").resolve()
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "lark.json").write_text("node-secret")
    cwd = home / "apps" / "kanban" / "agent-home"
    cwd.mkdir(parents=True)
    reports = home / "apps" / "kanban" / "reports"
    reports.mkdir()
    workspace = home / "agents" / "kanban" / "workspace"
    workspace.mkdir(parents=True)

    policy = session_attachment_policy(
        home, actor_name="kanban", workspace=workspace, cwd=cwd
    )
    root = reports if root_kind == "reports" else workspace
    assert root in (policy.own_app_reports, policy.own_workspace)

    shutil.rmtree(root)
    root.symlink_to(secrets, target_is_directory=True)

    with pytest.raises(PathRefused):
        read_authorized_file(root / "lark.json", policy, max_bytes=1 << 20)


def test_app_reports_hardlink_to_a_secret_is_refused_without_the_path(gated) -> None:
    """The st_nlink rule also covers the app reports root (#1204): a link in
    reports to ``secrets/`` is refused, and the refusal names no path."""

    secret = gated["home"] / "secrets" / "lark-alicesmith-hyprial-e2e.json"
    link = gated["app_reports"] / "daily.html"
    os.link(secret, link)

    error = _refused(gated, _APP_SESSION, link)
    assert error.data["reason"] == "hardlinked"
    assert "path" not in error.data
    assert str(link) not in str(error)
