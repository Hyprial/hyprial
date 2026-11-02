"""Logical cell attachments; no mailbox, execution loop or business authority."""

from .model import CellSnapshot
from .ports import CellObservationPort

__all__ = ["CellObservationPort", "CellSnapshot"]
