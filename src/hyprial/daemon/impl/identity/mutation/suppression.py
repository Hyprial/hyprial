"""Durable operator intent for organization binding publication."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from hyprial.kernel import atomic_json_write


IDENTITY_BINDING_SUPPRESSION_FILENAME = "identity-binding-suppression.json"
_BASE_KEYS = frozenset({"all", "except", "orgs"})
_KEYS = _BASE_KEYS | {"setBy"}


class IdentityBindingSuppression:
    """Atomic 0600 suppression state, including exceptions to ``all``."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / IDENTITY_BINDING_SUPPRESSION_FILENAME
        self._lock = threading.Lock()

    @staticmethod
    def _operator(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("identity binding suppression attribution is invalid")
        if (
            value.get("operator") != "unverified"
            or value.get("operatorVerified") is not False
        ):
            raise ValueError("identity binding suppression attribution is invalid")
        if set(value) - {"operator", "operatorVerified", "callerPid"}:
            raise ValueError("identity binding suppression attribution is invalid")
        caller_pid = value.get("callerPid")
        if caller_pid is not None and (
            isinstance(caller_pid, bool)
            or not isinstance(caller_pid, int)
            or caller_pid <= 0
        ):
            raise ValueError("identity binding suppression attribution is invalid")
        return dict(value)

    def _read(
        self,
    ) -> tuple[bool, set[str], set[str], dict[str, object]]:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False, set(), set(), {}
        if not isinstance(record, dict) or set(record) not in {_BASE_KEYS, _KEYS}:
            raise ValueError("identity binding suppression state is invalid")
        all_orgs = record.get("all")
        orgs = record.get("orgs")
        exceptions = record.get("except")
        if (
            not isinstance(all_orgs, bool)
            or not isinstance(orgs, list)
            or not isinstance(exceptions, list)
            or any(not isinstance(org, str) or not org for org in orgs)
            or any(not isinstance(org, str) or not org for org in exceptions)
        ):
            raise ValueError("identity binding suppression state is invalid")
        raw_set_by = record.get("setBy", {})
        if not isinstance(raw_set_by, dict) or set(raw_set_by) - {"all", "orgs"}:
            raise ValueError("identity binding suppression attribution is invalid")
        set_by: dict[str, object] = {}
        if "all" in raw_set_by:
            set_by["all"] = self._operator(raw_set_by["all"])
        raw_orgs = raw_set_by.get("orgs", {})
        if not isinstance(raw_orgs, dict) or any(
            not isinstance(name, str) or not name for name in raw_orgs
        ):
            raise ValueError("identity binding suppression attribution is invalid")
        if raw_orgs:
            set_by["orgs"] = {
                name: self._operator(value) for name, value in raw_orgs.items()
            }
        return all_orgs, set(orgs), set(exceptions), set_by

    def _write(
        self,
        all_orgs: bool,
        orgs: set[str],
        exceptions: set[str],
        set_by: dict[str, object],
    ) -> None:
        atomic_json_write(
            self.path,
            {
                "all": all_orgs,
                "except": sorted(exceptions),
                "orgs": sorted(orgs),
                "setBy": set_by,
            },
        )

    def check(self) -> None:
        """Raise ``ValueError`` when the stored state is unreadable; change nothing."""
        with self._lock:
            self._read()

    def withdraw(
        self,
        *,
        org: str | None,
        all_orgs: bool,
        set_by: dict[str, object],
    ) -> None:
        with self._lock:
            current_all, orgs, exceptions, attribution = self._read()
            if all_orgs:
                self._write(True, set(), set(), {"all": dict(set_by)})
                return
            assert org is not None
            orgs.add(org)
            exceptions.discard(org)
            org_attribution = dict(attribution.get("orgs", {}))
            org_attribution[org] = dict(set_by)
            attribution["orgs"] = org_attribution
            self._write(current_all, orgs, exceptions, attribution)

    def publish(self, *, org: str | None, all_orgs: bool) -> None:
        with self._lock:
            current_all, orgs, exceptions, attribution = self._read()
            if all_orgs:
                self._write(False, set(), set(), {})
                return
            assert org is not None
            orgs.discard(org)
            org_attribution = dict(attribution.get("orgs", {}))
            org_attribution.pop(org, None)
            if org_attribution:
                attribution["orgs"] = org_attribution
            else:
                attribution.pop("orgs", None)
            if current_all:
                exceptions.add(org)
            self._write(current_all, orgs, exceptions, attribution)

    def allowed(self, orgs: tuple[str, ...]) -> tuple[str, ...]:
        with self._lock:
            all_orgs, suppressed, exceptions, _set_by = self._read()
        return tuple(
            org
            for org in orgs
            if org not in suppressed and (not all_orgs or org in exceptions)
        )

    def status(self) -> dict[str, object] | None:
        """Return active durable suppression and its unverified setter."""

        with self._lock:
            all_orgs, orgs, exceptions, set_by = self._read()
        if not all_orgs and not orgs:
            return None
        return {
            "all": all_orgs,
            "except": sorted(exceptions),
            "orgs": sorted(orgs),
            "setBy": set_by,
        }


__all__ = [
    "IDENTITY_BINDING_SUPPRESSION_FILENAME",
    "IdentityBindingSuppression",
]
