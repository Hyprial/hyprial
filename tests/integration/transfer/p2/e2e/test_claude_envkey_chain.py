"""C3 follow-up: the claude env-key credential's export side.

The claude row's first proof used a placeholder ``.credentials.json``; the
agent's *env-key* mode (an explicitly authorized ``ANTHROPIC_AUTH_TOKEN`` /
``ANTHROPIC_BASE_URL`` instead of a native file) had no export-side producer.
This file drives the whole chain for that mode on two real daemons:

* the source daemon's own grant + incarnation gate resolves the agent's
  authorized env credentials at quiesce (nothing scans a process environment
  and the CLI never opens the secret file);
* the export seals exactly those into the bundle's credential envelope and
  holds no plaintext behind in the payload;
* the target opens the envelope with its own recipient key, installs the
  entries at 0600 and binds each environment name to the incarnation it just
  minted;
* the target's launch then passes the Claude auth gate *because of* the
  landed credential -- sealing to another key makes the same launch refuse
  with ``CLAUDE_CREDENTIAL_MISSING``, so the credential demonstrably comes
  from the opened envelope and not from anything else on the target.

The model protocol is still the repository's double
(``tests/fixtures/harnesses/fake_claude_sdk.py``); the real vendor binary,
SDK and credential remain NOT_RUN (TC012 BLOCKED).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.archive.envelope import (
    ENVELOPE_RELPATH,
    EnvCredential,
    ensure_recipient_key,
    envelope_json_bytes,
    generate_keypair,
    plan_credential_seal,
    recipient_key_path,
    seal_credential_envelope,
)

from tests.support.transfer.signed import sign_bundle_for_target

from .conftest import FAKE_CLAUDE, OWNER, SOURCE_NODE
from .test_two_daemon_chain import (
    PLAN_DIGEST,
    agent_home,
    entity_token,
    ipc_raw,
    p2_params,
    running,
    seed_worker,
    wait_running,
    work_dir,
    worker_spec,
)

WORKER = "at07-worker"
# Local runs start this fake harness in a few seconds; 30 s leaves room for a
# loaded CI host to finish restore without turning a real refusal into a wait.
WORKER_START_BUDGET_SECONDS = 30.0
ENV_ENTRY = "claude-env"
DUMMY_TOKEN = "sk-dummy-ant-env-marker-4f21"
DUMMY_BASE_URL = "https://gateway.invalid/anthropic"
CREATED_AT = "2026-10-16T00:00:00+00:00"


def _seed_grants(node) -> None:
    """One user-provider entry per env name, bound by an explicit grant.

    This is the operator's authorized secret face: the values live outside the
    agent home, and only the daemon's grant + incarnation resolver can read
    them -- exactly the launch's own source.
    """

    node.ipc(
        "agent.secret.provider-write",
        {"entryId": ENV_ENTRY, "fieldName": "token", "value": DUMMY_TOKEN},
    )
    node.ipc(
        "agent.secret.grant",
        {
            "actor": WORKER,
            "grantId": "g-token",
            "source": "user-provider",
            "entryId": ENV_ENTRY,
            "fieldName": "token",
            "environmentNames": ["ANTHROPIC_AUTH_TOKEN"],
            "revision": 1,
        },
    )
    node.ipc(
        "agent.secret.provider-write",
        {"entryId": "claude-base", "fieldName": "url", "value": DUMMY_BASE_URL},
    )
    node.ipc(
        "agent.secret.grant",
        {
            "actor": WORKER,
            "grantId": "g-base",
            "source": "user-provider",
            "entryId": "claude-base",
            "fieldName": "url",
            "environmentNames": ["ANTHROPIC_BASE_URL"],
            "revision": 1,
        },
    )


def _write_config(node) -> None:
    """The one honest config artifact; deliberately NO ``.credentials.json``."""

    config_item = agent_home(node) / "config" / "native" / "claude" / "settings.json"
    config_item.parent.mkdir(parents=True, exist_ok=True)
    config_item.write_text(json.dumps({"harness": "claude", "sessionRef": WORKER}))


def _env_credentials(quiesce: dict) -> tuple[EnvCredential, ...]:
    return tuple(
        EnvCredential.from_dict(item)
        for item in (quiesce["chain"].get("envCredentials") or [])
    )


def _sealed_bundle(
    source,
    target,
    tmp_path: Path,
    *,
    credentials: tuple[EnvCredential, ...],
    recipient_public_key: bytes,
) -> Path:
    """Export, seal and sign the quiesced home -- the production seal shape."""

    plan = plan_credential_seal(
        agent_home(source), env_credentials=credentials
    )
    envelope = seal_credential_envelope(
        plan,
        actor=WORKER,
        owner=OWNER,
        created_at=CREATED_AT,
        recipient_public_key=recipient_public_key,
    )
    bundle = tmp_path / "at07-envkey-bundle"
    export_bundle(
        agent_home(source),
        bundle,
        owner=OWNER,
        actor=WORKER,
        source_machine=SOURCE_NODE,
        created_at=CREATED_AT,
        session_refs=("test-at07-worker",),
        source_epoch=entity_token(source),
        credential_envelope=ENVELOPE_RELPATH,
        exclude_paths=plan.excluded,
        extra_files={ENVELOPE_RELPATH: (envelope_json_bytes(envelope), 0o600)},
    )
    sign_bundle_for_target(
        bundle,
        actor=WORKER,
        source_machine=SOURCE_NODE,
        source_home=source.home,
        target_home=target.home,
        credential_envelope=ENVELOPE_RELPATH,
    )
    return bundle


def _receive(target, source, operation: str, bundle: Path) -> dict:
    return target.ipc(
        "transfer.receive",
        {
            "operationId": operation,
            "planDigest": PLAN_DIGEST,
            "sourceNode": SOURCE_NODE,
            "sourceActor": WORKER,
            "sourceEntityToken": entity_token(source),
            "targetActor": WORKER,
            "bundleDir": str(bundle),
            "spec": worker_spec(target, "claude"),
        },
    )


def _launch(target) -> dict:
    """The target's own launch entry; a refusal is synchronous here."""

    return ipc_raw(
        target,
        "lifecycle.start",
        {
            "provider": "claude",
            "name": WORKER,
            "headless": True,
            "cwd": work_dir(target),
            "args": ["--approve"],
            "command": [sys.executable, str(FAKE_CLAUDE)],
        },
    )


