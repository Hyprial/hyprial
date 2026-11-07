"""S1: org membership commands write the Casdoor group first (design §2).

Casdoor group membership is the authority and OrgFS the owner-kept cache,
so a Casdoor failure must leave OrgFS untouched and success must write both.
Only a fake Casdoor is used here; no credential file value is ever read.
"""

# ruff: noqa: F811 - the imported pytest fixtures are redefined as parameters

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
import urllib.request

import pytest
from tests.support.tailnet_membership import confirm_removal

from hyprial.daemon.impl.org.network.casdoor import (
    CasdoorGroupError,
    HttpCasdoorGroups,
    casdoor_groups_for_home,
    load_casdoor_credentials,
)
from hyprial.daemon.impl.org.network.casdoor.client import (
    CasdoorCredentials,
    _urllib_transport,
)
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.daemon.impl.orgfs.api import OrgFsError

from .network.test_org_lifecycle import (  # noqa: F401 - fixtures
    BOB,
    OWNER,
    _acl_member_users,
    _as_bob,
    _member_users,
    _service,
    _space_id,
    home,
    runtime,
)


class FakeCasdoor:
    def __init__(
        self,
        *,
        fail: set[str] | None = None,
        code: str = "CASDOOR_GROUP_REFUSED",
        member: bool = True,
        ensure_code: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail or set()
        self.code = code
        # ``member=True`` is the steady state: the owner is already in the group,
        # so nothing needs creating.  ``ensure_code`` makes ``ensure_group`` fail
        # with that code (e.g. CASDOOR_ORG_QUOTA_EXCEEDED).
        self.member = member
        self.ensure_code = ensure_code

    def _record(self, method: str, group: str, user: str) -> None:
        self.calls.append((method, group, user))
        if method in self.fail:
            raise CasdoorGroupError(self.code, f"{method} refused")

    def member_state(self, group: str, user: str) -> str:
        self._record("STATE", group, user)
        return "member" if self.member else "not-member"

    def ensure_group(self, group: str, user: str) -> dict[str, object]:
        self._record("ENSURE", group, user)
        if self.ensure_code is not None:
            raise CasdoorGroupError(self.ensure_code, f"ensure refused: {self.ensure_code}")
        return {"org": group.split("/", 1)[-1], "state": "created", "quota": 1}

    def require_member(self, group: str, user: str) -> None:
        self._record("GET", group, user)

    def add_member(self, group: str, user: str) -> bool:
        self._record("POST", group, user)
        return True

    def remove_member(self, group: str, user: str) -> bool:
        self._record("DELETE", group, user)
        return True


def _space_names(runtime) -> set[str]:  # type: ignore[no-untyped-def]
    return {space.name for space in runtime.facade.spaces()}


# -- create ---------------------------------------------------------------------


def test_create_verifies_the_group_holds_the_owner_before_any_orgfs_write(runtime, home) -> None:
    casdoor = FakeCasdoor()

    result = _service(runtime, home, casdoor=casdoor).create("group-acme")

    # The probe answers "member", so the create path never asks Casdoor to write.
    assert casdoor.calls == [
        ("STATE", "hyprial/group-acme", f"hyprial/{OWNER}"),
    ]
    assert result["casdoorGroup"] == "verified"
    assert {"group-acme", "group-acme-acl"} <= _space_names(runtime)


def test_create_fails_whole_and_names_the_setup_when_the_group_is_unusable(runtime, home) -> None:
    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=FakeCasdoor(fail={"STATE"})).create("group-acme")

    assert refused.value.code == "CASDOOR_GROUP_UNAVAILABLE"
    assert "infra-ops" in str(refused.value) and "permission" in str(refused.value)
    assert _space_names(runtime) == set()


def test_create_ensures_the_group_when_the_owner_is_not_in_it_yet(runtime, home) -> None:
    """The whole point of 2026-10-20: a missing group is created, not refused."""

    casdoor = FakeCasdoor(member=False)

    result = _service(runtime, home, casdoor=casdoor).create("group-acme")

    assert casdoor.calls == [
        ("STATE", "hyprial/group-acme", f"hyprial/{OWNER}"),   # probe: not a member
        ("ENSURE", "hyprial/group-acme", f"hyprial/{OWNER}"),  # create it
        ("GET", "hyprial/group-acme", f"hyprial/{OWNER}"),     # re-confirm
    ]
    assert result["casdoorGroup"] == "verified"
    assert {"group-acme", "group-acme-acl"} <= _space_names(runtime)


