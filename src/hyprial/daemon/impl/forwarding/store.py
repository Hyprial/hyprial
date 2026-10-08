"""The durable network exposure store."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from .policy import (
    ForwardingSidecarError,
    _normalize_exposure,
)


class ExposureStore:
    """Small atomic desired-state file owned by the daemon state directory."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._rejected: list[dict[str, object]] = []

    def _read(self) -> dict[int, dict[str, object]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise ForwardingSidecarError(
                f"cannot read desired network exposures: {self._path}"
            ) from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ForwardingSidecarError(
                "desired network exposures have an unsupported format"
            )
        items = raw.get("exposures")
        if not isinstance(items, list):
            raise ForwardingSidecarError("desired network exposures must be an array")
        result: dict[int, dict[str, object]] = {}
        rejected: list[dict[str, object]] = []
        for item in items:
            # One entry that no longer validates (e.g. v2 on a tcp target,
            # written before that was refused) must not take down forwarding:
            # skip it and keep it visible instead.
            try:
                if not isinstance(item, dict):
                    raise ForwardingSidecarError(
                        "desired network exposure must be an object"
                    )
                normalized = _normalize_exposure(item)
            except ForwardingSidecarError as error:
                rejected.append({"exposure": item, "reason": str(error)})
                continue
            result[int(normalized["port"])] = normalized
        self._rejected = rejected
        return result

    def rejected(self) -> list[dict[str, object]]:
        """Persisted entries skipped by the last read, with their reasons."""

        with self._lock:
            self._read()
            return list(self._rejected)

    def _write(self, exposures: Mapping[int, Mapping[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "version": 1,
            "exposures": [dict(exposures[port]) for port in sorted(exposures)],
        }
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, self._path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return list(self._read().values())

    def set(self, exposure: Mapping[str, object]) -> dict[str, object]:
        normalized = _normalize_exposure(exposure)
        with self._lock:
            exposures = self._read()
            port = int(normalized["port"])
            existing = exposures.get(port)
            if existing is not None and existing != normalized:
                raise ForwardingSidecarError(
                    f"port {port} is already exposed; unexpose it before changing the target"
                )
            exposures[port] = normalized
            self._write(exposures)
        return normalized

    def remove(self, port: int) -> bool:
        with self._lock:
            exposures = self._read()
            removed = exposures.pop(port, None) is not None
            if removed:
                self._write(exposures)
            return removed