def _open_chain(source, operation: str) -> dict:
    source.start_daemon()
    source.ipc("agent.create", {"name": WORKER})
    _seed_grants(source)
    seed_worker(source, harness="claude")
    _launch(source)
    assert wait_running(source, timeout=WORKER_START_BUDGET_SECONDS) is True
    quiesce = source.ipc("transfer.quiesce", p2_params(operation, "claude"))
    assert quiesce["stopped"] is True
    return quiesce


def test_the_env_key_credential_travels_sealed_and_the_target_launch_uses_it(
    source_node, target_node, tmp_path
) -> None:
    """Positive arm: the whole chain, and the credential is what starts it."""

    quiesce = _open_chain(source_node, "c3-envkey")
    credentials = {
        tuple(item["environment"]): item
        for item in quiesce["chain"]["envCredentials"]
    }
    assert credentials[("ANTHROPIC_AUTH_TOKEN",)]["value"] == DUMMY_TOKEN
    assert credentials[("ANTHROPIC_BASE_URL",)]["value"] == DUMMY_BASE_URL
    assert running(source_node) is False

    source_node.stop_daemon()
    _write_config(source_node)
    _private_key, public_key = ensure_recipient_key(
        recipient_key_path(target_node.home)
    )
    bundle = _sealed_bundle(
        source_node,
        target_node,
        tmp_path,
        credentials=_env_credentials(quiesce),
        recipient_public_key=public_key,
    )

    # Nothing in the bundle carries a plaintext value: the whole point.
    for path in bundle.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert DUMMY_TOKEN.encode() not in raw, path
            assert DUMMY_BASE_URL.encode() not in raw, path

    target_node.start_daemon()
    received = _receive(target_node, source_node, "c3-envkey", bundle)
    assert received["phases"] == ["landed", "active"]
    entry = agent_home(target_node) / "secrets" / "env-claude-claude-env"
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert json.loads(entry.read_text())["token"] == DUMMY_TOKEN
    base = agent_home(target_node) / "secrets" / "env-claude-claude-base"
    assert json.loads(base.read_text())["url"] == DUMMY_BASE_URL

    grants = target_node.ipc("agent.secret.list", {"actor": WORKER})["grants"]
    bound = {tuple(grant["environmentNames"]): grant["entryId"] for grant in grants}
    assert bound[("ANTHROPIC_AUTH_TOKEN",)] == "env-claude-claude-env"
    assert bound[("ANTHROPIC_BASE_URL",)] == "env-claude-claude-base"

    _launch(target_node)
    assert wait_running(target_node), "the landed env credential must pass the auth gate"
    # Anti-dual-active, read from the daemon's own monitor: restart the source
    # under the hold and confirm only the target runs the worker.
    source_node.start_daemon()
    assert running(source_node) is False
    assert running(target_node) is True


