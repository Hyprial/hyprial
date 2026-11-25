"""E2E-004 evidence; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from feishu.settings import EVENT_TIMEOUT_SECONDS

_NATIVE_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?P<kind>oc|ou)_[0-9A-Za-z_-]+")


@dataclass(frozen=True)
class EvidencePorts:
    """Only this module's observed collaborators and configuration."""

    POLL_SECONDS: float


#: Non-text coverage venue.  The shared test GROUP only delivers messages that
#: @mention the bot, and an `image`/`file`/`merge_forward` message cannot carry
#: a mention, so those types provably never reach the App there (measured: sent
#: to the group, no `im.message.receive_v1` event arrives).  The bot's own p2p
#: DM delivers every type, so non-text assertions run there.  Its id is
#: resolved live from the bot's open_id and never committed, exactly like the
#: bot's @mention: a DM is a per-developer private conversation.


class ContractFailure(AssertionError):
    pass


class ContractBlocked(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def native_id_label(value: str) -> str:
    """Return a stable correlation label without disclosing a native ID."""

    kind = "chat" if value.startswith("oc_") else "user"
    return f"{kind}#{text_digest(value)}"


def redact_native_ids(value: str) -> str:
    """Redact chat and open-user IDs from console/error/evidence strings."""

    return _NATIVE_ID_PATTERN.sub(lambda match: native_id_label(match.group(0)), value)


def _sanitize_output_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_native_ids(value)
    if isinstance(value, dict):
        return {key: _sanitize_output_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_output_value(item) for item in value]
    return value


def assert_sanitized_evidence(
    evidence: str, *, forbidden_values: tuple[str | None, ...]
) -> None:
    """Fail A10 if a secret, plaintext, chat ID, or open-user ID escaped."""

    if any(value and value in evidence for value in forbidden_values):
        raise ContractFailure(
            "evidence log contains a secret, plaintext, or sensitive native identifier"
        )
    if _NATIVE_ID_PATTERN.search(evidence) is not None:
        raise ContractFailure("evidence log contains a sensitive native identifier")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_cli_error(stderr: str, stdout: str) -> str:
    for candidate in (stderr, stdout):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        error = value.get("error") if isinstance(value, dict) else None
        if isinstance(error, dict):
            safe = {
                key: error.get(key)
                for key in (
                    "type",
                    "subtype",
                    "code",
                    "message",
                    "hint",
                    "missing_scopes",
                )
                if error.get(key) is not None
            }
            return redact_native_ids(
                json.dumps(safe, ensure_ascii=False, sort_keys=True)
            )
    tail = (stderr or stdout)[-1000:].replace("\n", " ")
    return redact_native_ids(tail) or "no diagnostic output"


class EvidenceLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields: Any) -> None:
        record = _sanitize_output_value(
            {"kind": kind, "ts": utc_now(), "ts_ms": epoch_ms(), **fields}
        )
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read_evidence(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            records.append(value)
    return records


def wait_for_evidence(
    path: Path,
    kind: str,
    predicate: Callable[[Mapping[str, Any]], bool] | None = None,
    *,
    timeout: float = EVENT_TIMEOUT_SECONDS,
    process: subprocess.Popen[str] | None = None,
    _ports: EvidencePorts,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in read_evidence(path):
            if record.get("kind") != kind:
                continue
            if predicate is None or predicate(record):
                return record
        if process is not None and process.poll() is not None:
            raise ContractFailure(
                f"gateway child exited before {kind}: {process.returncode}"
            )
        time.sleep(_ports.POLL_SECONDS)
    raise ContractFailure(f"timed out waiting for evidence: {kind}")


class _FailurePreservingTempDir(tempfile.TemporaryDirectory):
    """Copy the scenario tree out before cleanup when the body raised.

    The workflow's cleanup is success()-only, so $HYPRIAL_E2E_ROOT survives a
    failed run; without this copy the gateway child's evidence.ndjson and
    stdout/stderr vanish with the TemporaryDirectory.  That is exactly how
    the 2026-08-23 A8 red (\"OnIt did not clear\", runs #1393/#1395) lost
    its decisive row (adapter.delivery.cleanup_error): the failure text was
    retroactively unverifiable until this preservation existed.
    """

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if exc_type is not None:
            try:
                preserve_root = os.environ.get("HYPRIAL_E2E_ROOT", "").strip()
                if preserve_root:
                    target = Path(preserve_root) / "artifacts" / "E2E-004"
                else:
                    target = Path(self.name + "-preserved")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(self.name, target, dirs_exist_ok=True)
                print(
                    f"CLEANUP INFO E2E-004 failure state preserved at {target}",
                    file=sys.stderr,
                )
            except OSError as error:
                print(
                    "CLEANUP WARN E2E-004 failure-state preserve failed: "
                    f"{type(error).__name__}",
                    file=sys.stderr,
                )
        return super().__exit__(exc_type, exc, tb)
