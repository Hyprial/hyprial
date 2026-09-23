"""Canonical, versioned Zenoh key-expression layout."""

from __future__ import annotations

import base64
import re

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._:-]+$")


class KeySpace:
    prefix = "hyprial/v1"

    @staticmethod
    def encode_identity(value: str) -> str:
        if not value:
            raise ValueError("identity must not be empty")
        if _SAFE_SEGMENT.fullmatch(value):
            return value
        encoded = (
            base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
        )
        return f"~{encoded}"

    @staticmethod
    def decode_identity(value: str) -> str:
        if not value.startswith("~"):
            return value
        encoded = value[1:]
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")

    def inbox(self, actor: str, message_id: str) -> str:
        return f"{self.prefix}/inbox/{self.encode_identity(actor)}/{self.encode_identity(message_id)}"

    def inbox_all(self, actor: str) -> str:
        return f"{self.prefix}/inbox/{self.encode_identity(actor)}/**"

    def receipt(self, sender: str, message_id: str) -> str:
        return f"{self.prefix}/receipt/{self.encode_identity(sender)}/{self.encode_identity(message_id)}"

    def receipt_all(self, sender: str) -> str:
        return f"{self.prefix}/receipt/{self.encode_identity(sender)}/**"

    def receipt_any(self) -> str:
        return f"{self.prefix}/receipt/**"

    def fetch_receipt(self, sender: str, message_id: str) -> str:
        """Queryable proof that a pull consumer fetched one inbox row."""

        return (
            f"{self.prefix}/fetch-receipt/{self.encode_identity(sender)}/"
            f"{self.encode_identity(message_id)}"
        )

    def fetch_receipt_any(self) -> str:
        return f"{self.prefix}/fetch-receipt/**"

    def custody(self, mailbox: str, message_id: str) -> str:
        return f"{self.prefix}/custody/{self.encode_identity(mailbox)}/{self.encode_identity(message_id)}"

    def custody_all(self, mailbox: str) -> str:
        return f"{self.prefix}/custody/{self.encode_identity(mailbox)}/**"

    def custody_receipt(self, sender: str, message_id: str) -> str:
        return f"{self.prefix}/custody-receipt/{self.encode_identity(sender)}/{self.encode_identity(message_id)}"

    def custody_receipt_any(self) -> str:
        return f"{self.prefix}/custody-receipt/**"

    def system_notice(self, node: str, message_id: str) -> str:
        """A non-receipted alarm addressed to the sender's daemon."""

        return (
            f"{self.prefix}/system-notice/{self.encode_identity(node)}/"
            f"{self.encode_identity(message_id)}"
        )

    def system_notice_all(self, node: str) -> str:
        return f"{self.prefix}/system-notice/{self.encode_identity(node)}/**"

    def progress(self, node: str, message_id: str) -> str:
        """A non-receipted progress event addressed to the sender's daemon."""

        return (
            f"{self.prefix}/progress/{self.encode_identity(node)}/"
            f"{self.encode_identity(message_id)}"
        )

    def progress_all(self, node: str) -> str:
        return f"{self.prefix}/progress/{self.encode_identity(node)}/**"

    def registry(self, actor: str) -> str:
        return f"{self.prefix}/registry/{self.encode_identity(actor)}"

    def actor_liveliness(self, actor: str) -> str:
        return f"{self.prefix}/liveliness/actor/{self.encode_identity(actor)}"

    def mailbox_liveliness(self, node: str) -> str:
        return f"{self.prefix}/liveliness/mailbox/{self.encode_identity(node)}"

    def mailbox_liveliness_all(self) -> str:
        return f"{self.prefix}/liveliness/mailbox/*"

    def daemon_liveliness(self, node: str, generation: str) -> str:
        """One daemon process's generation-bearing liveliness token.

        Actor and mailbox tokens name an identity only, so two daemons
        sharing one node identity declare byte-identical keys and no
        observer can tell one instance from two.  The generation segment
        makes each process's token distinct, which is what lets a peer
        notice a duplicate instance of its own node identity.
        """

        return (
            f"{self.prefix}/liveliness/daemon/{self.encode_identity(node)}/"
            f"{self.encode_identity(generation)}"
        )

    def daemon_liveliness_for_node(self, node: str) -> str:
        """Wildcard matching every daemon generation of one node identity."""

        return f"{self.prefix}/liveliness/daemon/{self.encode_identity(node)}/*"

    def user_delivery(self, owner: str, message_id: str) -> str:
        return (
            f"{self.prefix}/user/{self.encode_identity(owner)}/"
            f"{self.encode_identity(message_id)}"
        )

    def user_delivery_any(self) -> str:
        return f"{self.prefix}/user/**"

    def user_receipt(self, sender: str, message_id: str) -> str:
        return (
            f"{self.prefix}/user-receipt/{self.encode_identity(sender)}/"
            f"{self.encode_identity(message_id)}"
        )

    def user_receipt_any(self) -> str:
        return f"{self.prefix}/user-receipt/**"

    # --- owner-sovereign organization context -------------------------------

    def org_context(self, node: str) -> str:
        """Best-effort announcement of one node's accepted document."""

        return f"{self.prefix}/org/context/{self.encode_identity(node)}"

    def org_context_any(self) -> str:
        return f"{self.prefix}/org/context/*"

    def org_request(self, node: str) -> str:
        """Queryable for one node's accepted document (never its pending set)."""

        return f"{self.prefix}/org/request/{self.encode_identity(node)}"

    def org_request_any(self) -> str:
        return f"{self.prefix}/org/request/*"

    # --- delivery terminal state (msg/ namespace, design section 5) ----------
    # Owned by the delivery line.  The agent-entity line owns registry/; these
    # two namespaces are disjoint by construction so the lines cannot collide.
    # Queries must use the concrete forms below: a queryable replies with the
    # query's own key expression, which a wildcard cannot be.

    def message_status_root(self, sender: str) -> str:
        """Every terminal record a holder has for this sender."""

        return f"{self.prefix}/msg/status/{self.encode_identity(sender)}"

    def message_status(self, sender: str, message_id: str) -> str:
        """One sent message's terminal record."""

        return (
            f"{self.prefix}/msg/status/{self.encode_identity(sender)}/"
            f"{self.encode_identity(message_id)}"
        )

    def message_status_any(self) -> str:
        """Declaration side: one status queryable serves a whole node."""

        return f"{self.prefix}/msg/status/**"
