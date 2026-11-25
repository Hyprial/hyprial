"""E2E-001's real, task-owned Tailcat control and private container topology."""

from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv4Interface, ip_network
from urllib.parse import parse_qsl, urlsplit


BACKEND = "unixsock-stream//data/home/net/zenoh.sock"
SLOT_PREFIX = "unixsock-stream//data/home/state/tailcat/slots/slot-"
BINARY = "/opt/hyprial/bin/hyprial-tailcat"
LABRELAY = "/opt/hyprial/bin/hyprial-tailcat-labrelay"


def _private_ipv4(value) -> IPv4Address:
    try:
        address = IPv4Address(value)
    except (ValueError, TypeError):
        raise AssertionError("relay host must be an owned private bridge IPv4") from None
    if not any(address in ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise AssertionError("relay host must be an owned RFC1918 private bridge IPv4")
    return address


def _relay_ipv4(ports) -> str:
    network_rows = ports.json.loads(ports.docker(["network", "inspect", ports.NETWORK], timeout=30.0))
    relay_rows = ports.json.loads(ports.docker(["container", "inspect", ports.RELAY], timeout=30.0))
    if not isinstance(network_rows, list) or len(network_rows) != 1 or not isinstance(network_rows[0], dict):
        raise AssertionError("owned relay network inspection is invalid")
    if not isinstance(relay_rows, list) or len(relay_rows) != 1 or not isinstance(relay_rows[0], dict):
        raise AssertionError("owned relay container inspection is invalid")
    network, relay = network_rows[0], relay_rows[0]
    if (
        network.get("Name") != ports.NETWORK or network.get("Driver") != "bridge"
        or network.get("Internal") is not True or not network.get("Id")
        or (network.get("Labels") or {}).get("hyprial-e2e001") != "1"
        or relay.get("Name") != "/" + ports.RELAY
        or relay.get("Config", {}).get("Labels", {}).get("hyprial-e2e001") != "1"
        or relay.get("State", {}).get("Running") is not True
    ):
        raise AssertionError("relay must belong to the running owned internal bridge")
    attached = relay.get("NetworkSettings", {}).get("Networks", {})
    members = network.get("Containers", {})
    matches = [(identifier, row) for identifier, row in members.items() if row.get("Name") == ports.RELAY]
    if set(attached) != {ports.NETWORK} or len(matches) != 1 or matches[0][0] != relay.get("Id"):
        raise AssertionError("owned relay must have exactly its one verified private network")
    try:
        interface = IPv4Interface(matches[0][1]["IPv4Address"])
        address = _private_ipv4(str(interface.ip))
        subnets = [ip_network(row["Subnet"]) for row in network["IPAM"]["Config"]]
    except (KeyError, ValueError, TypeError):
        raise AssertionError("owned relay IPv4 or network IPAM is invalid") from None
    connection = attached[ports.NETWORK]
    if (
        connection.get("NetworkID") != network["Id"]
        or connection.get("IPAddress") != str(address)
        or not any(interface.network == subnet and address in subnet for subnet in subnets)
    ):
        raise AssertionError("relay IPv4 must match its owned network ID and private IPAM subnet")
    return str(address)


def join_diagnostic(value, invite: str) -> dict:
    """Whitelist bounded public fields while removing the invite's secret forms."""

    from hyprial.identity import decode_invite

    parsed = urlsplit(invite)
    secrets = [invite, parsed.fragment, *(item for _, item in parse_qsl(parsed.query))]
    try:
        decoded = decode_invite(invite)
    except ValueError:
        pass
    else:
        secrets.extend((decoded.token, decoded.inviter_address))
        if isinstance(decoded.tailnet, dict):
            secrets.append(decoded.tailnet.get("token"))
            authority = decoded.tailnet.get("authority")
            if isinstance(authority, dict):
                secrets.append(authority.get("address"))
    secrets.extend(
        secret.partition("#")[2] for secret in tuple(secrets)
        if isinstance(secret, str) and "#" in secret
    )

    def text(raw, limit=512):
        if not isinstance(raw, str):
            return None
        for secret in secrets:
            if isinstance(secret, str) and secret:
                raw = raw.replace(secret, "[redacted-invite]")
        raw = re.sub(r'''(?:https?|hyprial)://[^\s<>'"\\]+''', "[redacted-link]", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(?:v[12]\.[A-Za-z0-9_.=-]+|Bearer\s+[A-Za-z0-9._~+/-]+=*)", "[redacted-token]", raw, flags=re.IGNORECASE)
        raw = re.sub(r'''(?i)\b((?:access[_-]?)?token|secret|password|credential|private[_-]?key|authorization)\b["']?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]]+)''', r"\1=[redacted]", raw)
        raw = re.sub(r"\b[A-Za-z0-9_-]{40,}\b", "[redacted-opaque]", raw)
        return raw[:limit]

    if not isinstance(value, dict):
        return {}
    result = {"code": text(value.get("code"), 80), "error": text(value.get("error"))}
    rows = value.get("steps")
    if isinstance(rows, list):
        result["steps"] = [
            {"name": text(row.get("name", row.get("kind")), 80),
             "code": text(row.get("code"), 80), "error": text(row.get("error")),
             "ok": row.get("ok") if isinstance(row.get("ok"), bool) else None}
            for row in rows[:8] if isinstance(row, dict)
        ]
    return result


def start_relay(ports) -> None:
    ports._DERP.clear()
    ports.docker([
        "run", "-d", "-i", "--name", ports.RELAY, "--network", ports.NETWORK,
        "--label", "hyprial-e2e001=1", "--entrypoint", LABRELAY, ports.IMAGE,
        "--bind", "0.0.0.0", "--advertise-host", ports.RELAY,
    ], timeout=120.0)

    def config():
        text = ports.docker(["logs", ports.RELAY], timeout=30.0)
        return ports.json.loads(text.splitlines()[0]).get("derp") if text else None

    value = ports.wait_for(
        config,
        lambda value: isinstance(value, dict) and value.get("host") == ports.RELAY
        and isinstance(value.get("derpPort"), int) and isinstance(value.get("stunPort"), int)
        and value.get("insecureForTests") is True,
        timeout=30.0, what="owned private labrelay ready",
    )
    ports._DERP.update(value | {"host": _relay_ipv4(ports)})
    print(f"RESULT ok private labrelay {ports.RELAY} ready")


def environment(ports) -> dict[str, str]:
    _private_ipv4(ports._DERP.get("host"))
    return {
        "HYPRIAL_ZENOH_LISTEN": BACKEND,
        "HYPRIAL_FORWARDING": "on",
        "HYPRIAL_FORWARDING_INBOUND_TARGET": "unix:/data/home/net/zenoh.sock",
        "HYPRIAL_TAILCAT_BINARY": BINARY,
        "HYPRIAL_TAILCAT_DERP": ports.json.dumps(ports._DERP, separators=(",", ":")),
        "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_GOSSIP": "0",
        "HYPRIAL_USAGE_DISABLE": "1",
    }


def assert_endpoints(ports, node: str, value: dict[str, object]) -> None:
    zenoh = ports.zenoh_of(value)
    if zenoh.get("listen") != [BACKEND]:
        raise AssertionError(f"{node} did not use its private Unix backend: {zenoh.get('listen')}")
    connect = zenoh.get("connect")
    forwarding = value.get("forwarding")
    endpoints = forwarding.get("endpoints") if isinstance(forwarding, dict) else None
    if (
        not isinstance(connect, list) or not connect
        or not all(isinstance(item, str) and item.startswith(SLOT_PREFIX) for item in connect)
        or not isinstance(endpoints, list) or not endpoints
        or not all(isinstance(item, str) and item.startswith(SLOT_PREFIX) for item in endpoints)
    ):
        raise AssertionError(f"{node} connections must use reported private sidecar slots")
    if any(endpoint not in connect for endpoint in endpoints):
        raise AssertionError(f"{node} is not dialing every reported sidecar slot")


def invite_owner(ports, node: str, space_id: str, owner: str) -> None:
    org = ports._ORG_NAMES[space_id]
    value = ports.exec_json(node, "org", "invite", org, "--user", owner)
    link = value.get("link")
    if not isinstance(link, str) or not link.startswith("https://"):
        raise AssertionError(f"{node} org invite returned no usable link")
    ports._INVITES[(space_id, owner)] = link


def join_org(ports, node: str, space_id: str, *, actor: str) -> None:
    org = ports._ORG_NAMES[space_id]
    owner = ports.owner_of_node(node)
    key = (space_id, owner)
    if key not in ports._INVITES:
        invite_owner(ports, ports.NODES[0], space_id, owner)
    link = ports._INVITES.pop(key)
    completed = ports.subprocess.run(
        ["docker", "exec", "-i", node, "hyprial", "org", "join", "-", "--json"],
        input=link, capture_output=True, text=True, timeout=60.0, check=False,
    )
    try:
        joined = ports.json.loads(completed.stdout)
    except (TypeError, ValueError) as error:
        raise AssertionError(f"{node} org join did not return JSON") from error
    steps = joined.get("steps") if isinstance(joined, dict) else None
    if (
        completed.returncode != 0 or not isinstance(joined, dict) or joined.get("ok") is not True
        or joined.get("org") != org or not isinstance(steps, list) or not steps
        or not all(isinstance(step, dict) and step.get("ok") is True for step in steps)
    ):
        diagnostic = join_diagnostic(joined, link)
        raise AssertionError(
            f"{node} managed org join failed code={diagnostic.get('code')}; "
            f"returnCode={completed.returncode} public={ports.json.dumps(diagnostic, sort_keys=True)}"
        )
    ports.exec_json(node, "org", "acl", "grant", org, actor)