def test_create_ensures_the_group_when_the_probe_says_the_group_is_absent(runtime, home) -> None:
    """The first group an org ever gets: the probe cannot answer about members.

    Before this code existed the probe raised ``CASDOOR_GROUP_UNAVAILABLE`` and
    the create step below it was unreachable, so ``org create`` could never
    create its first Casdoor group -- every org was stuck behind an operator.
    """

    casdoor = FakeCasdoor(fail={"STATE"}, code="CASDOOR_GROUP_ABSENT")

    result = _service(runtime, home, casdoor=casdoor).create("group-acme")

    assert casdoor.calls == [
        ("STATE", "hyprial/group-acme", f"hyprial/{OWNER}"),   # probe: group absent
        ("ENSURE", "hyprial/group-acme", f"hyprial/{OWNER}"),  # create it
        ("GET", "hyprial/group-acme", f"hyprial/{OWNER}"),     # re-confirm
    ]
    assert result["casdoorGroup"] == "verified"
    assert {"group-acme", "group-acme-acl"} <= _space_names(runtime)


def test_create_never_ensures_when_the_owner_is_already_a_member(runtime, home) -> None:
    """A negative test: the probe must short-circuit, or every create writes."""

    casdoor = FakeCasdoor(member=True)

    _service(runtime, home, casdoor=casdoor).create("group-acme")

    assert [call[0] for call in casdoor.calls] == ["STATE"]
    assert "ENSURE" not in {call[0] for call in casdoor.calls}


def test_create_reports_quota_exhaustion_with_its_own_code(runtime, home) -> None:
    """Out of quota is not "the group is broken"; the codes must differ."""

    casdoor = FakeCasdoor(member=False, ensure_code="CASDOOR_ORG_QUOTA_EXCEEDED")

    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=casdoor).create("group-acme")

    assert refused.value.code == "CASDOOR_ORG_QUOTA_EXCEEDED"
    # The setup hint belongs to the *other* failure; quota needs its own words.
    assert "infra-ops" not in str(refused.value)
    assert _space_names(runtime) == set()


def test_create_keeps_group_unavailable_when_ensure_fails_for_another_reason(runtime, home) -> None:
    casdoor = FakeCasdoor(member=False, ensure_code="CASDOOR_ENSURE_GROUP_REFUSED")

    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=casdoor).create("group-acme")

    assert refused.value.code == "CASDOOR_GROUP_UNAVAILABLE"
    assert "infra-ops" in str(refused.value)
    assert _space_names(runtime) == set()


def test_create_does_not_ensure_when_the_probe_fails_for_a_transport_reason(runtime, home) -> None:
    """A slow or unreachable Casdoor must not be answered with a create request."""

    casdoor = FakeCasdoor(fail={"STATE"}, code="CASDOOR_UNREACHABLE")

    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=casdoor).create("group-acme")

    assert refused.value.code == "CASDOOR_GROUP_UNAVAILABLE"
    assert "ENSURE" not in {call[0] for call in casdoor.calls}
    assert _space_names(runtime) == set()


# -- invite ---------------------------------------------------------------------


def test_invite_adds_to_casdoor_then_writes_both_spaces(runtime, home) -> None:
    _service(runtime, home).create("group-acme")
    casdoor = FakeCasdoor()

    _service(runtime, home, casdoor=casdoor).invite("group-acme", f"user:{BOB}")

    assert casdoor.calls == [("POST", "hyprial/group-acme", f"hyprial/{BOB}")]
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    assert f"user:{BOB}" in _acl_member_users(runtime, "group-acme")


def test_a_refused_casdoor_add_writes_nothing_to_orgfs(runtime, home) -> None:
    _service(runtime, home).create("group-acme")

    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=FakeCasdoor(fail={"POST"})).invite(
            "group-acme", f"user:{BOB}"
        )

    assert refused.value.code == "CASDOOR_GROUP_WRITE_FAILED"
    assert f"user:{BOB}" not in _member_users(runtime, "group-acme")
    assert f"user:{BOB}" not in _acl_member_users(runtime, "group-acme")