def test_revoking_the_landed_binding_makes_the_same_launch_refuse(
    source_node, target_node, tmp_path
) -> None:
    """Negative arm: the launch uses the landed binding, not something else."""

    quiesce = _open_chain(source_node, "c3-envkey-revoke")
    source_node.stop_daemon()
    _write_config(source_node)
    _private_key, public_key = ensure_recipient_key(
        recipient_key_path(target_node.home)
    )
    bundle = _sealed_bundle(
        source_node,
        target_node,
        tmp_path,
        credentials=_env_credentials(quiesce),
        recipient_public_key=public_key,
    )

    target_node.start_daemon()
    received = _receive(target_node, source_node, "c3-envkey-revoke", bundle)
    assert received["phases"] == ["landed", "active"]
    grants = target_node.ipc("agent.secret.list", {"actor": WORKER})["grants"]
    token = next(
        grant
        for grant in grants
        if grant["environmentNames"] == ["ANTHROPIC_AUTH_TOKEN"]
    )

    target_node.ipc(
        "agent.secret.revoke", {"actor": WORKER, "grantId": token["grantId"]}
    )

    document = _launch(target_node)
    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "CLAUDE_CREDENTIAL_MISSING", error
    assert running(target_node) is False


def test_sealing_to_another_key_leaves_no_credential_and_the_launch_refuses(
    source_node, target_node, tmp_path
) -> None:
    """Negative arm: the credential really comes from the opened envelope."""

    quiesce = _open_chain(source_node, "c3-envkey-miss")
    source_node.stop_daemon()
    _write_config(source_node)

    # The target holds a real key -- just not the one the envelope was sealed
    # to, so the landing degrades to reauth-required and installs nothing.
    ensure_recipient_key(recipient_key_path(target_node.home))
    _sender_private, other_public = generate_keypair()
    bundle = _sealed_bundle(
        source_node,
        target_node,
        tmp_path,
        credentials=_env_credentials(quiesce),
        recipient_public_key=other_public,
    )

    target_node.start_daemon()
    received = _receive(target_node, source_node, "c3-envkey-miss", bundle)
    assert received["phases"] == ["landed", "active"]
    assert not (agent_home(target_node) / "secrets" / "env-claude-claude-env").exists()

    document = _launch(target_node)
    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "CLAUDE_CREDENTIAL_MISSING", error
    assert "credential is missing" in error["message"].lower(), error
