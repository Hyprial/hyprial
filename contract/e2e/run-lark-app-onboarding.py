#!/usr/bin/env python3
"""E2E-011: Lark App onboarding up to the human authorization handoff.

Creating a real Lark App is a device-authorization flow: the platform mints a
verification URL and *a person* must open it in Feishu/Lark and approve.  That
final approval cannot be automated, and this runner does not fake it -- it
drives the onboarding branch of `hyprial adapter add`, over a real pseudo-terminal,
against a loopback stand-in for the accounts endpoint, and stops at exactly the
boundary a human would take over:

    the verification URL has been minted, transformed by the official SDK, and
    presented to the user, carrying the correct scopes and event declaration.

Everything up to that line is asserted here.  The scan itself, the resulting
production App, and tenant-admin approval are marked `[REAL ENV]` in
`11-lark-app-onboarding.md` and are not simulated.

No production credential, App, or network endpoint is touched: `domain` and
`lark_domain` are both pinned to the loopback stub, so the SDK's tenant-brand
switch cannot escape to the public endpoint.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import pty
import re
import subprocess
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diagnostic_text import clip, one_line
from _daemon_runner import (
    PRODUCTION_FEISHU_APPS,
    IsolatedDaemon,
    free_tcp_endpoint,
    hyprial_executable,
    short_temp_root,
    teardown_guard,
)

# The App secret the stub hands back. It stands in for a real client_secret and
# must never reach stdout, stderr, or any evidence byte.
STUB_APP_SECRET = "e2e011-client-secret-must-not-be-printed"
STUB_APP_ID = "cli_e2e011_created_app"
STUB_DEVICE_CODE = "e2e011-device-code"
# The raw URL the platform would return; the SDK rewrites it before display.
STUB_VERIFICATION_URI = "https://accounts.example.invalid/accounts/page/app_register?state=e2e011"

EXPECTED_ADDONS = {
    "scopes": {"tenant": ["im:message", "im:message.group_at_msg:readonly"]},
    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
}

# `adapter scopes authorize --interactive --capability group-history` must add exactly
# one scope. The capability's second scope group ("im:message" /
# "im:message:readonly") is already satisfied, so nothing else may appear --
# an opt-in must not smuggle extra reach along with the scope that was asked for.
EXPECTED_GROUP_HISTORY_ADDONS = {
    "scopes": {
        "tenant": [
            "im:message",
            "im:message.group_at_msg:readonly",
            "im:message.group_msg",
        ]
    },
    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
}

# Production Apps that must never be touched by any test (repository red line).
PROTECTED_APPS = PRODUCTION_FEISHU_APPS  # one list, shared with the isolation guard


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    check: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, status: str, check: str, detail: str) -> None:
        clean = one_line(detail)
        if STUB_APP_SECRET in clean:  # defence in depth for the reporter itself
            clean = clean.replace(STUB_APP_SECRET, "<redacted>")
        clean = clip(clean)
        self.results.append(Result(status, check, clean))
        print(f"RESULT {status} E2E-011 {check} {clean}", flush=True)

    def exit_code(self) -> int:
        counts = {
            status: sum(item.status == status for item in self.results)
            for status in ("PASS", "FAIL", "BLOCKED")
        }
        overall = (
            "FAIL"
            if counts["FAIL"]
            else ("BLOCKED" if counts["BLOCKED"] else "PASS")
        )
        print(
            "RESULT "
            f"{overall} E2E-011 lark-app-onboarding "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


@dataclass
class RegistrationStub:
    """Loopback stand-in for the official accounts registration endpoint.

    Implements exactly the three form actions the SDK's device-authorization
    flow performs (`init`, `begin`, `poll`).  It deliberately never returns a
    `tenant_brand`, so the SDK's Feishu->Lark domain switch cannot fire and
    redirect the flow at a public host.
    """

    actions: list[str] = field(default_factory=list)
    paths: set[str] = field(default_factory=set)
    _polls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def respond(self, path: str, form: dict[str, list[str]]) -> dict[str, Any]:
        action = (form.get("action") or [""])[0]
        with self._lock:
            self.actions.append(action)
            self.paths.add(path)
            polls = self._polls
            if action == "poll":
                self._polls += 1
        if action == "init":
            return {"supported_auth_methods": ["client_secret", "none"]}
        if action == "begin":
            return {
                "device_code": STUB_DEVICE_CODE,
                # interval 0 keeps the poll loop instant; expires_in must be
                # positive or the SDK never enters the loop at all.
                "interval": 0,
                "expires_in": 300,
                "verification_uri_complete": STUB_VERIFICATION_URI,
            }
        if action == "poll":
            # One pending round first, so the real poll loop is exercised.
            if polls == 0:
                return {"error": "authorization_pending", "error_description": "pending"}
            return {"client_id": STUB_APP_ID, "client_secret": STUB_APP_SECRET}
        return {"error": "invalid_request", "error_description": f"action={action!r}"}


class _Handler(BaseHTTPRequestHandler):
    stub: RegistrationStub

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        body = json.dumps(self.stub.respond(self.path, form)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return  # keep the runner's output the report, not an access log


def start_stub() -> tuple[ThreadingHTTPServer, RegistrationStub, str]:
    stub = RegistrationStub()
    handler = type("BoundHandler", (_Handler,), {"stub": stub})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, stub, f"http://{host}:{port}"


def onboarding_env(home: Path, state: Path, *, accounts: str | None) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({"HYPRIAL_HOME": str(home), "HARNESS_STATE_DIR": str(state)})
    for name in ("HYPRIAL_LARK_APP_ID", "HYPRIAL_LARK_APP_SECRET", "HYPRIAL_ZENOH_LISTEN"):
        environment.pop(name, None)
    # A shell HTTP proxy would intercept the SDK's requests to loopback and the
    # stub would never be reached; force a direct connection.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment.pop(name, None)
        environment.pop(name.lower(), None)
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    if accounts is not None:
        environment["HYPRIAL_LARK_ACCOUNTS_DOMAIN"] = accounts
    return environment


@dataclass(frozen=True, slots=True)
class Capture:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr


def run_add(
    executable: Path,
    environment: dict[str, str],
    *arguments: str,
    interactive: bool,
    timeout: int = 90,
) -> Capture:
    """Run onboarding through ``hyprial adapter add`` behind an optional PTY.

    The interactive branch is gated on `sys.stdin.isatty()`, so a pipe would
    silently take the non-interactive path and the device-authorization flow
    would never run.  A pty is what makes this an end-to-end test of the branch
    a human actually hits.
    """

    return run_cli(
        executable,
        environment,
        "adapter",
        "add",
        *arguments,
        interactive=interactive,
        timeout=timeout,
    )


def run_cli(
    executable: Path,
    environment: dict[str, str],
    *arguments: str,
    interactive: bool = False,
    timeout: int = 90,
) -> Capture:
    command = [str(executable), *arguments]
    if not interactive:
        completed = subprocess.run(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return Capture(completed.returncode, completed.stdout, completed.stderr)

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            env=environment,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave)
        slave = -1
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise
        return Capture(process.returncode, stdout, stderr)
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)


def presented_url(stderr: str) -> str:
    matches = re.findall(r"https?://\S+", stderr)
    if not matches:
        raise AssertionError(f"no verification URL was presented: {stderr!r}")
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one presented URL, got {matches}")
    return matches[0]


def decode_addons(encoded: str) -> dict[str, Any]:
    """Reverse the SDK's urlsafe-base64(gzip(json)) addons parameter."""

    padded = encoded + "=" * (-len(encoded) % 4)
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(padded)))


