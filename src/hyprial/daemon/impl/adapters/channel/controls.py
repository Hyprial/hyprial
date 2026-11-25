"""Signed common control callbacks with per-principal one-shot authority."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class ControlDenied(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    control_id: str
    response_id: str
    principal: str
    choice: str
    replay: bool


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class ControlAuthority:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        private_key: Ed25519PrivateKey | None = None,
    ) -> None:
        self.connection = connection
        self.private_key = private_key or Ed25519PrivateKey.generate()
        for statement in (
            """CREATE TABLE IF NOT EXISTS control_responder_authorizations (
                control_id TEXT NOT NULL,
                principal TEXT NOT NULL,
                authorization_id TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL,
                nonce TEXT NOT NULL UNIQUE,
                expires_at_ms INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('authorized','consumed','revoked')),
                PRIMARY KEY(control_id,principal)
            )""",
            """CREATE TABLE IF NOT EXISTS chat_control_callback_refs (
                token_id TEXT PRIMARY KEY,
                signed_record_bytes BLOB NOT NULL,
                record_digest TEXT NOT NULL UNIQUE,
                signature_b64 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','consumed','revoked'))
            )""",
            """CREATE TABLE IF NOT EXISTS chat_control_responses (
                response_id TEXT PRIMARY KEY,
                control_id TEXT NOT NULL,
                native_delivery_id TEXT NOT NULL UNIQUE,
                principal TEXT NOT NULL,
                choice TEXT NOT NULL,
                token_id TEXT NOT NULL,
                UNIQUE(control_id)
            )""",
            """CREATE TABLE IF NOT EXISTS chat_control_effects (
                control_id TEXT PRIMARY KEY,
                response_id TEXT NOT NULL UNIQUE,
                effect_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('effect-pending','effect-unknown','settled'))
            )""",
        ):
            connection.execute(statement)
        connection.commit()

    def authorize_responder(
        self,
        *,
        control_id: str,
        principal: str,
        authorization_id: str,
        generation: int,
        nonce: str,
        expires_at_ms: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO control_responder_authorizations VALUES (?,?,?,?,?,?,'authorized')",
                (control_id, principal, authorization_id, generation, nonce, expires_at_ms),
            )

    def issue(
        self,
        *,
        control_id: str,
        presentation_id: str,
        instance_id: str,
        instance_generation: int,
        chat_ref_id: str,
        chat_ref_generation: int,
        allowed_principals: tuple[str, ...],
        allowed_choices: tuple[str, ...],
        expires_at_ms: int,
    ) -> str:
        principals = tuple(sorted(set(allowed_principals)))
        choices = tuple(sorted(set(allowed_choices)))
        if not principals or not choices:
            raise ControlDenied("CONTROL_PRESENTATION_INVALID")
        bindings: list[dict[str, object]] = []
        for principal in principals:
            row = self.connection.execute(
                "SELECT authorization_id,generation,nonce,expires_at_ms,state "
                "FROM control_responder_authorizations WHERE control_id=? AND principal=?",
                (control_id, principal),
            ).fetchone()
            if row is None or str(row[4]) != "authorized" or int(row[3]) < expires_at_ms:
                raise ControlDenied("CONTROL_RESPONDER_AUTHORIZATION_REQUIRED")
            bindings.append(
                {
                    "principal": principal,
                    "authorizationId": str(row[0]),
                    "generation": int(row[1]),
                    "nonce": str(row[2]),
                    "expiresAtMs": int(row[3]),
                }
            )
        token_id = uuid.uuid4().hex
        body = {
            "schema": "hyprial.chat.control-callback-ref/v1",
            "tokenId": token_id,
            "controlId": control_id,
            "presentationId": presentation_id,
            "instanceId": instance_id,
            "instanceGeneration": instance_generation,
            "chatRefId": chat_ref_id,
            "chatRefGeneration": chat_ref_generation,
            "allowedPrincipals": principals,
            "allowedChoices": choices,
            "responderBindings": bindings,
            "expiresAtMs": expires_at_ms,
        }
        payload = _canonical(body)
        digest = hashlib.sha256(b"hyprial-control-callback-ref-v1\0" + payload).hexdigest()
        signature = self.private_key.sign(
            b"hyprial-control-callback-ref-v1\0" + bytes.fromhex(digest) + payload
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO chat_control_callback_refs VALUES (?,?,?,?,'active')",
                (token_id, payload, digest, base64.b64encode(signature).decode("ascii")),
            )
        return "hc1." + base64.urlsafe_b64encode(token_id.encode()).decode().rstrip("=")

    def _record(self, token: str) -> tuple[str, dict[str, object]]:
        if not token.startswith("hc1."):
            raise ControlDenied("CONTROL_CALLBACK_INVALID")
        encoded = token[4:]
        try:
            token_id = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        except (ValueError, UnicodeDecodeError) as error:
            raise ControlDenied("CONTROL_CALLBACK_INVALID") from error
        row = self.connection.execute(
            "SELECT signed_record_bytes,record_digest,signature_b64 FROM "
            "chat_control_callback_refs WHERE token_id=?",
            (token_id,),
        ).fetchone()
        if row is None:
            raise ControlDenied("CONTROL_CALLBACK_INVALID")
        payload = bytes(row[0])
        digest = hashlib.sha256(b"hyprial-control-callback-ref-v1\0" + payload).hexdigest()
        if digest != str(row[1]):
            raise ControlDenied("CONTROL_CALLBACK_RECORD_CORRUPT")
        try:
            self.private_key.public_key().verify(
                base64.b64decode(str(row[2]), validate=True),
                b"hyprial-control-callback-ref-v1\0" + bytes.fromhex(digest) + payload,
            )
            value = json.loads(payload)
        except (ValueError, json.JSONDecodeError) as error:
            raise ControlDenied("CONTROL_CALLBACK_SIGNATURE_INVALID") from error
        return token_id, value

    def revoke(self, token: str) -> None:
        token_id, _ = self._record(token)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT state FROM chat_control_callback_refs WHERE token_id=?",
                (token_id,),
            ).fetchone()
            if row is None:
                raise ControlDenied("CONTROL_CALLBACK_INVALID")
            state = str(row[0])
            if state == "consumed":
                raise ControlDenied("CONTROL_CALLBACK_CONSUMED")
            if state == "active":
                self.connection.execute(
                    "UPDATE chat_control_callback_refs SET state='revoked' "
                    "WHERE token_id=? AND state='active'",
                    (token_id,),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def respond(
        self,
        token: str,
        *,
        response_id: str,
        native_delivery_id: str,
        instance_id: str,
        instance_generation: int,
        chat_ref_id: str,
        chat_ref_generation: int,
        principal: str,
        choice: str,
        now_ms: int,
    ) -> ControlReceipt:
        token_id, value = self._record(token)
        control_id = str(value["controlId"])
        if (
            value["instanceId"] != instance_id
            or value["instanceGeneration"] != instance_generation
            or value["chatRefId"] != chat_ref_id
            or value["chatRefGeneration"] != chat_ref_generation
        ):
            raise ControlDenied("CONTROL_CALLBACK_SCOPE_MISMATCH")
        if now_ms >= int(value["expiresAtMs"]):
            raise ControlDenied("CONTROL_CALLBACK_EXPIRED")
        if principal not in value["allowedPrincipals"] or choice not in value["allowedChoices"]:
            raise ControlDenied("CONTROL_CALLBACK_CHOICE_OR_PRINCIPAL_DENIED")
        binding = next(
            (
                item
                for item in value["responderBindings"]
                if item["principal"] == principal
            ),
            None,
        )
        if binding is None:
            raise ControlDenied("CONTROL_RESPONDER_AUTHORIZATION_REQUIRED")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self.connection.execute(
                "SELECT response_id,control_id,principal,choice,token_id "
                "FROM chat_control_responses WHERE native_delivery_id=?",
                (native_delivery_id,),
            ).fetchone()
            if replay is not None:
                if tuple(map(str, replay)) != (
                    response_id,
                    control_id,
                    principal,
                    choice,
                    token_id,
                ):
                    raise ControlDenied("CONTROL_CALLBACK_REPLAY_CONFLICT")
                self.connection.commit()
                return ControlReceipt(control_id, response_id, principal, choice, True)
            callback = self.connection.execute(
                "SELECT state FROM chat_control_callback_refs WHERE token_id=?",
                (token_id,),
            ).fetchone()
            if callback is None:
                raise ControlDenied("CONTROL_CALLBACK_INVALID")
            if str(callback[0]) != "active":
                suffix = "REVOKED" if str(callback[0]) == "revoked" else "CONSUMED"
                raise ControlDenied(f"CONTROL_CALLBACK_{suffix}")
            row = self.connection.execute(
                "SELECT authorization_id,generation,nonce,expires_at_ms,state FROM "
                "control_responder_authorizations WHERE control_id=? AND principal=?",
                (control_id, principal),
            ).fetchone()
            expected = (
                str(binding["authorizationId"]),
                int(binding["generation"]),
                str(binding["nonce"]),
            )
            if (
                row is None
                or tuple((str(row[0]), int(row[1]), str(row[2]))) != expected
                or int(row[3]) <= now_ms
                or str(row[4]) != "authorized"
            ):
                raise ControlDenied("CONTROL_RESPONDER_AUTHORIZATION_MISMATCH")
            self.connection.execute(
                "INSERT INTO chat_control_responses VALUES (?,?,?,?,?,?)",
                (response_id, control_id, native_delivery_id, principal, choice, token_id),
            )
            updated = self.connection.execute(
                "UPDATE control_responder_authorizations SET state='consumed' "
                "WHERE control_id=? AND principal=? AND state='authorized'",
                (control_id, principal),
            ).rowcount
            if updated != 1:
                raise ControlDenied("CHAT_CONTROL_ALREADY_SETTLED")
            self.connection.execute(
                "INSERT INTO chat_control_effects VALUES (?,?,?,'effect-pending')",
                (control_id, response_id, f"control-effect:{control_id}"),
            )
            callback_updated = self.connection.execute(
                "UPDATE chat_control_callback_refs SET state='consumed' "
                "WHERE token_id=? AND state='active'",
                (token_id,),
            ).rowcount
            if callback_updated != 1:
                raise ControlDenied("CONTROL_CALLBACK_NOT_ACTIVE")
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            raise ControlDenied("CHAT_CONTROL_ALREADY_SETTLED") from error
        except BaseException:
            self.connection.rollback()
            raise
        return ControlReceipt(control_id, response_id, principal, choice, False)

    def effect_count(self, control_id: str) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM chat_control_effects WHERE control_id=?", (control_id,)
        ).fetchone()[0])


def verify_control_callback_ref(value: dict[str, object]) -> dict[str, object]:
    if value.get("fullDigest") != value.get("responderSetDigest"):
        return {"ok": False, "code": "CONTROL_RESPONDER_DIGEST_MISMATCH", "responseRows": 0}
    return {"ok": True, "code": None, "responseRows": 0}


def accept_control_response(value: dict[str, object]) -> dict[str, object]:
    responders = tuple(value.get("responders", ()))
    responses = tuple(value.get("responses", ()))
    winner = next((item for item in responses if item in responders), None)
    return {
        "winner": winner,
        "consumedRows": int(winner is not None),
        "acceptedResponses": int(winner is not None),
        "version": int(value["expectedVersion"]) + int(winner is not None),
    }


def present_control_intent(value: dict[str, object]) -> dict[str, object]:
    if value.get("responderAuthorization") is None:
        return {
            "ok": False,
            "code": "CONTROL_RESPONDER_AUTHORIZATION_REQUIRED",
            "callbacks": 0,
        }
    return {"ok": True, "code": None, "callbacks": 1}


__all__ = [
    "ControlAuthority",
    "ControlDenied",
    "ControlReceipt",
    "accept_control_response",
    "present_control_intent",
    "verify_control_callback_ref",
]
