"""In-memory v6 membership peer for daemon tests (contract §2–§4).

This fake returns unwrapped data, not stdio frames. It does not model transport,
cryptography, disk durability or timers. Tests must explicitly seed rosters;
there is deliberately no OrgFS/directory reader or implicit admission fallback.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import secrets
import time
from typing import Any

from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError


def confirmation(preview):
    """Carry the exact preview binding; never enumerate targets at confirmation."""
    return {'operation_id': preview['operationId'], 'device_ids': list(preview['deviceIds'])}


def confirm_removal(remove, *args, **kwargs):
    preview = remove(*args, confirmed=False, **kwargs)
    return remove(*args, confirmed=True, **confirmation(preview), **kwargs)


def confirm_ipc_removal(remove, app, params):
    preview = remove(app, {**params, 'confirmed': False})
    return remove(app, {**params, 'confirmed': True, 'operationId': preview['operationId'],
                        'deviceIds': list(preview['deviceIds'])})


def key(label: str) -> str:
    return "nodekey:" + hashlib.sha256(label.encode()).hexdigest()


def member(owner: str, device_id: str, *, address: str | None = None) -> dict:
    server = key("server-" + device_id)
    return {
        "owner": owner, "deviceId": device_id,
        "masterPublic": key("client-" + device_id), "serverPublic": server,
        "clientPublicByServer": {},
        "address": address if address is not None else f"{server}@127.0.0.1:7447",
        "state": "active", "revision": 1,
    }


class FakeMembershipSidecar:
    """Inject ``membership_request`` into a service or use as its supervisor.

    ``for_device`` shares authority state, but retains independent local leases.
    ``seed`` is explicit test setup, never a daemon operation.
    """

    fields = {
        "membership-init": {"operationId", "tailnetId", "authority", "migration", "members"},
        "roster-fetch": {"tailnetId"},
        "member-announce": {"operationId", "tailnetId", "address", "clientPublicByServer"},
        "member-revoke": {"operationId", "tailnetId", "deviceIds", "expectedRevision", "confirmed", "reason"},
        "membership-status": set(),
        "invite-create": {"operationId", "tailnetId", "owner", "expiresAtMs"},
        "invite-redeem": {"operationId", "invite"},
        "authority-repin": {"operationId", "tailnetId", "authority", "expectedRevision", "confirmed"},
    }

    def __init__(self, *, owner="owner@example", device_id="node-a", local=None,
                 now_ms=1791028800000):
        self.local = deepcopy(local or member(owner, device_id))
        self.now_ms = now_ms
        self.tailnets: dict[str, dict] = {}
        self.receipts: dict[str, tuple[str, dict]] = {}
        self.leases: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self.backups: list[dict] = []
        self.failures: dict[str, str] = {}
        self.business_running = False
        #: (op, timeout) per call: pins which paths bound the sidecar wait.
        self.timeouts: list[tuple[str, float | None]] = []

    def for_device(self, owner: str, device_id: str) -> FakeMembershipSidecar:
        peer = FakeMembershipSidecar(owner=owner, device_id=device_id, now_ms=self.now_ms)
        peer.tailnets = self.tailnets
        peer.receipts = self.receipts
        return peer

    def seed(self, tailnet_id: str, *, authority=None, members=None) -> None:
        rows = deepcopy(members if members is not None else [self.local])
        authority = deepcopy(authority or {
            k: self.local[k] for k in ("owner", "deviceId", "masterPublic", "serverPublic", "address")
        } | {"controlPort": 7448})
        self.tailnets[tailnet_id] = {
            "tailnetId": tailnet_id, "authority": authority, "revision": 1,
            "members": {row["deviceId"]: row for row in rows}, "invites": {},
        }
        self.leases[tailnet_id] = 300

    @staticmethod
    def _error(code: str):
        raise ForwardingSidecarError("Fake membership operation refused.", code=code)

    def _authority(self, table):
        return table["authority"]["deviceId"] == self.local["deviceId"]

    def _table(self, tailnet_id):
        if tailnet_id not in self.tailnets:
            self._error("MEMBERSHIP_NOT_INITIALIZED")
        return self.tailnets[tailnet_id]

    def _active_local(self, table):
        row = table["members"].get(self.local["deviceId"])
        if row is None or any(row[field] != self.local[field] for field in (
            "owner", "masterPublic", "serverPublic"
        )):
            self._error("MEMBER_UNKNOWN")
        if row["state"] != "active":
            self._error("MEMBER_REVOKED")
        return row

    def _roster(self, table):
        return {"tailnetId": table["tailnetId"], "authority": table["authority"],
                "revision": table["revision"], "members": self._rows(table), "leaseSeconds": 300}

    @staticmethod
    def _rows(table):
        return [table["members"][device] for device in sorted(table["members"])]

    def membership_request(
        self, op: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((op, deepcopy(params)))
        self.timeouts.append((op, timeout))
        if op not in self.fields or not isinstance(params, dict):
            self._error("BAD_REQUEST")
        expected = self.fields[op]
        if (op == "membership-status" and set(params) - {"tailnetId"}) or (
            op != "membership-status" and set(params) != expected
        ):
            self._error("BAD_REQUEST")
        operation_id = params.get("operationId")
        normalized = json.dumps([op, params, self.local["owner"], self.local["deviceId"]], sort_keys=True)
        if "operationId" in expected:
            if not isinstance(operation_id, str) or not 0 < len(operation_id.encode()) <= 128:
                self._error("BAD_REQUEST")
            if operation_id in self.receipts:
                previous, result = self.receipts[operation_id]
                if previous != normalized:
                    self._error("OPERATION_CONFLICT")
                return deepcopy(result)
        if op in self.failures:
            self._error(self.failures[op])
        result = getattr(self, "_" + op.replace("-", "_"))(params)
        if operation_id:
            self.receipts[operation_id] = (normalized, deepcopy(result))
        return deepcopy(result)

    def _membership_status(self, params):
        if params.get('tailnetId') and params['tailnetId'] not in self.tailnets:
            self._error('MEMBERSHIP_NOT_INITIALIZED')
        statuses = []
        for tailnet_id, table in sorted(self.tailnets.items()):
            if params.get("tailnetId", tailnet_id) != tailnet_id:
                continue
            authority = self._authority(table)
            lease = None if authority else self.leases.get(tailnet_id, 0)
            local = table["members"].get(self.local["deviceId"])
            active = local is not None and local["state"] == "active"
            ready = active and (authority or lease > 0)
            code = None if ready else ("MEMBER_REVOKED" if local and not active
                                      else "MEMBERSHIP_LEASE_EXPIRED")
            statuses.append({
                "tailnetId": tailnet_id, "role": "authority" if authority else "member",
                "revision": table["revision"], "authorityDeviceId": table["authority"]["deviceId"],
                "memberReady": ready, "leaseRemainingSeconds": lease,
                "activeCount": sum(row["state"] == "active" for row in self._rows(table)),
                "tombstoneCount": sum(row["state"] == "tombstone" for row in self._rows(table)),
                "code": code, "members": self._rows(table),
            })
        return {"readiness": {"sidecar": True, "control": True,
                              "member": any(row["memberReady"] for row in statuses)},
                "tailnets": statuses}

    def _membership_init(self, params):
        tailnet_id = params["tailnetId"]
        if tailnet_id in self.tailnets:
            self._error("MEMBERSHIP_ALREADY_INITIALIZED")
        authority = params["authority"]["deviceId"] == self.local["deviceId"]
        rows = deepcopy(params["members"])
        if authority and not any(row["deviceId"] == self.local["deviceId"]
                                 and row["state"] == "active" for row in rows):
            self._error("MEMBER_UNKNOWN")
        if not authority and rows:
            self._error("BAD_REQUEST")
        for row in rows:
            row.update(revision=1, clientPublicByServer={})
        self.seed(tailnet_id, authority=params["authority"], members=rows)
        self.tailnets[tailnet_id]["revision"] = int(authority)
        self.leases[tailnet_id] = 0
        return {"tailnetId": tailnet_id, "revision": int(authority), "initialized": True}

    def _roster_fetch(self, params):
        table = self._table(params["tailnetId"])
        self._active_local(table)
        self.leases[params["tailnetId"]] = 300
        return self._roster(table)

    def _member_announce(self, params):
        table = self._table(params["tailnetId"])
        row = self._active_local(table)
        derived = {peer["serverPublic"]: key(row["masterPublic"] + peer["serverPublic"])
                   for peer in self._rows(table) if peer["state"] == "active"}
        if params["clientPublicByServer"] and params["clientPublicByServer"] != derived:
            self._error("MEMBER_CONFLICT")
        if row["address"] != params["address"] or row["clientPublicByServer"] != derived:
            table["revision"] += 1
            row.update(address=params["address"], clientPublicByServer=derived,
                       revision=table["revision"])
        return {"tailnetId": params["tailnetId"], "revision": table["revision"], "member": row}

    def _member_revoke(self, params):
        table = self._table(params["tailnetId"])
        targets = params["deviceIds"]
        if not targets:
            self._error('BAD_REQUEST')
        if params["confirmed"] is not True:
            self._error("CONFIRMATION_REQUIRED")
        if params["expectedRevision"] != table["revision"]:
            self._error("REVISION_CONFLICT")
        if any(device not in table["members"] for device in targets):
            self._error("MEMBER_UNKNOWN")
        if table["authority"]["deviceId"] in targets:
            self._error("LAST_AUTHORITY_REFUSED")
        if not self._authority(table) and targets != [self.local["deviceId"]]:
            self._error("AUTHORITY_REQUIRED")
        self.backups.append(deepcopy(table))
        table["revision"] += 1
        for device in targets:
            table["members"][device].update(state="tombstone", address="",
                                            clientPublicByServer={}, revision=table["revision"])
        return {"tailnetId": params["tailnetId"], "revision": table["revision"],
                "deviceIds": targets, "backupPath": f"/fake/backups/{params['operationId']}.json",
                "propagation": "lease-bounded"}

    def _invite_create(self, params):
        table = self._table(params["tailnetId"])
        if not self._authority(table):
            self._error("AUTHORITY_REQUIRED")
        now = self.now_ms if self.now_ms is not None else int(time.time() * 1000)
        if not now < params["expiresAtMs"] <= now + 3600000:
            self._error("INVITE_EXPIRED")
        invite = {"version": 2, "tailnetId": params["tailnetId"], "authority": table["authority"],
                  "inviteId": secrets.token_hex(16), "token": secrets.token_urlsafe(32),
                  "owner": params["owner"], "expiresAtMs": params["expiresAtMs"]}
        table["invites"][invite["inviteId"]] = {"invite": invite, "deviceId": ""}
        return {"invite": invite}

    def _invite_redeem(self, params):
        invite = params["invite"]
        if invite["version"] != 2:
            self._error("INVITE_VERSION_UNSUPPORTED")
        if invite["owner"] != self.local["owner"]:
            self._error("INVITE_INVALID")
        now = self.now_ms if self.now_ms is not None else int(time.time() * 1000)
        if invite["expiresAtMs"] <= now:
            self._error("INVITE_EXPIRED")
        table = self._table(invite["tailnetId"])
        stored = table["invites"].get(invite["inviteId"])
        if stored is None or stored["invite"] != invite:
            self._error("INVITE_INVALID")
        if stored["deviceId"] and stored["deviceId"] != self.local["deviceId"]:
            self._error("INVITE_CONSUMED")
        if not stored["deviceId"]:
            stored["deviceId"] = self.local["deviceId"]
            table["revision"] += 1
            row = deepcopy(self.local)
            server = table["authority"]["serverPublic"]
            row.update(revision=table["revision"], clientPublicByServer={
                server: key(row["masterPublic"] + server)})
            table["members"][row["deviceId"]] = row
            authority = table['members'][table['authority']['deviceId']]
            authority['clientPublicByServer'][row['serverPublic']] = key(
                authority['masterPublic'] + row['serverPublic'])
            authority['revision'] = table['revision']
        self.leases[invite["tailnetId"]] = 300
        if "result" not in stored:
            stored["result"] = deepcopy(self._roster(table) | {"deviceId": self.local["deviceId"]})
        return stored["result"]

    def _authority_repin(self, params):
        table = self._table(params["tailnetId"])
        if params["confirmed"] is not True:
            self._error("CONFIRMATION_REQUIRED")
        if self.business_running:
            self._error("NETWORK_NOT_QUIESCENT")
        if params["expectedRevision"] != table["revision"]:
            self._error("REVISION_CONFLICT")
        target = table["members"].get(params["authority"]["deviceId"])
        if target is None or target["state"] != "active":
            self._error("MEMBER_UNKNOWN")
        self.backups.append(deepcopy(table))
        table["authority"] = deepcopy(params["authority"])
        self.leases[params["tailnetId"]] = 0
        return {"tailnetId": params["tailnetId"], "revision": table["revision"],
                "backupPath": f"/fake/backups/{params['operationId']}.json", "restartRequired": True}