def test_inviting_a_name_casdoor_does_not_know_fails_and_writes_nothing(runtime, home) -> None:
    """Production 2026-10-08: `--user amy.williams` became user:amy.williams,
    which never matches the login identity amy.williams@example.com."""

    _service(runtime, home).create("group-acme")

    class _Unknown(FakeCasdoor):
        def add_member(self, group: str, user: str) -> bool:
            self.calls.append(("POST", group, user))
            if "@" not in user:
                raise CasdoorGroupError("CASDOOR_USER_NOT_FOUND", f"Casdoor has no user {user}")
            return True

    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=_Unknown()).invite("group-acme", "amy.williams")

    assert refused.value.code == "CASDOOR_USER_NOT_FOUND"
    assert "login identity" in str(refused.value) and "name@example.com" in str(refused.value)
    assert not any("amy.williams" in user for user in _member_users(runtime, "group-acme"))
    assert not any("amy.williams" in user for user in _acl_member_users(runtime, "group-acme"))

# -- remove and an admitted leave --------------------------------------------------


def _with_bob(runtime, home) -> None:  # type: ignore[no-untyped-def]
    _service(runtime, home).create("group-acme")
    _service(runtime, home).invite("group-acme", f"user:{BOB}")


def test_remove_removes_from_casdoor_then_from_orgfs(runtime, home) -> None:
    _with_bob(runtime, home)
    casdoor = FakeCasdoor()

    result = confirm_removal(_service(runtime, home, casdoor=casdoor).remove, "group-acme", f"user:{BOB}")
    assert result["phase"] == "complete"

    assert casdoor.calls == [("DELETE", "hyprial/group-acme", f"hyprial/{BOB}")]
    assert f"user:{BOB}" not in _member_users(runtime, "group-acme")


def test_a_refused_casdoor_remove_leaves_orgfs_untouched(runtime, home) -> None:
    _with_bob(runtime, home)

    with pytest.raises(OrgNetworkError) as refused:
        confirm_removal(_service(runtime, home, casdoor=FakeCasdoor(fail={"DELETE"})).remove,
            "group-acme", f"user:{BOB}"
        )

    assert refused.value.code == "CASDOOR_GROUP_WRITE_FAILED"
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    assert f"user:{BOB}" in _acl_member_users(runtime, "group-acme")


def _bob_requests_leave(runtime) -> OrgFsDirectoryStore:  # type: ignore[no-untyped-def]
    store = OrgFsDirectoryStore(runtime.facade)
    with _as_bob(runtime):
        store.put_leave_request(
            "group-acme", BOB, {"org": "group-acme", "user": BOB, "requestedAt": "t1"}
        )
    return store


def test_an_admitted_leave_removes_from_casdoor_first(runtime, home) -> None:
    _with_bob(runtime, home)
    store = _bob_requests_leave(runtime)
    casdoor = FakeCasdoor()

    service = _service(runtime, home, casdoor=casdoor)
    result = service.process_leave_requests()
    assert result == {"processed": [], "skipped": ["group-acme"]}
    assert casdoor.calls == []
    assert BOB in store.list_leave_requests("group-acme")
    assert confirm_removal(service.remove, "group-acme", BOB)["phase"] == "complete"
    assert casdoor.calls == [("DELETE", "hyprial/group-acme", f"hyprial/{BOB}")]
    assert f"user:{BOB}" not in _member_users(runtime, "group-acme")
    assert store.list_leave_requests("group-acme") == {}


def test_a_refused_casdoor_remove_keeps_the_leave_pending_for_the_next_pass(runtime, home) -> None:
    _with_bob(runtime, home)
    store = _bob_requests_leave(runtime)
    logged: list[str] = []

    service = _service(
        runtime,
        home,
        casdoor=FakeCasdoor(fail={"DELETE"}),
        logger=lambda _level, _domain, event, **_fields: logged.append(event),
    )
    result = service.process_leave_requests()
    assert result == {"processed": [], "skipped": ["group-acme"]}
    with pytest.raises(OrgNetworkError) as refused:
        confirm_removal(service.remove, "group-acme", BOB)
    assert refused.value.code == "CASDOOR_GROUP_WRITE_FAILED"
    assert not any(op == "member-revoke" for op, _ in runtime.membership_sidecar.calls)
    assert service.process_leave_requests()["processed"] == []
    assert "org.leaves.casdoor-failed" in logged
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    assert BOB in store.list_leave_requests("group-acme")


