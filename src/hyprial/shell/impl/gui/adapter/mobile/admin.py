"""Local operator CLI for the durable mobile-access authority.

Run as ``python -m hyprial.shell.impl.gui.adapter.mobile.admin`` with the
host's own database and node id.  ``--operator`` is a required audit label
supplied by the local operator; it is never inferred from the host login.
Authorization remains the authority database's file permissions: only the
host account that owns that file can run these commands.

``pair issue`` prints the one-time pairing code exactly once, on stdout, so
the operator can hand it to the device.  The code is never written to the
database (only a salted hash is), never logged here, and every other command
prints structured state without key material.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hyprial.shell.impl.gui.adapter.mobile.models import MobileAccessError
from hyprial.shell.impl.gui.adapter.mobile.store import MobileAccessStore


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _store(args: argparse.Namespace) -> MobileAccessStore:
    return MobileAccessStore(Path(args.db), node_id=args.node_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage one host's durable mobile-device access authority."
    )
    parser.add_argument("--db", required=True, help="mobile-access SQLite path")
    parser.add_argument("--node-id", required=True, help="this host's trusted node id")
    parser.add_argument(
        "--operator",
        required=True,
        help="audit label for this local operator (never inferred from the host login)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    pair = commands.add_parser("pair", help="pairing lifecycle")
    pair_commands = pair.add_subparsers(dest="pair_command", required=True)
    pair_commands.add_parser("issue", help="mint one short-lived pairing code")
    inspect = pair_commands.add_parser("inspect", help="show one pending request")
    inspect.add_argument("request_id")
    approve = pair_commands.add_parser("approve", help="approve a pending request")
    approve.add_argument("request_id")
    approve.add_argument("--fingerprint", required=True)
    approve.add_argument("--device-id", required=True)
    approve.add_argument("--client-public", required=True)
    reject = pair_commands.add_parser("reject", help="reject a pending request")
    reject.add_argument("request_id")

    device = commands.add_parser("device", help="approved devices")
    device_commands = device.add_subparsers(dest="device_command", required=True)
    device_commands.add_parser("list", help="list approved and revoked devices")
    show = device_commands.add_parser("show", help="show one device")
    show.add_argument("device_id")
    revoke = device_commands.add_parser("revoke", help="revoke one device")
    revoke.add_argument("device_id")
    revoke.add_argument("--reason", required=True)

    grant = commands.add_parser("grant", help="explicit per-session grants")
    grant_commands = grant.add_subparsers(dest="grant_command", required=True)
    add = grant_commands.add_parser("add", help="grant one session to one device")
    add.add_argument("device_id")
    add.add_argument("session_id")
    add.add_argument("--session-owner", required=True)
    add.add_argument("--shared-from", default=None)
    remove = grant_commands.add_parser("revoke", help="revoke one grant")
    remove.add_argument("device_id")
    remove.add_argument("session_id")
    list_grants = grant_commands.add_parser("list", help="list grants")
    list_grants.add_argument("--device", default=None)

    audit = commands.add_parser("audit", help="read the access audit trail")
    audit.add_argument("--device", default=None)
    audit.add_argument("--session", default=None)
    audit.add_argument("--limit", type=int, default=200)
    return parser


def _run(args: argparse.Namespace, store: MobileAccessStore) -> object:
    operator = args.operator
    if args.command == "pair":
        if args.pair_command == "issue":
            code, expires_at = store.issue_pairing_code(operator=operator)
            return {"code": code, "expiresAt": expires_at}
        if args.pair_command == "inspect":
            return store.inspect_pairing_request(args.request_id)
        if args.pair_command == "approve":
            return store.approve_pairing_request(
                args.request_id,
                operator=operator,
                node_id=store.node_id,
                device_id=args.device_id,
                client_public=args.client_public,
                fingerprint=args.fingerprint,
            )
        return store.reject_pairing_request(
            args.request_id, operator=operator, node_id=store.node_id
        )
    if args.command == "device":
        if args.device_command == "list":
            return store.list_devices()
        if args.device_command == "show":
            return store.device_status(args.device_id)
        return store.revoke_device(
            args.device_id, operator=operator, reason=args.reason
        )
    if args.command == "grant":
        if args.grant_command == "add":
            return store.grant_session(
                args.device_id,
                args.session_id,
                session_owner=args.session_owner,
                operator=operator,
                source_device_id=args.shared_from,
            )
        if args.grant_command == "revoke":
            return store.revoke_session_grant(
                args.device_id,
                args.session_id,
                operator=operator,
            )
        return store.list_grants(args.device)
    return store.audit_events(
        device_id=args.device, session_id=args.session, limit=args.limit
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with _store(args) as store:
            _emit(_run(args, store))
    except MobileAccessError as error:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "reason": error.reason,
                    "details": error.details,
                },
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
