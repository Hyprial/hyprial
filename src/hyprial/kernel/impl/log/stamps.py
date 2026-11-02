"""Time-and-instance stamps for a process's own stdout/stderr records.

Service logs survive restarts. JSON objects gain fields; text gains a prefix.
The wrapper applies only to Python text writes, not inherited file descriptors
or native code. Fragmented JSON is held until the object is complete so that
json.dump and pretty-printed objects remain parseable.
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from typing import Any, TextIO


def utc_stamp() -> str:
    """UTC ISO-8601 with milliseconds and a Z suffix."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LogStamp:
    """One process's time-and-instance identity for its stdio records."""

    def __init__(self) -> None:
        self._epoch: str | None = None

    @property
    def epoch(self) -> str | None:
        return self._epoch

    def bind_epoch(self, epoch: str) -> None:
        """Name the daemon instance once the runtime has minted it."""
        self._epoch = epoch

    def fields(self) -> dict[str, Any]:
        stamped: dict[str, Any] = {"t": utc_stamp(), "pid": os.getpid()}
        if self._epoch is not None:
            stamped["epoch"] = self._epoch
        return stamped

    def prefix(self) -> str:
        return " ".join(f"{key}={value}" for key, value in self.fields().items()) + " "


def stamp_json_record(record: str, stamp: LogStamp) -> str | None:
    """Stamp a complete object, replacing reserved fields without duplicates."""
    try:
        parsed = json.loads(record)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed.update(stamp.fields())
    if stamp.epoch is None:
        parsed.pop("epoch", None)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) + (
        "\n" if record.endswith("\n") else ""
    )


def _can_continue_after_newline(record: str) -> bool:
    """Only an incomplete structural token can continue past a physical LF."""
    try:
        json.loads(record)
    except json.JSONDecodeError as error:
        return error.pos >= len(record.rstrip())
    except (ValueError, RecursionError):
        return False
    return False


class StampedTextStream(io.TextIOBase):
    """A non-owning text stream that stamps records forwarded to target."""

    def __init__(self, target: TextIO, stamp: LogStamp) -> None:
        self._target = target
        self._stamp = stamp
        self._at_line_start = True
        self._pending_json = ""
        self._lock = RLock()

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError(f"text stream expects str, not {type(text).__name__}")
        with self._lock:
            self._target.write(self._stamped(text))
        return len(text)

    def flush(self) -> None:
        # A writer may flush between JSON fragments. Do not finalize here.
        with self._lock:
            self._target.flush()

    def close(self) -> None:
        self.finish()  # The service manager's underlying stream is not ours.

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._target.fileno()

    def isatty(self) -> bool:
        return self._target.isatty()

    @property
    def encoding(self) -> str | None:  # type: ignore[override]
        return getattr(self._target, "encoding", None)

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return getattr(self._target, "errors", None)

    def finish(self) -> None:
        """Emit an unfinished JSON candidate as diagnostic text on exit."""
        with self._lock:
            if self._pending_json:
                self._target.write("".join(
                    self._stamp.prefix() + line
                    for line in self._pending_json.splitlines(keepends=True)
                ))
                self._pending_json = ""
            self.flush()

    def _stamped(self, text: str) -> str:
        out: list[str] = []
        # Split on LF only: a carriage return is not a new log record.
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            record = piece + ("\n" if index < len(pieces) - 1 else "")
            if not record:
                continue
            if self._pending_json or (self._at_line_start and record.lstrip().startswith("{")):
                self._pending_json += record
                stamped = stamp_json_record(self._pending_json, self._stamp)
                if stamped is not None:
                    out.append(stamped)
                    self._pending_json = ""
                elif record.endswith("\n") and not _can_continue_after_newline(self._pending_json):
                    # A braced sentence or malformed JSON must not hold every
                    # later lifecycle/error line until daemon shutdown.
                    out.extend(self._stamp.prefix() + line
                               for line in self._pending_json.splitlines(keepends=True))
                    self._pending_json = ""
            elif self._at_line_start:
                out.append(self._stamp.prefix() + record)
            else:
                out.append(record)
            self._at_line_start = record.endswith("\n")
        return "".join(out)


@contextmanager
def stamp_std_streams(
    stamp: LogStamp,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> Iterator[LogStamp]:
    """Stamp Python stdout/stderr writes, then restore the previous streams."""
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    stamped_stdout = StampedTextStream(previous_stdout if stdout is None else stdout, stamp)
    stamped_stderr = StampedTextStream(previous_stderr if stderr is None else stderr, stamp)
    sys.stdout, sys.stderr = stamped_stdout, stamped_stderr
    try:
        yield stamp
    finally:
        try:
            for stream in (stamped_stdout, stamped_stderr):
                try:
                    stream.finish()
                except (OSError, ValueError):  # A closed log must not mask exit.
                    pass
        finally:
            sys.stdout, sys.stderr = previous_stdout, previous_stderr


__all__ = ["LogStamp", "StampedTextStream", "stamp_json_record", "stamp_std_streams", "utc_stamp"]