def test_delete_never_touches_the_casdoor_group(runtime, home) -> None:
    _with_bob(runtime, home)
    casdoor = FakeCasdoor()

    with pytest.raises(OrgNetworkError) as refused:
        _service(runtime, home, casdoor=casdoor).delete("group-acme")
    assert refused.value.code == "LAST_AUTHORITY_REFUSED"
    assert f"user:{BOB}" in _member_users(runtime, "group-acme")
    assert casdoor.calls == []


# -- the HTTP client and its configuration --------------------------------------


def _credentials() -> CasdoorCredentials:
    return CasdoorCredentials(
        endpoint="http://casdoor.test:8000",
        client_id="cid",
        client_secret="csecret",
        username="hq-group-sync",
        password="pw",
    )


class _Transport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method, url, headers, body):  # type: ignore[no-untyped-def]
        self.requests.append((method, url, dict(headers), body))
        if url.endswith("/api/login/oauth/access_token"):
            return 200, {"access_token": "tok", "expires_in": 3600}
        if method == "GET":
            return 200, {"status": "ok", "data": {"groups": ["hyprial/group-internal"]}}
        return 200, {"status": "ok", "data": {"changed": True, "member": {}}}


def test_the_client_encodes_an_owner_with_at_and_keeps_secrets_out_of_urls() -> None:
    transport = _Transport()
    client = HttpCasdoorGroups(_credentials(), transport=transport, clock=lambda: 0.0)

    client.require_member("hyprial/group-internal", "hyprial/bob.smith@example.com")
    client.add_member("hyprial/group-internal", "hyprial/bob.smith@example.com")

    token_call, get_call, post_call = transport.requests
    assert token_call[0] == "POST" and b"client_secret=csecret" in (token_call[3] or b"")
    assert "csecret" not in token_call[1] and "pw" not in token_call[1].split("?")[-1]
    assert get_call[1] == (
        "http://casdoor.test:8000/api/hyprial/group-member"
        "?user=hyprial%2Fbob.smith%40example.com&group=hyprial%2Fgroup-internal"
    )
    assert get_call[2]["Authorization"] == "Bearer tok"
    assert post_call[0] == "POST"
    assert len([r for r in transport.requests if "access_token" in r[1]]) == 1  # cached


def _answering(msg: str):  # type: ignore[no-untyped-def]
    """Casdoor answers HTTP 200 with status=error for every refusal."""

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        if url.endswith("/api/login/oauth/access_token"):
            return 200, {"access_token": "tok", "expires_in": 3600}
        return 200, {"status": "error", "msg": msg}

    return HttpCasdoorGroups(_credentials(), transport=transport, clock=lambda: 0.0)


@pytest.mark.parametrize(
    ("msg", "state"),
    [
        # Verbatim from controllers/group_member.go (production image 8847a6e).
        ("the user is not a member of the group", "not-member"),
        ("the user: hyprial/bob@example is not found", "no-user"),
    ],
)
def test_the_client_recognises_exactly_two_membership_answers(msg: str, state: str) -> None:
    assert _answering(msg).member_state("hyprial/group-acme", "hyprial/bob@example") == state


def test_an_unauthorized_answer_means_the_group_is_absent_not_refused() -> None:
    """Measured 2026-10-20 against the configured service user.

    Group-scoped permissions are matched by group id, so a group that does not
    exist matches none of them and every member call on it answers this.  The
    same service user on an existing group (``hyprial/group-internal``) answers
    ``member``, so the answer is about the group, not about a bad credential.
    """

    with pytest.raises(CasdoorGroupError) as absent:
        _answering("Unauthorized operation").member_state("hyprial/group-acme", "hyprial/bob@example")

    assert absent.value.code == "CASDOOR_GROUP_ABSENT"


@pytest.mark.parametrize(
    "msg",
    [
        "Access token doesn't exist in database",
        "the group manager cannot manage its own membership",
        "the user: hyprial/someone-else is not found",
        "record not found",
    ],
)
def test_every_other_refusal_is_an_error_never_a_membership_answer(msg: str) -> None:
    """The catch-all still covers everything that is not a group being absent."""

    with pytest.raises(CasdoorGroupError) as refused:
        _answering(msg).member_state("hyprial/group-acme", "hyprial/bob@example")

    assert refused.value.code == "CASDOOR_GROUP_REFUSED"


