# Decision record (native_imports/launch, W1E-1 2026-11-21):
# - Updated _snapshot to consume the Path returned by native._open_private_chain
#   after removing POSIX dir_fd handles from native_credential and manifest.
"""Per-launch native selection and completion evidence in the existing import record.

The carrier must select its actual native root. Completion compares the same
import generation and private file snapshot again. No-op imports retain it;
replacement, logout, external edits and incarnation changes cannot inherit it.
Observation failure leaves verification unknown without breaking a real turn.
"""

from pathlib import Path

from . import serialized
from .manifest import observe


def _snapshot(agent_home, harness, record):
    from .. import native_credential as native

    directory = native._open_private_chain(Path(agent_home), harness, create=False)
    if directory is None:
        return None
    allowed = native._allowed(harness)
    if any(native._lstat_at(name + suffix, directory) is not None
           for name in allowed for suffix in (native._STAGING_SUFFIX, native._PREVIOUS_SUFFIX)):
        return None
    return record.snapshot(harness, observe(directory, allowed))


@serialized
def _capture(agent_home, harness, *, import_record):
    snapshot = _snapshot(agent_home, harness, import_record)
    return NativeAuthLaunch(agent_home, harness, import_record, snapshot) if snapshot is not None else None


def capture_native_auth_launch(agent_home, harness, *, import_record):
    """Capture bounded import provenance; absence or unsafe state proves nothing."""
    try:
        return _capture(agent_home, harness, import_record=import_record)
    except Exception:
        return None


@serialized
def _match(agent_home, harness, *, import_record, snapshot, at_ms=None):
    if _snapshot(agent_home, harness, import_record) != snapshot:
        return False
    if at_ms is not None:
        import_record.confirm(harness, snapshot, at_ms)
    return True


class NativeAuthLaunch:
    """Internal, non-serializable observation bound to one launch and import."""

    def __init__(self, agent_home, harness, record, snapshot):
        self._home = Path(agent_home)
        self._harness = harness
        self._record = record
        self._snapshot = snapshot
        self._selected = False

    @property
    def snapshot(self):
        """The (import_id, manifest) pair this launch was captured against."""

        return self._snapshot

    def select(self, native_root: Path | None) -> None:
        """Called by the carrier after native authentication selection."""
        self._selected = False
        try:
            expected = self._home / "secrets" / "native" / self._harness
            self._selected = native_root is not None and Path(native_root).resolve() == expected.resolve() and _match(
                self._home, self._harness, import_record=self._record, snapshot=self._snapshot,
            )
        except Exception:
            return

    def confirm(self, at_ms: int) -> None:
        """Called only for a successful response from this launch's carrier."""
        if self._selected:
            try:
                _match(self._home, self._harness, import_record=self._record, snapshot=self._snapshot, at_ms=at_ms)
            except Exception:
                return
