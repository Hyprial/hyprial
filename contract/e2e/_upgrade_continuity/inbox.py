"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def write_channels(_entry: dict[str, Any], /, home):
    gateways = []
    for index, name in enumerate(_entry['ADAPTER_NAMES'], 1):
        credential_ref = f"lark-{name}"
        gateways.append({
            "provider": "lark",
            "name": name,
            "appId": f"isolated_upgrade_{index}",
            "credentialRef": credential_ref,
            "routes": [{"name": "default", "type": "direct",
                        "nativeId": f"agent:isolated:{name}"}],
            "defaultRoute": "default",
        })
        secret_path = home / "secrets" / f"{credential_ref}.json"
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(
            _entry['json'].dumps({"appSecret": f"{_entry['SECRET_MARKER']}-{index}"}), encoding="utf-8"
        )
        secret_path.chmod(0o600)
    channels = home / "channels.json"
    channels.parent.mkdir(parents=True, exist_ok=True)
    channels.write_text(
        _entry['json'].dumps({"version": 1, "gateways": gateways}, indent=2), encoding="utf-8"
    )


def adapters_online(_entry: dict[str, Any], /, status):
    return {
        item["name"]: item
        for item in status.get("adapters", [])  # type: ignore[union-attr]
        if isinstance(item, dict)
    }


def wait_for_adapters(_entry: dict[str, Any], /, executable, env, timeout):
    deadline = _entry['time'].monotonic() + timeout
    latest: dict[str, object] = {}
    while _entry['time'].monotonic() < deadline:
        latest = _entry['adapters_online'](_entry['ps'](env))
        if all(
            latest.get(name, {}).get("status") == "online"  # type: ignore[union-attr]
            for name in _entry['ADAPTER_NAMES']
        ) and len(latest) == len(_entry['ADAPTER_NAMES']):
            return latest
        _entry['time'].sleep(0.2)
    raise AssertionError(f"adapters did not all recover online: {latest}")


def pending_ids(_entry: dict[str, Any], /, status):
    return [item["messageId"] for item in status.get("pendingMessages", [])]  # type: ignore[union-attr]


def delivery_for(_entry: dict[str, Any], /, status, message_id):
    for item in status.get("pendingMessages", []):  # type: ignore[union-attr]
        if item.get("messageId") == message_id:
            return str(item.get("deliveryId"))
    raise AssertionError(f"no pending delivery for {message_id}: {status}")


def pending_delivery_for(_entry: dict[str, Any], /, pending, message_id):
    """Extract a deliveryId from a message.pending.list result (any era)."""

    for item in pending.get("messages", []):  # type: ignore[union-attr]
        if item.get("messageId") == message_id:
            return str(item.get("deliveryId"))
    raise AssertionError(f"no pending delivery for {message_id}: {pending}")


def init_daemon_identity(_entry: dict[str, Any], /, init):
    """init's daemon identity, across the readiness-split shape change.

    Builds since the restore gate spread the ping payload -- flat
    running/pid/epoch/nodeId/owner -- while older builds nested the ps
    daemon block under "daemon".  This scenario drives both.
    """

    daemon = init.get("daemon")
    if isinstance(daemon, dict) and daemon:
        return daemon
    return init


def local_channel_actor(_entry: dict[str, Any], /, daemon):
    """Mint the Channel actor from the old daemon's real local identity."""

    owner = daemon.get("owner")
    node_id = daemon.get("nodeId")
    if not isinstance(owner, str) or not owner or ":" in owner:
        raise AssertionError(f"old daemon omitted a usable owner: {daemon}")
    if not isinstance(node_id, str) or not node_id or ":" in node_id:
        raise AssertionError(f"old daemon omitted a usable nodeId: {daemon}")
    return f"agent:{owner}:{node_id}:channel-probe"


def assert_legacy_rows(
    _entry: dict[str, Any], /, state, driver_uri: str, expectations: dict[str, str],
    *, summaries: dict[str, str | None] | None = None,
):
    """C48: rows written by the old version stay readable on the new one.

    Legacy rows keep ``summary=null``; current old rows keep the exact supplied
    summary. Both retain exact body byte counts and fetchable bodies.
    """

    pending = _entry['ipc'](state, "message.pending.list", {"actor": driver_uri})
    rows = {
        item.get("messageId"): item
        for item in pending.get("messages", [])  # type: ignore[union-attr]
        if isinstance(item, dict)
    }
    read_ref = f"sref-{_entry['uuid4']()}"
    registered = _entry['ipc'](state, "session.register", {
        "actor": driver_uri,
        "sessionRef": read_ref,
        "command": [_entry['sys'].executable, "-c", "pass"],
        "cwd": str(_entry['Path'](state).parent),
        "source": "e2e-upgrade-continuity",
        "runtime": "pi",
    })
    if registered.get("registered") is not True:
        raise AssertionError(f"legacy-read session did not register: {registered}")
    for message_id, body in expectations.items():
        row = rows.get(message_id)
        if row is None:
            raise AssertionError(f"legacy row {message_id} missing after upgrade: {pending}")
        expected_summary = None if summaries is None else summaries[message_id]
        if row.get("summary") != expected_summary:
            raise AssertionError(
                f"old row {message_id} summary expected={expected_summary!r} "
                f"actual={row.get('summary')!r}"
            )
        if row.get("kind") != "message":
            raise AssertionError(f"legacy row {message_id} lost kind message: {row}")
        expected_bytes = len(body.encode("utf-8"))
        if row.get("bodyBytes") != expected_bytes:
            raise AssertionError(
                f"legacy row {message_id} bodyBytes expected={expected_bytes} "
                f"actual={row.get('bodyBytes')}"
            )
        fetched = _entry['ipc'](state, "message.read_body", {
            "actor": driver_uri,
            "sessionRef": read_ref,
            "messageId": message_id,
        })
        if fetched.get("message") != body:
            raise AssertionError(
                f"legacy row {message_id} body mismatch: {fetched}"
            )
    return rows
