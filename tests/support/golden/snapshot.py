"""Record and compare daemon request goldens (G0 characterization baseline).

Each scenario is a list of steps.  A step is one ``app.handle`` call stored
in its IPC wire shape (``{"result": ...}`` or ``{"error": {code, message,
data?}}``), or one named observation of a fake's state.  Steps are compared
as ordered JSON text, so key order, values, codes and messages are all pinned.

``Golden.call`` applies the parts of ``ipc/server.py _response`` that decide
the recorded value: a ``message.send`` without ``idempotencyKey`` gets the
request id as its key, ``NameError``/``ImportError`` propagate (the server
re-raises them, so they fail the test instead of becoming a recorded
``DAEMON_ERROR``), and any other exception is framed by its ``code`` and
``data``.  It does not apply the version/envelope parsing or the
``_timed_handle`` IPC-stats accounting, neither of which changes a result.

Normalisation is an explicit allowlist and nothing else:

- the test's tmp root (and with it the socket path) becomes ``<tmp>``;
- a ``pid`` key holding this process id becomes ``<pid>``;
- the keys in ``KEY_PLACEHOLDERS`` (a filesystem mtime and a monotonic age,
  neither driven by the frozen wall clock) become their named placeholder
  suffixed with the value's JSON type (``<fs-mtime-ms:int>``), so a type
  change of those fields still shows in the diff;
- the MAC half of an inbox-batch cursor (``<payload>.<mac>``) becomes
  ``<cursor-mac>``: its key hashes the inbox database path, i.e. the tmp root.
  The cursor payload itself is kept verbatim;
- every value drawn from the seeded ``uuid4`` (as text or hex, also inside a
  longer string) becomes ``<uuid4#N>`` numbered by first appearance in the
  scenario, so an extra draw elsewhere cannot shift the recorded ids, while
  equal ids stay equal.  ``uuid5`` ids and digests are never drawn from
  ``uuid4`` and are compared verbatim; those derived from a drawn value stay
  stable because each call site draws from its own seeded sequence
  (``app.SeededUuid4``), whatever the thread interleaving;
- an id a scenario created outside the request surface (a fixture-built PAC
  graph) becomes the placeholder the scenario names through ``alias``.

The wall clock is frozen by the harness, so timestamps are compared
verbatim.  ``HYPRIAL_GOLDEN_RECORD=1`` writes snapshots instead of comparing;
the manifest test then re-pins their sha256 values.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.kernel import DaemonRequestError

SNAPSHOT_SCHEMA = "hyprial-golden/v1"
RECORD_ENV = "HYPRIAL_GOLDEN_RECORD"
_CURSOR = re.compile(r"(eyJ[A-Za-z0-9_-]{16,})\.[A-Za-z0-9_-]{22}(?![A-Za-z0-9_-])")
KEY_PLACEHOLDERS = {
    "agentHomeMtimeMs": "fs-mtime-ms",
    "heartbeatAgeSeconds": "monotonic-age-s",
}
SNAPSHOT_ROOT = (
    Path(__file__).resolve().parents[2] / "daemon" / "golden" / "snapshots"
)

__all__ = [
    "Golden",
    "GoldenMismatch",
    "KEY_PLACEHOLDERS",
    "RECORD_ENV",
    "SNAPSHOT_ROOT",
    "SNAPSHOT_SCHEMA",
    "recording",
    "render",
    "snapshot_path",
]


class GoldenMismatch(AssertionError):
    """A recorded golden no longer matches the current responses."""


def recording() -> bool:
    return os.environ.get(RECORD_ENV, "") == "1"


def snapshot_path(group: str, name: str) -> Path:
    return SNAPSHOT_ROOT / group / f"{name}.json"


def render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=1, ensure_ascii=False) + "\n"


class _Normaliser:
    def __init__(self, tmp_root: Path, drawn: Callable[[], list[str]]) -> None:
        self._tmp = str(tmp_root)
        self._drawn = drawn
        self._placeholders: dict[str, str] = {}
        self.aliases: dict[str, str] = {}

    def _uuid_tokens(self) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        for text in self._drawn():
            tokens.append((text, text))
            tokens.append((text.replace("-", ""), text))
        return tokens

    def _string(self, value: str) -> str:
        if self._tmp and self._tmp in value:
            value = value.replace(self._tmp, "<tmp>")
        for raw, placeholder in self.aliases.items():
            value = value.replace(raw, placeholder)
        value = _CURSOR.sub(r"\1.<cursor-mac>", value)
        hits: list[tuple[int, str, str]] = []
        for token, canonical in self._uuid_tokens():
            start = value.find(token)
            while start != -1:
                hits.append((start, token, canonical))
                start = value.find(token, start + len(token))
        if not hits:
            return value
        for _start, token, canonical in sorted(hits):
            placeholder = self._placeholders.setdefault(
                canonical, f"<uuid4#{len(self._placeholders) + 1}>"
            )
            value = value.replace(token, placeholder)
        return value

    def __call__(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {str(k): self(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            return self._string(value)
        if key == "pid" and value == os.getpid():
            return "<pid>"
        if key in KEY_PLACEHOLDERS and value is not None:
            return f"<{KEY_PLACEHOLDERS[key]}:{_json_type(value)}>"
        return value


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return type(value).__name__


def _wire(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


class Golden:
    """One scenario's recorded steps against one golden daemon."""

    def __init__(
        self,
        daemon: Any,
        group: str,
        name: str,
        *,
        drawn: Callable[[], list[str]],
    ) -> None:
        self.daemon = daemon
        self.group = group
        self.name = name
        self.steps: list[dict[str, Any]] = []
        self._normalise = _Normaliser(daemon.root, drawn)

    def call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        origin: str | None = "ipc",
        label: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Run one request through ``app.handle`` and record its wire shape."""

        step: dict[str, Any] = {
            "step": len(self.steps) + 1,
            "method": method,
        }
        if label is not None:
            step["label"] = label
        step["origin"] = origin
        if request_id is not None:
            step["requestId"] = request_id
        step["params"] = _wire(dict(params))
        effective = dict(params)
        if method == "message.send" and request_id and "idempotencyKey" not in effective:
            effective["idempotencyKey"] = request_id
        try:
            result = self.daemon.app.handle(
                method, effective, _trusted_message_origin=origin
            )
        except (NameError, ImportError):
            raise
        except DaemonRequestError as error:
            failure: dict[str, Any] = {"code": str(error.code), "message": str(error)}
            if error.data is not None:
                failure["data"] = _wire(error.data)
            step["error"] = failure
            self.steps.append(self._normalise(step))
            return error
        except Exception as error:  # noqa: BLE001 - the server frames these too
            failure = {
                "code": str(getattr(error, "code", "DAEMON_ERROR")),
                "message": str(error),
            }
            data = getattr(error, "data", None)
            if data is not None:
                failure["data"] = _wire(data)
            step["error"] = failure
            self.steps.append(self._normalise(step))
            return error
        step["result"] = _wire(result)
        self.steps.append(self._normalise(step))
        return result

    def alias(self, raw: str, placeholder: str) -> str:
        """Record ``raw`` (a fixture-made id) as ``placeholder`` from now on."""

        self._normalise.aliases[raw] = placeholder
        return raw

    def observe(self, label: str, value: Any) -> Any:
        """Record a named observation of a fake's state (not a request)."""

        self.steps.append(
            self._normalise(
                {"step": len(self.steps) + 1, "observe": label, "value": _wire(value)}
            )
        )
        return value

    def document(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "group": self.group,
            "scenario": self.name,
            "steps": self.steps,
        }

    def mismatch(self) -> str | None:
        """The diff against the stored snapshot, or None when it matches."""

        path = snapshot_path(self.group, self.name)
        actual = render(self.document())
        if not path.is_file():
            return f"golden snapshot missing: {path} (record with {RECORD_ENV}=1)"
        expected = path.read_text(encoding="utf-8")
        if expected == actual:
            return None
        diff = difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"recorded/{self.group}/{self.name}.json",
            tofile="current",
            lineterm="",
            n=2,
        )
        return "\n".join(list(diff)[:200])

    def check(self) -> None:
        """Compare against the snapshot, or write it in record mode."""

        if recording():
            path = snapshot_path(self.group, self.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(self.document()), encoding="utf-8")
            return
        difference = self.mismatch()
        if difference is not None:
            raise GoldenMismatch(difference)
