"""Bounded fixture observations, without turning unknown host state into offline.

A retained host row is not an active delivery promise. Negative readiness
requires public agent-target absence and the test owner's stopped-container
fact as well. All observations share the original caller deadline.
"""

from hyprial.kernel import parse_agent_uri


def _rows(value, key):
    if (
        not isinstance(value, dict)
        or ("ok" in value and value["ok"] is not True)
        or not isinstance(value.get(key), list)
    ):
        raise AssertionError(f"{key} observation has an invalid response shape")
    return value[key]


def _host_online(value, peer):
    matches = []
    for row in _rows(value, "hosts"):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("nodeId"), str) or not row["nodeId"]
            or not isinstance(row.get("status"), str)
            or row.get("status") not in {"online", "unknown"}
        ):
            raise AssertionError("host observation has an invalid row")
        if row["nodeId"] == peer:
            matches.append(row)
    if len(matches) > 1:
        raise AssertionError("host observation has duplicate peer rows")
    return bool(matches and matches[0]["status"] == "online")


def _peer_target_present(value, peer):
    present = False
    for row in _rows(value, "targets"):
        if (
            not isinstance(row, dict) or row.get("targetKind") != "agent"
            or row.get("deliverable") is not True
            or not isinstance(row.get("status"), str) or not row["status"]
            or not isinstance(row.get("actor"), str)
            or not isinstance(row.get("targetUri"), str)
        ):
            raise AssertionError("agent target observation has an invalid row")
        actor = parse_agent_uri(row["actor"])
        address = parse_agent_uri(row["targetUri"])
        if actor is None or address is None or actor != address:
            raise AssertionError("agent target observation has inconsistent canonical addresses")
        present = present or actor[1].casefold() == peer.casefold()
    return present


def wait_for_target(ports, node, peer, *, absent=False, timeout=45.0):
    """Observe online presence, or prove the owned peer stopped and unadvertised."""

    if node not in ports.ALL_NODES or peer not in ports.ALL_NODES:
        raise AssertionError("presence observations require task-owned containers")
    deadline = ports.time.monotonic() + timeout

    def remaining():
        budget = deadline - ports.time.monotonic()
        if budget <= 0:
            raise AssertionError("presence observation exhausted its original deadline")
        return min(30.0, budget)

    def probe():
        host_online = _host_online(
            ports.exec_json(node, "host", "list", timeout=remaining()), peer,
        )
        result = {"hostOnline": host_online}
        if absent:
            result["targetPresent"] = _peer_target_present(
                ports.exec_json(node, "message", "targets", "--kind", "agent", timeout=remaining()), peer,
            )
            raw_state = ports.docker(
                ["inspect", "--format", "{{json .State}}", peer], timeout=remaining(),
            )
            try:
                state = ports.json.loads(raw_state)
            except ports.json.JSONDecodeError as error:
                raise AssertionError("owned container state observation is not JSON") from error
            if not isinstance(state, dict) or type(state.get("Running")) is not bool or not isinstance(state.get("Status"), str):
                raise AssertionError("owned container state observation has an invalid shape")
            result["containerStopped"] = state["Running"] is False and state["Status"] == "exited"
        return result

    def accept(value):
        if ports.time.monotonic() > deadline:
            return False
        if absent:
            return not value["hostOnline"] and not value["targetPresent"] and value["containerStopped"]
        return value["hostOnline"]

    ports.wait_for(
        probe, accept, timeout=timeout,
        what=f"{node} {'stopped and unadvertised' if absent else 'online'} peer {peer}",
    )
