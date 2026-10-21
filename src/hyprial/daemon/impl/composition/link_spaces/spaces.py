"""Discover link-bearing org spaces behind the legacy composition facade.

The scan resolves store and left-org readers through ``composition.links`` at
call time so its established monkeypatch targets remain effective.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space

#: ``OrgLinkSpaces`` refresh cadence: one org-meta read per five seconds.
SPACE_REFRESH_MS = 5_000


def _composition_links() -> Any:
    from hyprial.daemon.impl.composition import links

    return links


class OrgLinkSpaces:
    """The orgfs directory spaces that hold ``link/`` (wp2-composition §3)."""

    def __init__(
        self,
        fs: Any,
        *,
        home: Path,
        clock_ms: Callable[[], int],
        invalid_devices: Any,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self._fs = fs
        self._home = Path(home)
        self._clock = clock_ms
        self._invalid_devices = invalid_devices
        self._logger = logger
        self._at_ms = -(10**18)
        self._ready = False
        self._spaces: tuple[str, ...] = ()
        self._ambiguous: frozenset[str] = frozenset()
        self._skipped: dict[str, str] = {}

    def __call__(self) -> tuple[str, ...]:
        now = self._clock()
        if self._ready and now - self._at_ms < SPACE_REFRESH_MS:
            return self._spaces
        self._at_ms = now
        first = not self._ready
        self._ready = True
        result = self._resolve()
        if result != self._spaces:
            if not first:
                self._note_transition(result)
            if result:
                self._log(
                    "link.spaces_changed",
                    count=len(result),
                    spaceIds=result[:8],
                    skippedOrgSpaces=self.skipped_count,
                )
        self._spaces = result
        return result

    @property
    def skipped_count(self) -> int:
        return len(self._skipped)

    def _resolve(self) -> tuple[str, ...]:
        composition = _composition_links()
        store = composition.OrgFsDirectoryStore(
            self._fs,
            logger=self._logger,
            invalid_devices=self._invalid_devices,
        )
        left = set(composition.read_left_orgs(self._home))
        spaces: list[str] = []
        ambiguous: set[str] = set()
        skipped: dict[str, str] = {}
        for org in store.orgs():
            if org in left:
                continue
            space, reason = resolve_org_directory_space(self._fs, org)
            if space is None:
                if reason == "ambiguous-directory-space":
                    ambiguous.add(org)
                    if org not in self._ambiguous:
                        self._log("link.space_ambiguous", org=org)
                continue
            try:
                state, problem = store.org_state_details(org)
            except Exception as error:  # noqa: BLE001 - org state fails closed
                self._record_skip(
                    skipped,
                    org,
                    "state-unreadable",
                    errorType=type(error).__name__,
                )
                continue
            if problem is not None:
                self._record_skip(
                    skipped,
                    org,
                    "state-data-problem",
                    dataReason=str(problem.get("reason", "unknown")),
                )
                continue
            if state not in ("active", "legacy-active"):
                reason = (
                    state
                    if state in ("creating", "partial", "deleted")
                    else "unknown-state"
                )
                self._record_skip(skipped, org, reason)
                continue
            spaces.append(space.space_id)
        self._ambiguous = frozenset(ambiguous)
        self._skipped = skipped
        return tuple(sorted(spaces))

    def _record_skip(
        self, skipped: dict[str, str], org: str, reason: str, **fields: Any
    ) -> None:
        fingerprint = f"{reason}:{fields.get('dataReason', '')}"
        skipped[org] = fingerprint
        if self._skipped.get(org) != fingerprint:
            self._log(
                "link.space_skipped", level="warn", org=org, reason=reason, **fields
            )

    def _note_transition(self, result: tuple[str, ...]) -> None:
        if self._spaces and not result:
            self._log("link.org_unavailable")
        elif not self._spaces and result:
            self._log("link.org_available")

    def _log(self, event: str, *, level: str = "info", **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks a scan
            pass