def test_a_member_check_refuses_a_user_outside_the_group() -> None:
    with pytest.raises(CasdoorGroupError) as refused:
        _answering("the user is not a member of the group").require_member(
            "hyprial/group-acme", "hyprial/bob@example"
        )

    assert refused.value.code == "CASDOOR_NOT_A_MEMBER"


def test_the_credential_file_error_names_keys_never_values(tmp_path: Path) -> None:
    path = tmp_path / "casdoor.env"
    path.write_text(
        "CASDOOR_GROUP_SYNC_ENDPOINT=http://casdoor.test:8000\n"
        "CASDOOR_GROUP_SYNC_CLIENT_SECRET=s3cret-value\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(CasdoorGroupError) as incomplete:
        load_casdoor_credentials(path)

    assert incomplete.value.code == "CASDOOR_CREDENTIAL_INCOMPLETE"
    assert "CASDOOR_GROUP_SYNC_CLIENT_ID" in str(incomplete.value)
    assert "s3cret-value" not in str(incomplete.value)


def test_settings_select_the_client_and_a_broken_one_fails_every_call(tmp_path: Path) -> None:
    assert casdoor_groups_for_home(tmp_path) is None
    (tmp_path / "settings.json").write_text(json.dumps({"owner": "x"}), encoding="utf-8")
    assert casdoor_groups_for_home(tmp_path) is None

    (tmp_path / "settings.json").write_text(
        json.dumps({"orgSync": {"casdoorCredentialFile": str(tmp_path / "missing.env")}}),
        encoding="utf-8",
    )
    broken: Any = casdoor_groups_for_home(tmp_path)
    with pytest.raises(CasdoorGroupError) as unusable:
        broken.add_member("hyprial/group-acme", "hyprial/bob@example")
    assert unusable.value.code == "CASDOOR_CREDENTIAL_UNREADABLE"


def test_the_client_reports_an_unknown_user_distinctly() -> None:
    client = _answering("the user: hyprial/amy.williams is not found")

    with pytest.raises(CasdoorGroupError) as unknown:
        client.add_member("hyprial/group-internal", "hyprial/amy.williams")

    assert unknown.value.code == "CASDOOR_USER_NOT_FOUND"


# -- review 1197 ------------------------------------------------------------------


@pytest.mark.parametrize("code", ["CASDOOR_USER_NOT_FOUND", "CASDOOR_NOT_A_MEMBER"])
def test_removing_a_member_casdoor_already_lacks_succeeds(runtime, home, code: str) -> None:
    """A member invited by a name Casdoor never had can still be removed."""

    _with_bob(runtime, home)

    confirm_removal(_service(runtime, home, casdoor=FakeCasdoor(fail={"DELETE"}, code=code)).remove,
        "group-acme", f"user:{BOB}"
    )

    assert f"user:{BOB}" not in _member_users(runtime, "group-acme")
    assert f"user:{BOB}" not in _acl_member_users(runtime, "group-acme")


def test_a_failed_orgfs_invite_takes_the_user_back_out_of_the_group(runtime, home, monkeypatch) -> None:
    _service(runtime, home).create("group-acme")
    casdoor = FakeCasdoor()
    real_invite = runtime.facade.invite
    calls = []

    def failing_second_invite(space_id, user, mode):  # type: ignore[no-untyped-def]
        calls.append(space_id)
        if len(calls) == 2:
            raise OrgFsError("store-failed")
        return real_invite(space_id, user, mode)

    monkeypatch.setattr(runtime.facade, "invite", failing_second_invite)

    with pytest.raises(OrgFsError):
        _service(runtime, home, casdoor=casdoor).invite("group-acme", f"user:{BOB}")

    assert casdoor.calls == [
        ("POST", "hyprial/group-acme", f"hyprial/{BOB}"),
        ("DELETE", "hyprial/group-acme", f"hyprial/{BOB}"),
    ]


def _credential_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "casdoor.env"
    path.write_text(
        "CASDOOR_GROUP_SYNC_ENDPOINT=http://casdoor.test:8000\n"
        "CASDOOR_GROUP_SYNC_CLIENT_ID=cid\n"
        "CASDOOR_GROUP_SYNC_CLIENT_SECRET=s3cret-value\n"
        "CASDOOR_GROUP_SYNC_USERNAME=hq-group-sync\n"
        "CASDOOR_GROUP_SYNC_PASSWORD=pw\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def test_a_credential_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = _credential_file(tmp_path, 0o644)

    with pytest.raises(CasdoorGroupError) as refused:
        load_casdoor_credentials(path)

    assert refused.value.code == "CASDOOR_CREDENTIAL_PERMISSIONS"
    assert "s3cret-value" not in str(refused.value)


def test_one_client_per_credential_file_keeps_its_token_cache(tmp_path: Path) -> None:
    path = _credential_file(tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"orgSync": {"casdoorCredentialFile": str(path)}}), encoding="utf-8"
    )

    assert casdoor_groups_for_home(tmp_path) is casdoor_groups_for_home(tmp_path)


