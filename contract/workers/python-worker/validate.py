from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).parent
MAX_LINE_BYTES = 8 * 1024 * 1024


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
    state = "starting"
    reserved: set[str] = set()
    terminal: set[str] = set()
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
            if not isinstance(request_id, str) or not request_id or request_id in reserved | terminal:
                raise ValueError("duplicate or invalid live id")
            if len(reserved) >= 10:
                raise ValueError("11th call")
            reserved.add(request_id)
            state = "accepting"
            continue
        if frame.get("op") == "stop":
            if state not in {"ready", "accepting"}:
                raise ValueError("stop outside accepting")
            state = "draining"
            continue
        if frame.get("type") in {"result", "error"}:
            request_id = frame.get("id")
            if not isinstance(request_id, str) or request_id not in reserved:
                raise ValueError("unknown or duplicate terminal")
            reserved.remove(request_id)
            terminal.add(request_id)
            continue
        raise ValueError("unknown frame")
    if state == "starting":
        raise ValueError("EOF before ready")
    if reserved:
        raise ValueError("EOF with active calls")


schema = json.loads((ROOT / "protocol.schema.json").read_text(encoding="utf-8"))
validator = Draft202012Validator(schema)
success = json.loads((ROOT / "fixtures" / "success.json").read_text(encoding="utf-8"))
negative = json.loads((ROOT / "fixtures" / "negative.json").read_text(encoding="utf-8"))

for label, frame in [("request", success["request"]), *[(f"response[{i}]", item) for i, item in enumerate(success["responses"])]]:
    errors = sorted(validator.iter_errors(frame), key=lambda error: error.path)
    if errors:
        raise SystemExit(f"{label}: {errors[0].message}")

for label, raw in negative["rawRejects"].items():
    if label == "boolAsInteger":
        if not list(validator.iter_errors(raw)):
            raise SystemExit("boolAsInteger was accepted")
        continue
    raw_text = "{" + ("x" * int(raw["bytes"])) + "}\n" if label == "oversized" else raw
    try:
        _strict(raw_text)
    except ValueError:
        continue
    raise SystemExit(f"raw negative accepted: {label}")

for label, frames in negative["stateRejects"].items():
    try:
        _state_trace(frames)
    except ValueError:
        continue
    raise SystemExit(f"state negative accepted: {label}")

print(
    f"PASS python-worker protocol v1 frames={1 + len(success['responses'])} "
    f"negative={len(negative['rawRejects']) + len(negative['stateRejects'])}"
)
