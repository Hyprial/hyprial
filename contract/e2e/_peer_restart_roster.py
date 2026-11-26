"""E2E-024's physical peer ordering within the shared protocol-v7 fixture."""

from collections.abc import Callable
from datetime import UTC, datetime
import json

from hyprial.identity import directory_device_path


def local_member(factory: Callable[..., dict], owner: str, device_id: str,
                 address: str, *, local: bool = False) -> dict:
    is_a = device_id.startswith("orgfs-redial-a")
    member = factory(owner, device_id, address, local=local or is_a)
    if not is_a:
        member["masterPublic"] = "nodekey:" + "ff" * 32
    return member


def plant_device(daemon, space_id: str, member: dict) -> None:
    path = directory_device_path(member["owner"], member["deviceId"])
    try:
        daemon.command("org", "fs", "mkdir", space_id, path.rsplit("/", 1)[0], "--json")
    except AssertionError as error:
        if "node already exists" not in str(error):
            raise
    record = {
        "deviceId": member["deviceId"], "owner": member["owner"],
        "serverPublic": member["serverPublic"], "clientPublic": member["masterPublic"],
        "clientPublicByServer": {}, "address": member["address"], "keyGeneration": 1,
        "updatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    daemon.command("org", "fs", "write", space_id, path, json.dumps(record), "--json")
