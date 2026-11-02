"""The ``hyprial message`` command family.

Allen 2026-10-06: the message domain is one top-level noun.  Delivery
observation (``status``/``trajectory``/``inbox``/``outbox``/``targets``) lives
here; the sending verbs join it in a later slice.
"""

from hyprial.shell.impl.cli.commands.common.groups import group


message_app = group(
    help="Send, read, and observe the full message lifecycle."
)
