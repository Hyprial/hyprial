from __future__ import annotations

import json
import base64
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.org import commands as org_commands
from hyprial.daemon import NetworkProfile
from hyprial.daemon.impl.org.document import parse_document, serialize_document
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


ROOT = Path(__file__).resolve().parents[4]


def _environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _run(home: Path, *args: str, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "org", *args, "--json"],
        cwd=ROOT,
        env=_environment(home),
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_human(home: Path, *args: str, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "org", *args],
        cwd=ROOT,
        env=_environment(home),
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _document(path: Path, version: int = 1) -> None:
    unsigned = parse_document(
        f"""# Harness Bridge organization

The owner-adopted view.

```yaml
meta:
  version: {version}
  issued_at: 2026-08-16
  publisher: alicesmith
lines:
  - {{line: hyprial, owner: gregpaws}}
members: []
routing: []
norms:
  autonomy: [任务域内自治]
  escalation: 超域必须升级
  etiquette: 先引荐再委派
residents: []
```
"""
    )
    path.write_text(
        serialize_document(unsigned),
        encoding="utf-8",
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def test_org_show_absent_is_loud_but_successful(tmp_path: Path) -> None:
    result = _run(tmp_path / "home", "context", "show")

    assert result.returncode == 0, result.stderr
    assert _payload(result) == {
        "ok": True,
        "status": "absent",
        "message": "org-context absent",
    }


def test_org_import_show_and_status_public_cli_seams(tmp_path: Path) -> None:
    home = tmp_path / "home"
    incoming = tmp_path / "incoming.md"
    _document(incoming)

    imported = _run(home, "context", "import", str(incoming), "--force")
    assert imported.returncode == 0, imported.stderr
    import_body = _payload(imported)
    assert import_body["ok"] is True
    assert import_body["adopted"] is True
    assert import_body["meta"]["version"] == 1
    assert import_body["meta"]["publisher"] == "alicesmith"
    assert import_body["diff"].startswith("--- accepted/org-context.md")

    shown = _payload(_run(home, "context", "show"))
    assert shown["ok"] is True
    assert shown["status"] == "accepted"
    assert shown["meta"]["version"] == 1
    assert shown["summary"]["title"] == "Harness Bridge organization"

    status = _payload(_run(home, "context", "status"))
    assert status["ok"] is True
    assert status["slot"] == "accepted"
    assert status["accepted"]["version"] == 1
    assert status["pendingCount"] == 0


def test_org_import_without_force_requires_confirmation_in_json_mode(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming.md"
    _document(incoming)

    result = _run(tmp_path / "home", "context", "import", str(incoming))

    assert result.returncode == 1
    body = _payload(result)
    assert body["code"] == "CONFIRMATION_REQUIRED"
    assert body["data"]["meta"]["publisher"] == "alicesmith"
    assert "diff" in body["data"]


def test_org_import_human_flow_shows_provenance_and_diff_before_confirmation(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming.md"
    _document(incoming)

    result = _run_human(
        tmp_path / "home", "context", "import", str(incoming), stdin="y\n"
    )

    assert result.returncode == 0, result.stderr
    # The preview and the question come before the result, on stderr.
    assert "alicesmith" in result.stderr
    assert "accepted/org-context.md" in result.stderr
    assert "Adopt this org-context on this node?" in result.stderr
    assert result.stderr.index("accepted/org-context.md") < result.stderr.index("Adopt this")
    assert (tmp_path / "home" / "org-context.md").is_file()


def test_org_fetch_uses_daemon_and_preserves_selected_target(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    observed: dict[str, object] = {}

    def request(method, params=None, *, timeout=15.0, restore_wait=None):
        observed.update(method=method, params=params, timeout=timeout)
        return {
            "requestedSources": ["node-a"],
            "responseCount": 1,
            "receivedCount": 1,
            "rejectedCount": 0,
            "candidates": [{"version": 4, "source": "node-a"}],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "context", "fetch", "--from", "agent:node-a", "--timeout", "0.5", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["receivedCount"] == 1
    assert observed == {
        "method": "org.fetch",
        "params": {"timeoutSeconds": 0.5, "from": "agent:node-a"},
        "timeout": 1.5,
    }


def test_org_fetch_without_source_asks_daemon_to_enumerate_targets(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    observed: dict[str, object] = {}

    def request(method, params=None, *, timeout=15.0, restore_wait=None):
        observed.update(method=method, params=params, timeout=timeout)
        return {
            "requestedSources": ["node-a", "node-b"],
            "responseCount": 0,
            "receivedCount": 0,
            "rejectedCount": 0,
            "candidates": [],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "context", "fetch", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["requestedSources"] == ["node-a", "node-b"]
    assert observed == {
        "method": "org.fetch",
        "params": {"timeoutSeconds": 2.0},
        "timeout": 3.0,
    }


def test_org_import_requests_daemon_publication_after_local_adoption(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    incoming = tmp_path / "incoming.md"
    _document(incoming, version=2)
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    calls: list[str] = []

    def request(method, params=None, *, timeout=15.0, restore_wait=None):
        del params, timeout
        calls.append(method)
        return {"published": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "context", "import", str(incoming), "--force", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["published"] is True
    assert calls == ["org.publish"]
    assert (home / "org-context.md").is_file()


def test_org_show_full_prints_the_public_document_and_its_digest(tmp_path: Path) -> None:
    """Org context is public: --full exposes the adopted document, no file read."""

    import hashlib

    home = tmp_path / "home"
    incoming = tmp_path / "incoming.md"
    unsigned = parse_document(
        """# HyprialOS organization

```yaml
meta:
  version: 9
  issued_at: 2026-09-29
  publisher: bob.smith
lines:
  - {line: orgfs, owner: bob.smith}
members:
  - {username: bob.smith@example.com, display: Allen}
  - {username: rachel@example.com, display: Rachel}
routing: []
norms:
  autonomy: [任务域内自治]
  escalation: 超域必须升级
  etiquette: 先引荐再委派
residents: []
```
"""
    )
    incoming.write_text(serialize_document(unsigned), encoding="utf-8")
    assert _run(home, "context", "import", str(incoming), "--force").returncode == 0

    summary = _payload(_run(home, "context", "show"))
    assert "document" not in summary and "documentSha256" not in summary

    shown = _payload(_run(home, "context", "show", "--full"))
    assert shown["meta"]["version"] == 9
    members = shown["document"]["members"]
    assert [member["username"] for member in members] == [
        "bob.smith@example.com",
        "rachel@example.com",
    ]
    assert shown["document"]["norms"]["escalation"] == "超域必须升级"
    assert "meta" not in shown["document"]
    accepted = (home / "org-context.md").read_bytes()
    assert shown["documentSha256"] == hashlib.sha256(accepted).hexdigest()

    human = _run_human(home, "context", "show", "--full")
    assert human.returncode == 0, human.stderr
    assert "rachel@example.com" in human.stdout


# -- org network lifecycle (tailnet cutover §4.4; IPC org.*) ------------------


def _org_ipc_env(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    # exist_ok: some cases re-enter the same env (several names in one test), and
    # the second call must be a no-op rather than a FileExistsError.
    home.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    return home


def test_org_create_passes_the_org_name_through(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return {
            "org": "group-acme",
            "spaceId": "space-1",
            "existed": False,
            "casdoorGroup": "not-configured",
            "devicePublished": True,
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "create", "group-acme", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["org"] == "group-acme"
    assert payload["spaceId"] == "space-1"
    assert observed == {"method": "org.create", "params": {"org": "group-acme"}}


def test_org_repair_defaults_to_dry_run_and_apply_is_explicit(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    observed: list[tuple[str, object]] = []

    def request(method, params=None, **_kwargs):
        observed.append((method, params))
        return {"apply": bool(params["apply"]), "candidates": [], "repaired": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    preview = CliRunner().invoke(_cli_root_app, ["org", "repair", "--json"])
    applied = CliRunner().invoke(
        _cli_root_app, ["org", "repair", "acme", "--apply", "--json"]
    )

    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["apply"] is False
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["apply"] is True
    assert observed == [
        ("org.repair", {"apply": False}),
        ("org.repair", {"apply": True, "org": "group-acme"}),
    ]


def test_org_create_rejects_a_non_group_name_before_ipc(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid org names must not reach IPC")
        ),
    )

    # A bare name is now a valid organization name: `org-acme` becomes
    # `group-org-acme`, so rejection needs a name that is invalid on its own
    # terms (uppercase here).  The message must stay actionable without naming
    # the internal `group-` prefix.
    result = CliRunner().invoke(_cli_root_app, ["org", "create", "ACME", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "letters, digits and hyphens" in payload["error"]
    assert "group-" not in payload["error"]


def _invite_result() -> dict[str, object]:
    """The daemon's post-§5.1 invite answer: one payload, three forms."""
    https = "https://invite.hyprial.invalid/join#v1.secretpayload"
    return {
        "org": "group-acme",
        "spaceId": "space-1",
        "user": "bob",
        "link": https,
        "deepLink": "hyprial://join#v1.secretpayload",
        "command": f"hyprial org join '{https}'",
        "invite": "hyprial-invite:v1:secretpayload",
        "expiresAt": "2026-10-04T00:00:00+00:00",
    }


def test_org_invite_json_passes_the_three_link_forms_through(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return _invite_result()

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "invite", "group-acme", "--user", "bob", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    for field in ("link", "deepLink", "command", "invite", "expiresAt"):
        assert payload[field] == _invite_result()[field]
    assert observed == {
        "method": "org.invite",
        "params": {"org": "group-acme", "user": "bob"},
    }


def test_org_invite_human_prints_the_https_link_and_the_command(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)

    def request(method, params=None, **_kwargs):
        return _invite_result()

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "invite", "group-acme", "--user", "bob"])

    assert result.exit_code == 0, result.output
    https = _invite_result()["link"]
    assert https in result.stdout
    assert f"或在终端执行：hyprial org join '{https}'" in result.stdout
    # The https link appears exactly twice: its own line and inside the
    # command; nothing else leaks the payload.
    assert result.stdout.count(https) == 2
    assert "secretpayload" not in (result.stderr or "")


def test_org_join_passes_every_link_form_through_untouched(
    monkeypatch, tmp_path: Path
) -> None:
    """https / hyprial:// / 裸 token / 裸 v1.：shell 不解码，原样交给 daemon。"""
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []

    def request(method, params=None, **_kwargs):
        assert _kwargs["timeout"] == 120.0
        calls.append({"method": method, "params": params})
        return {"org": "group-acme", "spaceId": "space-1", "steps": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    forms = [
        "https://invite.hyprial.invalid/join#v1.payload",
        "hyprial://join#v1.payload",
        "hyprial-invite:v1:payload",
        "v1.payload",
    ]
    for form in forms:
        result = CliRunner().invoke(_cli_root_app, ["org", "join", form, "--json"])
        assert result.exit_code == 0, result.output
        # Desktop's deep-link patch depends on the org field.
        assert json.loads(result.stdout)["org"] == "group-acme"
    assert calls == [
        {"method": "org.join", "params": {"link": form}} for form in forms
    ]


def test_org_join_reads_the_link_from_stdin_to_avoid_shell_history(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return {"org": "group-acme", "spaceId": "space-1", "steps": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app,
        ["org", "join", "-", "--json"],
        input="hyprial-invite:v1:from-stdin\n",
    )

    assert result.exit_code == 0, result.output
    assert observed == {
        "method": "org.join",
        "params": {"link": "hyprial-invite:v1:from-stdin"},
    }


def test_org_join_retries_binding_proof_before_daemon_publishes(
    monkeypatch, tmp_path: Path
) -> None:
    home = _org_ipc_env(monkeypatch, tmp_path)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "owner": "alicesmith",
                "identityMode": "casdoor",
                "identityIssuer": "https://issuer.invalid",
                "userId": "user.feishu.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "username": "alicesmith",
                "identityClaims": {
                    "properties": {"oauth_Lark_unionId": "on_allen"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "resolve_profile",
        lambda: (
            NetworkProfile(
                org="acme",
                issuer="https://issuer.invalid",
                client_id="client-id",
            ),
            "profile.json",
        ),
    )
    proof_profiles: list[object] = []
    monkeypatch.setattr(
        org_commands,
        "obtain_binding_proof",
        lambda profile, *_args, **_kwargs: proof_profiles.append(profile) or (
            base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode())
            .rstrip(b"=")
            .decode()
            + "."
            + base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "aud": "1be2763a29154030fbcb",
                        "name": "alicesmith",
                        "owner": "hyprial",
                        "oauth_Lark_unionId": "on_allen",
                    }
                ).encode()
            )
            .rstrip(b"=")
            .decode()
            + ".signature"
        ),
    )

    def request(method, params=None, **_kwargs):
        settings = json.loads(
            (home / "settings.json").read_text(encoding="utf-8")
        )
        assert settings["identityBinding"]["proof"].endswith(".signature")
        return {"org": "group-acme", "spaceId": "space-1", "steps": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "join", "v1.payload", "--json"])

    assert result.exit_code == 0, result.output
    # The proof is requested from the network's own issuer (the resolved
    # profile), not a hard-coded one.
    assert [profile.issuer for profile in proof_profiles] == ["https://issuer.invalid"]


def test_org_leave_and_remove_pass_their_arguments_through(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []

    def request(method, params=None, **_kwargs):
        calls.append({"method": method, "params": params})
        return {"org": "group-acme", "operationId": "preview-id", "deviceIds": ["b1"],
                "phase": "complete" if params['confirmed'] else 'pending-confirmation'}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    left = CliRunner().invoke(
        _cli_root_app, ["org", "leave", "group-acme", "--yes", "--json"]
    )
    assert left.exit_code == 0, left.output
    removed = CliRunner().invoke(
        _cli_root_app, ["org", "remove", "group-acme", "bob", "--yes", "--json"]
    )
    assert removed.exit_code == 0, removed.output
    assert calls == [
        {"method": "org.leave", "params": {"org": "group-acme", "confirmed": False}},
        {"method": "org.leave", "params": {"org": "group-acme", "confirmed": True,
                                           "operationId": "preview-id", "deviceIds": ["b1"]}},
        {"method": "org.remove", "params": {"org": "group-acme", "user": "bob", "confirmed": False}},
        {"method": "org.remove", "params": {"org": "group-acme", "user": "bob", "confirmed": True,
                                            "operationId": "preview-id", "deviceIds": ["b1"]}},
    ]


def test_org_delete_requires_yes_off_a_tty(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)

    def request(method, params=None, **_kwargs):  # pragma: no cover - refused first
        raise AssertionError("org.delete must not be called without confirmation")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "delete", "group-acme", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "USER_ACTION_REQUIRED"


def test_org_delete_with_yes_calls_the_daemon(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return {"org": "group-acme", "state": "deleted", "removedMembers": ["bob"]}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "delete", "group-acme", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "deleted"
    assert observed == {"method": "org.delete", "params": {"org": "group-acme"}}


def test_org_delete_on_a_tty_asks_and_honors_the_answer(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[str] = []

    def request(method, params=None, **_kwargs):
        calls.append(method)
        return {"org": "group-acme", "state": "deleted"}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: True)

    declined = CliRunner().invoke(_cli_root_app, ["org", "delete", "group-acme"], input="n\n")
    assert declined.exit_code == 1
    assert calls == []

    confirmed = CliRunner().invoke(_cli_root_app, ["org", "delete", "group-acme"], input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert calls == ["org.delete"]


def test_org_list_asks_the_daemon_for_this_nodes_orgs(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return {"orgs": [{"org": "group-acme", "role": "owner", "devices": 2}]}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["orgs"][0]["org"] == "group-acme"
    assert observed == {"method": "org.list", "params": None}


def test_org_pending_accept_all_runs_the_pickup_and_joins(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    from hyprial.shell.impl.invites import pending as pending_module

    seen: dict[str, object] = {}

    def fake_run_pending(home, *, accept_all, confirm, ipc_join, now, **kwargs):
        seen.update(
            home=home, accept_all=accept_all, confirm=confirm, now=now
        )
        ipc_join("v1.payload-1")
        return {
            "ok": True,
            "joined": [{"org": "group-acme", "inviter": "bob"}],
            "failed": [],
            "skipped": 0,
            "pending": [],
            "warnings": [],
        }

    joins: list[str] = []

    def request(method, params=None, **_kwargs):
        assert method == "org.join"
        assert _kwargs["timeout"] == 120.0
        joins.append(params["link"])
        return {"org": "group-acme"}

    monkeypatch.setattr(pending_module, "run_pending", fake_run_pending)
    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(_cli_root_app, ["org", "pending", "--accept-all", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["joined"] == [{"org": "group-acme", "inviter": "bob"}]
    assert seen["accept_all"] is True
    assert seen["home"] == tmp_path / "home"
    assert joins == ["v1.payload-1"]


def test_org_pending_off_a_tty_without_accept_all_only_lists(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    from hyprial.shell.impl.invites import pending as pending_module

    seen: dict[str, object] = {}

    def fake_run_pending(home, *, accept_all, confirm, ipc_join, now, **kwargs):
        seen.update(accept_all=accept_all, confirm=confirm)
        return {
            "ok": True,
            "joined": [],
            "failed": [],
            "skipped": 0,
            "pending": [
                {"org": "group-acme", "inviter": "bob", "acceptedAt": "2026-10-03"}
            ],
            "warnings": [],
        }

    monkeypatch.setattr(pending_module, "run_pending", fake_run_pending)

    result = CliRunner().invoke(_cli_root_app, ["org", "pending", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["joined"] == []
    assert payload["pending"][0]["org"] == "group-acme"
    # 非 TTY 且无 --accept-all：不接受任何邀请（confirm=None）。
    assert seen["accept_all"] is False
    assert seen["confirm"] is None


def test_org_pending_pickup_errors_keep_their_code(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    from hyprial.shell.impl.invites import pending as pending_module

    def fake_run_pending(home, **kwargs):
        raise pending_module.InvitePickupError(
            "NOT_LOGGED_IN", "no login credential: run hyprial login first"
        )

    monkeypatch.setattr(pending_module, "run_pending", fake_run_pending)

    result = CliRunner().invoke(_cli_root_app, ["org", "pending", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "NOT_LOGGED_IN"


def test_org_network_defaults_to_all_orgs_and_honors_an_explicit_one(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []

    def request(method, params=None, **_kwargs):
        calls.append({"method": method, "params": params})
        return {"orgs": ["group-acme"], "directory": {}, "sources": {}}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    default = CliRunner().invoke(_cli_root_app, ["org", "network", "--json"])
    assert default.exit_code == 0, default.output
    explicit = CliRunner().invoke(_cli_root_app, ["org", "network", "group-acme", "--json"])
    assert explicit.exit_code == 0, explicit.output

    assert calls == [
        {"method": "org.network", "params": {}},
        {"method": "org.network", "params": {"org": "group-acme"}},
    ]


def test_org_subcommands_keep_the_daemon_error_code(
    monkeypatch, tmp_path: Path
) -> None:
    """错误码沿用 daemon 返回的 code: the IPC failure surface passes through."""
    _org_ipc_env(monkeypatch, tmp_path)

    def request(method, params=None, **_kwargs):
        raise _cli_get_services().CliError("unknown-space", "no space named group-ghost")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "network", "group-ghost", "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "unknown-space"


def test_org_acl_grant_sends_the_operator_claim_and_until(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return {
            "org": "group-acme",
            "agent": "agent:alice:node-a:worker",
            "grantedAtMs": 1,
            "untilMs": 2,
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "org", "acl", "grant", "group-acme", "worker",
            "--until", "2099-01-01T00:00:00+00:00", "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert observed["method"] == "org.acl.grant"
    params = observed["params"]
    assert params["org"] == "group-acme"
    assert params["agent"] == "worker"
    assert params["untilMs"] == 4070908800000
    # The terminal claims the local operator identity, never an agent name.
    assert str(params["from"]).startswith("user:")
    assert "sessionRef" not in params


def test_org_acl_grant_rejects_a_malformed_until(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)

    def request(method, params=None, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("the daemon must not be asked")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app,
        ["org", "acl", "grant", "group-acme", "worker", "--until", "tomorrow", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"


def test_org_acl_worker_claim_carries_its_session_fence(
    monkeypatch, tmp_path: Path
) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:alice:node-a:proxy")
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "session-proxy")
    observed: dict[str, object] = {}

    def request(method, params=None, **_kwargs):
        observed.update(method=method, params=params)
        return {"org": "group-acme", "agent": "agent:alice:node-a:worker", "revoked": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "acl", "revoke", "group-acme", "worker", "--json"]
    )

    assert result.exit_code == 0, result.output
    params = observed["params"]
    assert params["from"] == "agent:alice:node-a:proxy"
    assert params["sessionRef"] == "session-proxy"


def test_org_acl_ls_renders_the_grant_table(monkeypatch, tmp_path: Path) -> None:
    _org_ipc_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)

    def request(method, params=None, **_kwargs):
        assert method == "org.acl.ls"
        return {
            "org": "group-acme",
            "grants": [
                {
                    "agent": "agent:alice:node-a:worker",
                    "grantedAtMs": 1764576000000,
                    "untilMs": None,
                }
            ],
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    machine = CliRunner().invoke(_cli_root_app, ["org", "acl", "ls", "group-acme", "--json"])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert payload["ok"] is True
    assert payload["grants"][0]["agent"] == "agent:alice:node-a:worker"

    human = CliRunner().invoke(_cli_root_app, ["org", "acl", "ls", "group-acme"])
    assert human.exit_code == 0, human.output
    assert "agent:alice:node-a:worker" in human.stdout
    assert "AGENT" in human.stdout


def test_org_invite_keeps_the_membership_timeout_code(monkeypatch, tmp_path: Path) -> None:
    """A bounded sidecar wait reaches the operator as its own code (0.5.5.1 hotfix).

    Not "Tailnet membership command failed" with a generic code: `--json`
    carries MEMBERSHIP_SIDECAR_TIMEOUT so a wrapper can retry or escalate on
    it, and the exit status is the failure exit.
    """
    _org_ipc_env(monkeypatch, tmp_path)

    def request(method, params=None, **_kwargs):
        assert method == "org.invite"
        raise _cli_support.CliError(
            "MEMBERSHIP_SIDECAR_TIMEOUT",
            "Tailnet membership command invite-create timed out after 10s.",
            {"operation": "invite-create", "leg": "invite-create", "budgetSeconds": 10},
        )

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = CliRunner().invoke(
        _cli_root_app, ["org", "invite", "group-acme", "--user", "bob", "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "MEMBERSHIP_SIDECAR_TIMEOUT"
    assert "timed out" in payload["error"]
    assert payload["data"] == {"operation": "invite-create", "leg": "invite-create", "budgetSeconds": 10}