def test_the_daemon_ipc_path_wires_casdoor_into_the_service(runtime, home, tmp_path, monkeypatch) -> None:
    """Through ``_build_service``: a configured Casdoor gates ``org create``."""

    from hyprial.daemon.impl.org.network.casdoor import client as client_module
    from hyprial.daemon.impl.org.network.ipc import ipc_org_create
    from hyprial.kernel import DaemonRequestError

    from .network.test_org_lifecycle import _IpcApp

    path = _credential_file(tmp_path)
    (home / "settings.json").write_text(
        json.dumps({"orgSync": {"casdoorCredentialFile": str(path)}}), encoding="utf-8"
    )
    seen: list[tuple[str, str]] = []

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        seen.append((method, url.split("?")[0].rsplit("/", 2)[-1]))
        if url.endswith("/api/login/oauth/access_token"):
            return 200, {"access_token": "tok", "expires_in": 3600}
        return 200, {"status": "error", "msg": "the user is not a member of the group"}

    monkeypatch.setattr(client_module, "_urllib_transport", transport)
    app = _IpcApp(runtime, home, owner=OWNER)

    with pytest.raises(DaemonRequestError) as refused:
        ipc_org_create(app, {"org": "group-acme"})

    assert refused.value.code == "CASDOOR_GROUP_UNAVAILABLE"
    assert ("GET", "group-member") in seen
    assert "group-acme" not in _space_names(runtime)

# --- the outbound agent -------------------------------------------------------
#
# The Cloudflare edge in front of the issuer answers urllib's stock agent with
# 403 / error code 1010 (browser_signature_banned), so the daemon transport must
# name a hyprial one.  The edge below reproduces that rule; the assertions are
# behavioural on purpose -- reverting the fix must make them FAIL, not fail at
# collection because a symbol the fix introduced is gone.


class _SignatureBannedEdge:
    """A local stand-in for the edge: 403 for the stock agent, 200 otherwise."""

    def __init__(self) -> None:
        self.user_agents: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        edge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # keep test output quiet
                return

            def do_POST(self) -> None:
                agent = self.headers.get("User-Agent", "")
                edge.user_agents.append(agent)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                banned = agent.startswith("Python-urllib/")
                body = json.dumps(
                    {"error_code": 1010 if banned else 0, "status": "ok"}).encode("utf-8")
                self.send_response(403 if banned else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def __enter__(self) -> "_SignatureBannedEdge":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def test_the_transport_is_not_answered_as_a_stock_urllib_agent() -> None:
    """Red before the fix: the edge answers the stock agent with 403 / 1010."""

    with _SignatureBannedEdge() as edge:
        status, _parsed = _urllib_transport("POST", edge.url, {}, b"{}")

    assert edge.user_agents, "the edge recorded no request"
    assert not edge.user_agents[0].startswith("Python-urllib/")
    assert status == 200


def test_a_caller_supplied_agent_wins_in_any_case() -> None:
    """Header names are case-insensitive, so any spelling counts as supplied."""

    for name in ("User-Agent", "user-agent", "USER-AGENT"):
        with _SignatureBannedEdge() as edge:
            _urllib_transport("POST", edge.url, {name: "probe/1"}, b"{}")
        assert edge.user_agents == ["probe/1"], name


def test_the_proxy_detection_stays_off() -> None:
    """The endpoint is reached over the tailnet, so ProxyHandler({}) must remain."""

    with _SignatureBannedEdge() as edge:
        seen: list[object] = []
        original = urllib.request.build_opener

        def recording_build_opener(*handlers: object) -> Any:
            seen.extend(handlers)
            return original(*handlers)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(urllib.request, "build_opener", recording_build_opener)
            _urllib_transport("POST", edge.url, {}, b"{}")

    assert any(type(handler).__name__ == "ProxyHandler" for handler in seen)