def query_of(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def main() -> int:
    report = Report()
    transcript: list[str] = []
    try:
        executable = hyprial_executable()
    except AssertionError as error:
        report.add("BLOCKED", "preflight", str(error))
        return report.exit_code()

    server, stub, accounts = start_stub()
    try:
        with tempfile.TemporaryDirectory(
            prefix="hyprial-e2e011-", dir=short_temp_root()
        ) as temporary:
            root = Path(temporary)
            home = root / "home"
            state = root / "state"
            home.mkdir(parents=True, exist_ok=True)
            state.mkdir(parents=True, exist_ok=True)

            # -- A1: environment credentials short-circuit the whole flow -----
            env_capture = run_add(
                executable,
                onboarding_env(
                    home,
                    state,
                    accounts=accounts,
                )
                | {
                    "HYPRIAL_LARK_APP_ID": "cli_env_app",
                    "HYPRIAL_LARK_APP_SECRET": STUB_APP_SECRET,
                },
                "env-gateway",
                "--json",
                interactive=False,
            )
            transcript.append(env_capture.combined)
            if env_capture.returncode != 0:
                raise AssertionError(f"env short-circuit failed: {env_capture.stderr}")
            body = json.loads(env_capture.stdout)
            if body.get("onboarding") != {
                "source": "environment",
                "createdApp": False,
            } or body["adapter"]["appId"] != "cli_env_app":
                raise AssertionError(f"unexpected env short-circuit result: {body}")
            if stub.actions:
                raise AssertionError(
                    f"env short-circuit contacted the accounts endpoint: {stub.actions}"
                )
            report.add(
                "PASS",
                "env-shortcircuit",
                "HYPRIAL_LARK_APP_ID/SECRET registered the adapter with no network call",
            )

            # -- A2: no human available -> structured refusal, nothing written -
            refusal = run_add(
                executable,
                onboarding_env(home, state, accounts=accounts),
                "refused-gateway",
                "--json",
                interactive=False,
            )
            transcript.append(refusal.combined)
            refused = json.loads(refusal.stdout)
            if refusal.returncode != 1 or refused.get("code") != "USER_ACTION_REQUIRED":
                raise AssertionError(f"expected USER_ACTION_REQUIRED, got {refused}")
            action = refused.get("data") or {}
            if action.get("command") != "hyprial adapter add refused-gateway" or action.get(
                "environment"
            ) != ["HYPRIAL_LARK_APP_ID", "HYPRIAL_LARK_APP_SECRET"]:
                raise AssertionError(f"refusal lacks an actionable recovery: {action}")
            if stub.actions:
                raise AssertionError("refusal contacted the accounts endpoint")
            channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
            if any(
                gateway["name"] == "refused-gateway"
                for gateway in channels.get("gateways", [])
            ):
                raise AssertionError("a refused onboarding wrote a gateway entry")
            report.add(
                "PASS",
                "non-interactive-refusal",
                "structured USER_ACTION_REQUIRED names the rerun command and both "
                "environment variables; no state written",
            )

            # -- A3/A4: the real device-authorization flow, up to the handoff --
            created = run_add(
                executable,
                onboarding_env(home, state, accounts=accounts),
                "created-gateway",
                interactive=True,
            )
            transcript.append(created.combined)
            if created.returncode != 0:
                raise AssertionError(
                    f"interactive onboarding failed: {created.stderr[-2000:]}"
                )
            if stub.actions[:2] != ["init", "begin"] or "poll" not in stub.actions:
                raise AssertionError(f"unexpected registration sequence: {stub.actions}")
            if stub.paths != {"/oauth/v1/app/registration"}:
                raise AssertionError(f"unexpected endpoint paths: {stub.paths}")
            report.add(
                "PASS",
                "device-authorization-flow",
                f"official SDK ran init/begin/poll against the loopback endpoint "
                f"({len(stub.actions)} requests, including a pending poll round)",
            )

            url = presented_url(created.stderr)
            query = query_of(url)
            if url == STUB_VERIFICATION_URI:
                raise AssertionError("the raw platform URI was shown untransformed")
            if (
                query.get("from") != ["sdk"]
                or query.get("tp") != ["sdk"]
                or query.get("source") != ["python-sdk/hyprial"]
            ):
                raise AssertionError(f"URL is not the SDK-transformed form: {query}")
            if query.get("state") != ["e2e011"]:
                raise AssertionError("the platform's own query was not preserved")
            report.add(
                "PASS",
                "verification-url-presented",
                "the SDK-transformed verification URL (from/tp/source) is written "
                "to stderr for the user, not buried in a log",
            )

            encoded = query.get("addons")
            if not encoded:
                raise AssertionError(f"no addons declaration in the URL: {query}")
            addons = decode_addons(encoded[0])
            if addons != EXPECTED_ADDONS:
                raise AssertionError(
                    f"declared addons are wrong: {addons} != {EXPECTED_ADDONS}"
                )
            report.add(
                "PASS",
                "scopes-and-events-declared",
                "URL-encoded addons decode to exactly im:message + "
                "im:message.group_at_msg:readonly and im.message.receive_v1",
            )

            if query.get("name") != ["Harness Bridge created-gateway"]:
                raise AssertionError(f"app preset name not carried: {query.get('name')}")
            if "createOnly" in query or "clientID" in query:
                raise AssertionError("a plain run must not request createOnly/clientID")
            report.add(
                "PASS",
                "app-preset-carried",
                "app preset pre-fills 'Harness Bridge created-gateway'; no "
                "createOnly or clientID on a plain run",
            )

            channels = json.loads((home / "channels.json").read_text(encoding="utf-8"))
            created_entry = next(
                gateway
                for gateway in channels["gateways"]
                if gateway["name"] == "created-gateway"
            )
            if created_entry["appId"] != STUB_APP_ID or created_entry["routes"] != []:
                raise AssertionError(f"created gateway not persisted: {created_entry}")
            secret_path = home / "secrets" / "lark-created-gateway.json"
            mode = secret_path.stat().st_mode & 0o777
            stored = json.loads(secret_path.read_text(encoding="utf-8"))
            if mode != 0o600 or stored.get("appSecret") != STUB_APP_SECRET:
                raise AssertionError(f"credential not stored at 0600: mode={mode:o}")
            report.add(
                "PASS",
                "credential-persisted",
                f"created appId persisted with an empty route set; secret stored "
                f"at mode {mode:o}",
            )

            # -- A5: --new creates; --app-id only registers existing credentials --
            new_capture = run_add(
                executable,
                onboarding_env(home, state, accounts=accounts)
                | {
                    "HYPRIAL_LARK_APP_ID": "cli_env_app",
                    "HYPRIAL_LARK_APP_SECRET": STUB_APP_SECRET,
                },
                "new-gateway",
                "--new",
                interactive=True,
            )
            transcript.append(new_capture.combined)
            if new_capture.returncode != 0:
                raise AssertionError(f"--new onboarding failed: {new_capture.stderr}")
            new_query = query_of(presented_url(new_capture.stderr))
            if new_query.get("createOnly") != ["true"]:
                raise AssertionError(f"--new did not request createOnly: {new_query}")
            report.add(
                "PASS",
                "create-only-overrides-environment",
                "--new ignores present HYPRIAL_LARK_APP_ID/SECRET and sends "
                "createOnly=true",
            )

            requests_before = len(stub.actions)
            existing_secret = root / "existing-app-secret"
            existing_secret.write_text(STUB_APP_SECRET, encoding="utf-8")
            existing_secret.chmod(0o600)
            try:
                adopt = run_add(
                    executable,
                    onboarding_env(home, state, accounts=accounts),
                    "adopted-gateway",
                    "--app-id",
                    "cli_existing_app",
                    "--route",
                    "dm=oc_existing_dm",
                    "--secret-file",
                    str(existing_secret),
                    "--json",
                    interactive=False,
                )
            finally:
                existing_secret.unlink(missing_ok=True)
            transcript.append(adopt.combined)
            if adopt.returncode != 0:
                raise AssertionError(f"--app-id registration failed: {adopt.stderr}")
            adopted = json.loads(adopt.stdout)
            if adopted.get("adapter", {}).get("appId") != "cli_existing_app":
                raise AssertionError(f"--app-id was not registered: {adopted}")
            if len(stub.actions) != requests_before:
                raise AssertionError("--app-id registration contacted the accounts endpoint")
            report.add(
                "PASS",
                "existing-app-registration-only",
                "--app-id registers the supplied App credential and route "
                "without contacting the accounts endpoint",
            )

            # -- A6: a doomed run must not cost a real App --------------------
            # 'created-gateway' already exists. Re-running --new against it
            # must fail before the SDK asks a human to approve a new App: that
            # approval is irreversible, and a credential we then refuse to
            # store would leave an orphaned App on the tenant.
            requests_before = len(stub.actions)
            clash = run_add(
                executable,
                onboarding_env(home, state, accounts=accounts),
                "created-gateway",
                "--new",
                interactive=True,
            )
            transcript.append(clash.combined)
            if clash.returncode != 1:
                raise AssertionError(
                    f"re-onboarding an existing name should fail: {clash.stdout}"
                )
            if len(stub.actions) != requests_before:
                raise AssertionError(
                    "a doomed onboarding still ran the registration flow: "
                    f"{stub.actions[requests_before:]}"
                )
            if "ADAPTER_EXISTS" not in clash.combined and "already exists" not in (
                clash.combined
            ):
                raise AssertionError(f"unexpected clash failure: {clash.combined}")
            report.add(
                "PASS",
                "clash-costs-no-app",
                "re-onboarding an existing adapter fails with zero registration "
                "requests, so no human is asked to create an orphan App",
            )

            # -- A7: one-click re-authorization of an existing adapter --------
            # `adapter scopes authorize --interactive` must reach the App the adapter
            # already owns and never create one. It is not gated on a tty: it
            # creates nothing irreversible, and the link is normally handed to
            # somebody else, so `--json` has to keep working.
            plain_auth = run_cli(
                executable,
                onboarding_env(home, state, accounts=accounts),
                "adapter",
                "scopes",
                "authorize",
                "created-gateway",
                "--interactive",
                "--json",
            )
            transcript.append(plain_auth.combined)
            if plain_auth.returncode != 0:
                raise AssertionError(
                    f"interactive authorize failed: {plain_auth.stderr[-2000:]}"
                )
            body = json.loads(plain_auth.stdout)  # stdout stays one JSON value
            if body.get("status") != "authorized" or body.get("appId") != STUB_APP_ID:
                raise AssertionError(f"unexpected authorize result: {body}")
            auth_query = query_of(presented_url(plain_auth.stderr))
            if auth_query.get("clientID") != [STUB_APP_ID]:
                raise AssertionError(
                    f"authorize did not target the adapter's App: {auth_query}"
                )
            if "createOnly" in auth_query:
                raise AssertionError("authorize must never request App creation")
            # An App that exists must not be handed preset values: they pre-fill
            # the creation page, and a name reaching a live App would rename it.
            if "name" in auth_query or "desc" in auth_query:
                raise AssertionError(
                    f"authorize sent App-creation preset values: {auth_query}"
                )
            default_addons = decode_addons(auth_query["addons"][0])
            if default_addons != EXPECTED_ADDONS:
                raise AssertionError(
                    f"default authorize addons are wrong: {default_addons}"
                )
            report.add(
                "PASS",
                "interactive-authorize-targets-existing-app",
                "authorize --interactive sends clientID of the configured "
                "adapter with no createOnly and no App-creation preset; "
                "--json stdout stays a single JSON value with the link on stderr",
            )

            # The privacy assertion that decides the design: an optional,
            # privacy-widening scope is never smuggled into the default request.
            if "im:message.group_msg" in json.dumps(default_addons):
                raise AssertionError(
                    "group-history was requested without --capability"
                )
            report.add(
                "PASS",
                "optional-scope-is-opt-in",
                "a plain authorize --interactive declares only the required "
                "scopes; im:message.group_msg is absent",
            )

            group_auth = run_cli(
                executable,
                onboarding_env(home, state, accounts=accounts),
                "adapter",
                "scopes",
                "authorize",
                "created-gateway",
                "--interactive",
                "--capability",
                "group-history",
                "--json",
            )
            transcript.append(group_auth.combined)
            if group_auth.returncode != 0:
                raise AssertionError(
                    f"group-history authorize failed: {group_auth.stderr[-2000:]}"
                )
            group_query = query_of(presented_url(group_auth.stderr))
            group_addons = decode_addons(group_query["addons"][0])
            if group_addons != EXPECTED_GROUP_HISTORY_ADDONS:
                raise AssertionError(
                    f"group-history addons are wrong: {group_addons} != "
                    f"{EXPECTED_GROUP_HISTORY_ADDONS}"
                )
            if "im:message.group_msg" not in group_auth.stderr:
                raise AssertionError(
                    "the widened scope was not named in the human-facing prompt"
                )
            report.add(
                "PASS",
                "group-history-rides-the-authorization-link",
                "--capability group-history adds exactly im:message.group_msg "
                "to the link's addons and names it in the stderr prompt",
            )

            # --capability without --interactive is a silent no-op against the
            # scopes/apply endpoint, so it must be refused rather than ignored.
            misuse = run_cli(
                executable,
                onboarding_env(home, state, accounts=accounts),
                "adapter",
                "scopes",
                "authorize",
                "created-gateway",
                "--capability",
                "group-history",
                "--json",
            )
            transcript.append(misuse.combined)
            misused = json.loads(misuse.stdout)
            if misuse.returncode != 1 or misused.get("code") != "INVALID_ARGUMENT":
                raise AssertionError(f"--capability alone must fail: {misused}")
            report.add(
                "PASS",
                "capability-requires-interactive",
                "--capability without --interactive is refused instead of "
                "silently dropped by the scopes/apply endpoint",
            )

            # -- A8: the onboarded, route-less gateway is real to the daemon ---
            daemon = IsolatedDaemon(
                executable,
                root,
                "e2e011",
                listen=free_tcp_endpoint(),
                connect="",
            )
            daemon.allow_feishu_test_apps(Path(__file__).name)
            with teardown_guard(
                daemon.stop, record=lambda detail: report.add(
                    "FAIL", "teardown", f"daemon stop during teardown raised: {detail}"
                )
            ):
                daemon.start()
                listed = daemon.command("adapter", "list", "--json")
                names = {
                    adapter.get("name") for adapter in listed.get("adapters", [])
                }
                if not {"created-gateway", "env-gateway"} <= names:
                    raise AssertionError(
                        f"daemon did not load the onboarded gateways: {listed}"
                    )
                report.add(
                    "PASS",
                    "route-less-gateway-boots",
                    "the daemon loads the onboarded route-less gateways at boot "
                    f"({len(names)} adapters visible)",
                )

            # -- Hygiene: no secret anywhere, no production App anywhere -------
            everything = "\n".join(transcript)
            if STUB_APP_SECRET in everything:
                raise AssertionError("the App secret appeared in command output")
            leaked = [name for name in PROTECTED_APPS if name in everything]
            if leaked:
                raise AssertionError(f"production App names referenced: {leaked}")
            report.add(
                "PASS",
                "evidence-sanitized",
                "no client_secret byte in any stdout/stderr; no production App "
                "name referenced",
            )
        return report.exit_code()
    except Exception as error:  # noqa: BLE001 - executable contract boundary
        report.add("FAIL", "lark-app-onboarding", str(error))
        return report.exit_code()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
