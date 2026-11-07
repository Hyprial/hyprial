"""Validate the user-proxy protocol v1 corpus, then the packaged worker against it.

Three layers, each able to fail on its own:

1. every fixture frame is valid under ``protocol.schema.json`` and every
   ``schemaRejects`` frame is not;
2. the raw framing and the state table reject every negative;
3. the REAL packaged worker, fed the fixture requests, emits exactly the
   fixture responses (only ``durationMs`` and the hint text are normalized) --
   so the corpus cannot drift from the implementation it describes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent
MAX_LINE_BYTES = 8 * 1024 * 1024
TERMINAL_TYPES = {"result", "forward", "error"}
COMMIT = "3137ed2ad92e5bb2c4369661d660815b63ae787b"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite number: {value}")


def _strict(raw: str) -> object:
    encoded = raw.encode("utf-8")
    if b"\r\n" in encoded:
        raise ValueError("CRLF")
    if len(encoded) > MAX_LINE_BYTES or not encoded.endswith(b"\n"):
        raise ValueError("line framing")
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def _state_trace(frames: list[dict[str, object]]) -> None:
    """Sequential, concurrency 1: at most one live call (design §7)."""

    state = "starting"
    live: str | None = None
    seen: set[str] = set()
    for frame in frames:
        if frame.get("type") == "ready":
            if state != "starting":
                raise ValueError("duplicate ready")
            state = "ready"
            continue
        if frame.get("op") == "call":
            if state not in {"ready", "accepting"}:
                raise ValueError("call outside accepting")
            request_id = frame.get("id")
            if not isinstance(request_id, str) or not request_id or request_id in seen:
                raise ValueError("duplicate or invalid id")
            if live is not None:
                raise ValueError("second call while one is live")
            live = request_id
            seen.add(request_id)
            state = "accepting"
            continue
        if frame.get("op") == "stop":
            if state not in {"ready", "accepting"}:
                raise ValueError("stop outside accepting")
            state = "draining"
            continue
        if frame.get("type") == "metric":
            continue
        if frame.get("type") in TERMINAL_TYPES:
            if frame.get("id") != live or live is None:
                raise ValueError("unknown or duplicate terminal")
            live = None
            continue
        raise ValueError("unknown frame")
    if state == "starting":
        raise ValueError("EOF before ready")
    if live is not None:
        raise ValueError("EOF with a live call")


def _run_packaged_worker(route: str, requests: list[dict[str, object]]) -> list[dict[str, object]]:
    environment = {name: value for name, value in os.environ.items() if name in {"PATH", "PYTHONPATH", "LANG"}}
    environment["HYPRIAL_SOURCE_COMMIT"] = COMMIT
    stdin = "".join(json.dumps(request) + "\n" for request in requests) + json.dumps({"v": 1, "op": "stop"}) + "\n"
    completed = subprocess.run(
        [sys.executable, "-m", "hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker", "--kind", "user-proxy", "--route", route],
        input=stdin.encode("utf-8"),
        capture_output=True,
        env=environment,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"packaged worker exited {completed.returncode}: {completed.stderr.decode()[-500:]}")
    return [_strict(line + "\n") for line in completed.stdout.decode("utf-8").splitlines()]  # type: ignore[misc]


def _normalized(frame: dict[str, object]) -> dict[str, object]:
    copy = dict(frame)
    if "durationMs" in copy:
        copy["durationMs"] = 0
    output = copy.get("output")
    if copy.get("type") == "result" and isinstance(output, dict) and "message" in output:
        copy["output"] = {"message": "HINT"}
    return copy


schema = json.loads((ROOT / "protocol.schema.json").read_text(encoding="utf-8"))
validator = Draft202012Validator(schema)
success = json.loads((ROOT / "fixtures" / "success.json").read_text(encoding="utf-8"))
negative = json.loads((ROOT / "fixtures" / "negative.json").read_text(encoding="utf-8"))

frames: list[tuple[str, object]] = [("ready", success["ready"])]
for exchange in success["exchanges"]:
    frames.append((f"{exchange['label']}: request", exchange["request"]))
    frames.extend((f"{exchange['label']}: response[{i}]", item) for i, item in enumerate(exchange["responses"]))
for label, frame in frames:
    errors = sorted(validator.iter_errors(frame), key=lambda error: list(error.path))
    if errors:
        raise SystemExit(f"{label}: {errors[0].message}")

for label, frame in negative["schemaRejects"].items():
    if not list(validator.iter_errors(frame)):
        raise SystemExit(f"schema negative accepted: {label}")

for label, raw in negative["rawRejects"].items():
    try:
        _strict(raw)
    except ValueError:
        continue
    raise SystemExit(f"raw negative accepted: {label}")

for label, trace in negative["stateRejects"].items():
    try:
        _state_trace(trace)
    except ValueError:
        continue
    raise SystemExit(f"state negative accepted: {label}")

trace = [success["ready"]]
for exchange in success["exchanges"]:
    trace.append(exchange["request"])
    trace.extend(exchange["responses"])
trace.append({"v": 1, "op": "stop"})
_state_trace(trace)

emitted = _run_packaged_worker(success["route"], [exchange["request"] for exchange in success["exchanges"]])
ready, responses = emitted[0], emitted[1:]
if set(ready) != set(success["ready"]) or ready.get("kind") != "user-proxy" or ready.get("hyprialCommit") != COMMIT:
    raise SystemExit(f"packaged worker ready frame differs: {ready}")
if list(validator.iter_errors(ready)):
    raise SystemExit("packaged worker ready frame fails the schema")
expected = [item for exchange in success["exchanges"] for item in exchange["responses"]]
if [_normalized(frame) for frame in responses] != [_normalized(frame) for frame in expected]:
    raise SystemExit(
        "packaged worker drifted from the fixture:\n"
        + json.dumps(responses, ensure_ascii=False, indent=1)
    )

negatives = len(negative["schemaRejects"]) + len(negative["rawRejects"]) + len(negative["stateRejects"])
print(f"PASS user-proxy protocol v1 frames={len(frames)} negative={negatives} packaged-worker=conforms")
