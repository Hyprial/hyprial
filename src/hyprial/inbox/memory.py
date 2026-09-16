"""Deterministic delivery transport for contracts and daemon unit tests."""

from __future__ import annotations

from .api import InboxMessage


class MemoryDeliveryTransport:
    def __init__(self) -> None:
        self._online: set[str] = set()
        self._mailboxes: list[str] = []
        self._direct_confirmations: set[str] = set()
        self._fetch_confirmations: set[str] = set()
        self._custody_confirmations: set[str] = set()
        self.delivered: list[str] = []
        self.custody_transfers: list[tuple[str, str]] = []
        self.notices: list[tuple[str, InboxMessage]] = []
        self.progress_events: list[tuple[str, InboxMessage]] = []

    def set_online(self, recipient: str, online: bool) -> None:
        if online:
            self._online.add(recipient)
        else:
            self._online.discard(recipient)

    def confirm_direct(self, message_id: str) -> None:
        self._direct_confirmations.add(message_id)

    def confirm_custody(self, message_id: str) -> None:
        self._custody_confirmations.add(message_id)

    def confirm_fetch(self, message: InboxMessage) -> bool:
        return message.message_id in self._fetch_confirmations

    def confirm_fetched(self, message_id: str) -> None:
        self._fetch_confirmations.add(message_id)

    def advertise_mailbox(self, node: str) -> None:
        if node not in self._mailboxes:
            self._mailboxes.append(node)

    def is_online(self, recipient: str) -> bool:
        return recipient in self._online

    def deliver(self, message: InboxMessage) -> bool:
        self.delivered.append(message.message_id)
        return message.message_id in self._direct_confirmations

    def online_mailboxes(self) -> tuple[str, ...]:
        return tuple(self._mailboxes)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        self.custody_transfers.append((mailbox, message.message_id))
        return message.message_id in self._custody_confirmations

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        self.notices.append((node, message))
        return True

    def deliver_progress(self, node: str, message: InboxMessage) -> bool:
        self.progress_events.append((node, message))
        return True
