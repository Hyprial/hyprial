"""The published login result of a daemon-run first login.

Decision: daemon-first-z5-cli-result-20261119.  The result keeps the keys the
in-process first-time route printed (``ok, identity, device, daemon, migration,
pending, verificationUri, userCode``, plus ``nextSteps``); every difference in a
block's own keys is pinned here so it is a decision and not an accident.  A
device that is not ready is a success (exit 0) with the daemon's retry rule
stated, and the CLI never starts or stops a daemon for it.
"""

from __future__ import annotations

import json

from .support import CODE, DEVICE_NOT_READY, OWNER, URI, answer

PUBLISHED_KEYS = {"ok", "identity", "device", "daemon", "migration", "pending", "verificationUri", "userCode"}


def test_the_result_has_the_published_keys_and_blocks(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")

    result = world.login("--no-open")

    payload = world.payload(result)
    assert result.exit_code == 0, result.stderr
    assert set(payload) == PUBLISHED_KEYS
    assert payload["ok"] is True
    assert payload["identity"] == {
        "status": "authenticated",
        "owner": OWNER,
        "mode": "casdoor",
        "issuer": "https://id.example.test",
        "source": "profile.json",
        "committed": True,
    }
    assert payload["device"] == {
        "ready": True,
        "deviceId": "scripted-node",
        "owner": OWNER,
        "keyGeneration": 1,
        "userKey": {"state": "unbound", "code": "USER_KEY_UNBOUND"},
    }
    assert payload["daemon"]["running"] is True and payload["daemon"]["phase"] == "serving"
    assert payload["daemon"]["pid"] == 4242 and payload["daemon"]["owner"] == OWNER
    assert payload["daemon"]["ok"] is True
    assert payload["migration"] == {"status": "not-required"}
    assert payload["pending"] == {"status": "not-attempted"}
    assert (payload["verificationUri"], payload["userCode"]) == (URI, CODE)
    assert len(world.pickups) == 1


def test_started_says_whether_this_command_launched_the_daemon(make_world):
    attached = make_world(cell="ownerless", phase="bootstrap")
    assert attached.payload(attached.login("--no-open"))["daemon"]["started"] is False
    assert attached.launches == []

    launched = make_world(cell="ownerless")
    daemon = launched.payload(launched.login("--no-open"))["daemon"]
    assert daemon["started"] is True and daemon["alreadyRunning"] is False


def test_started_is_false_when_the_launch_found_a_daemon_already_running(make_world):
    world = make_world(cell="ownerless")
    world.already_running = True

    daemon = world.payload(world.login("--no-open"))["daemon"]

    assert len(world.launches) == 1
    assert daemon["started"] is False and daemon["alreadyRunning"] is True


def test_a_daemon_that_does_not_answer_after_the_login_is_reported_unknown_not_failed(make_world):
    from hyprial.kernel import ipc_errors

    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["ping"] = [None, ipc_errors.DaemonUnavailableError("gone")]

    result = world.login("--no-open")

    payload = world.payload(result)
    assert result.exit_code == 0
    assert payload["daemon"]["state"] == "unknown"
    assert payload["identity"]["committed"] is True


def test_organization_next_steps_are_added_only_for_a_provably_empty_directory(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["org.list"] = [{"orgs": []}]

    payload = world.payload(world.login("--no-open"))

    assert payload["nextSteps"][0] == "hyprial org create <name>"


def test_a_device_that_is_not_ready_is_a_success_with_the_daemons_retry_rule(make_world):
    world = make_world(cell="ownerless", phase="bootstrap")
    world.answers["auth.operation.get"] = [answer(device=DEVICE_NOT_READY)]

    result = world.login("--no-open")

    payload = world.payload(result)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    device = payload["device"]
    assert device["ready"] is False and device["retry"] == "serving-start"
    assert device["error"]["code"] == "DEVICE_ENSURE_FAILED"
    assert "once at each daemon start" in device["nextStep"]
    assert "promise" not in device["nextStep"]
    assert payload["identity"]["committed"] is True
    assert world.methods == ["ping", "auth.login.begin", "auth.operation.get", "ping", "org.list"]
    assert world.launches == []


def test_the_device_url_and_code_reach_the_person_on_stderr_in_both_modes(make_world):
    human = make_world(cell="ownerless", phase="bootstrap")
    result = human.login("--no-open", json_output=False)
    assert result.exit_code == 0, result.stderr
    assert URI in result.stderr and f"code: {CODE}" in result.stderr
    assert "sign in with Feishu" in result.stderr

    machine = make_world(cell="ownerless", phase="bootstrap")
    result = machine.login("--no-open")
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    device = next(event for event in events if event["event"] == "device")
    assert device["verificationUri"] == URI and device["userCode"] == CODE
    assert "deviceCode" not in json.dumps(events)


def test_invites_are_picked_up_once_after_the_daemon_serves(make_world, monkeypatch):
    import hyprial.shell.impl.cli.commands.login.aftercare as cli_aftercare

    world = make_world(cell="ownerless", phase="bootstrap")
    seen = []

    def pickup(home, *, json_output, services=None):
        del services
        seen.append((list(world.methods), json_output))
        return {"status": "ok", "pending": [{"org": "group-acme"}]}

    monkeypatch.setattr(cli_aftercare, "_pickup_org_invites_after_login", pickup)

    payload = world.payload(world.login("--no-open"))

    assert seen == [(["ping", "auth.login.begin", "auth.operation.get", "ping"], True)]
    assert payload["pending"]["pending"][0]["org"] == "group-acme"
