"""Contract Mock example loading and validation for the daemon request projection.

The served mock is the JavaScript module ``gui/contract-mock``; this package no
longer serves anything.
"""

from __future__ import annotations

from .checks import check_contract
from .contract import (
    DEFAULT_CONTRACT,
    DEFAULT_ENVELOPE,
    SCENARIOS,
    Contract,
    ContractError,
    load_contract,
)
from .envelope import Envelope

__all__ = [
    "Contract",
    "ContractError",
    "DEFAULT_CONTRACT",
    "DEFAULT_ENVELOPE",
    "Envelope",
    "SCENARIOS",
    "check_contract",
    "load_contract",
]
