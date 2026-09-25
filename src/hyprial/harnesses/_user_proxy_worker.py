"""Packaged user-proxy worker for the ``python_worker`` harness mechanism.

One per person (docs/design-user-proxy-harness.md).  It relays; it never
answers.  For every delivery it decides a recipient and hands the text back in
a ``forward`` frame -- the parent daemon performs the send under this agent's
identity and settles the inbox row.  This module holds no credential, calls no
service, and never acknowledges, retries, or discards an inbox row.

The rule (§3), a pure function of the delivery and ``--route``:

* from this proxy's own adapter (the person, in their DM) -- the message must
  start with ``@<recipient>``; it is forwarded to that recipient without the
  marker.  No marker, or nothing after it -> a plain ``result`` back to the
  person explaining the format.  Never a guess.
* from anyone else -- forwarded to the person's DM route unchanged; the daemon
  attributes the post to the original sender.

Deliberately independent of ``_python_worker``: the ready frame's
``scriptSha256`` then covers every line of the forwarding rule.  Address
grammar comes from the leaf ``hyprial.uri`` module, the one reader for it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from hyprial.uri import ADAPTER_URI_PREFIX, parse_route_uri

PROTOCOL_VERSION = 1
KIND = "user-proxy"
MAX_LINE_BYTES = 8 * 1024 * 1024
RECIPIENT_MARKER = "@"
_ADDRESSED = re.compile(r"\s*@(\S+)\s+(\S.*)", re.DOTALL)
_HEX40 = set("0123456789abcdef")

#: Sent back to the person when they did not say who the message is for.  The
#: cost Allen accepted on 2026-09-23: replies must name their recipient.
MISSING_RECIPIENT_HINT = (
    "没有转发:请在开头写明收件人,例如「@alice 午饭十二点」。"
    "收件人可以是 agent 名、agent URI 或 route:<adapter>:<route>。"
)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def parse_route(value: str) -> tuple[str, str]:
    """``route:<adapter>:<route>`` -> ``(adapter, route)``; ValueError otherwise."""

    parsed = parse_route_uri(value)
    if parsed is None:
        raise ValueError("--route must be route:<adapter>:<route>")
    return parsed


def person_address(adapter: str) -> str:
    """The sender every message the person writes through ``adapter`` carries.

    Built, not parsed: it is the identity the Lark adapter worker stamps on its
    inbound messages (``adapters/lark/worker.py``: ``channel_actor_id``).  The
    legacy ``channel:lark:`` spelling is never minted for new messages, so a
    proxy started today never sees it from the person.
    """

    return f"{ADAPTER_URI_PREFIX}lark:{adapter}"


def split_recipient(message: str) -> tuple[str, str] | None:
    """``"@alice  hi"`` -> ``("alice", "hi")``; ``None`` when either half is missing.

    Any whitespace (a newline included) ends the recipient.
    """

    match = _ADDRESSED.match(message)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def decide(sender: str, message: str, *, route: str) -> dict[str, object]:
    """The frame fields (minus envelope) for one delivery."""

    adapter, _ = parse_route(route)
    if sender == person_address(adapter):
        split = split_recipient(message)
        if split is None:
            return {"type": "result", "ok": True, "output": {"message": MISSING_RECIPIENT_HINT}}
        recipient, body = split
        return {"type": "forward", "ok": True, "to": recipient, "message": body}
    return {"type": "forward", "ok": True, "to": route, "message": message}


def _resolve_commit() -> str | None:
    candidates: list[object] = [os.environ.get("HYPRIAL_SOURCE_COMMIT")]
    try:
        from hyprial import updates

        candidates.append(updates.read_installation("hyprial").commit)
    except Exception:  # noqa: BLE001 - optional installation metadata
        pass
    package_root = Path(__file__).resolve()
    for parent in (package_root, *package_root.parents):
        if (parent / ".git").exists():
            try:
                candidates.append(
                    subprocess.check_output(
                        ["git", "-C", str(parent), "rev-parse", "HEAD"],
                        text=True,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                    ).strip()
                )
            except (OSError, subprocess.SubprocessError):
                pass
            break
    for value in candidates:
        if isinstance(value, str) and len(value) == 40 and set(value) <= _HEX40:
            return value
    return None


def _emit(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _metric(request_id: str, segment: str, started: float, ok: bool) -> None:
    _emit(
        {
            "v": PROTOCOL_VERSION,
            "type": "metric",
            "id": request_id,
            "segment": segment,
            "durationMs": max(0, int((time.monotonic() - started) * 1000)),
            "ok": ok,
        }
    )


def _decode_call(line: bytes) -> tuple[str, str, str] | None:
    """Return ``(id, from, message)`` for a call; ``None`` for stop.  Raises on anything else."""

    frame = json.loads(
        line.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite,
    )
    if not isinstance(frame, dict) or frame.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol frame")
    if frame.get("op") == "stop":
        if set(frame) != {"v", "op"}:
            raise ValueError("stop has unknown fields")
        return None
    if frame.get("op") != "call" or set(frame) != {"v", "op", "id", "payload"}:
        raise ValueError("unsupported protocol frame")
    request_id = frame["id"]
    payload = frame["payload"]
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("call id must be non-empty")
    # ``to`` names which actor was addressed (every delivery now carries both
    # ends); the relay needs only the sender, so it is checked, not used.
    # The two-key form stays accepted so a parent from before ``to`` existed
    # still drives this child.
    if (
        not isinstance(payload, dict)
        or set(payload) not in ({"from", "message"}, {"from", "to", "message"})
        or not isinstance(payload["from"], str)
        or not payload["from"]
        or not isinstance(payload["message"], str)
        or ("to" in payload and not isinstance(payload["to"], str))
    ):
        raise ValueError("call payload must be {from, to?, message}")
    return request_id, payload["from"], payload["message"]


def run(route: str) -> int:
    try:
        parse_route(route)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    commit = _resolve_commit()
    if commit is None:
        print("Hyprial commit is unavailable", file=sys.stderr)
        return 1
    _emit(
        {
            "v": PROTOCOL_VERSION,
            "type": "ready",
            "kind": KIND,
            "pid": os.getpid(),
            "hyprialCommit": commit,
            "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
    )
    seen: set[str] = set()
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n") or line.endswith(b"\r\n"):
            print("protocol frame is too large or not LF-delimited", file=sys.stderr)
            return 1
        started = time.monotonic()
        try:
            call = _decode_call(line)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            print(f"protocol frame rejected: {type(error).__name__}", file=sys.stderr)
            return 1
        if call is None:
            return 0
        request_id, sender, message = call
        if request_id in seen:
            print("call id is already live", file=sys.stderr)
            return 1
        seen.add(request_id)
        _metric(request_id, "decode", started, True)
        started = time.monotonic()
        frame = decide(sender, message, route=route)
        _metric(request_id, "call", started, True)
        _emit({"v": PROTOCOL_VERSION, "id": request_id, **frame})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--kind")
    parser.add_argument("--route")
    args, unknown = parser.parse_known_args(argv)
    if unknown or args.kind != KIND or args.route is None:
        print(
            "usage: python -m hyprial.harnesses._user_proxy_worker "
            "--kind user-proxy --route route:<adapter>:<route>",
            file=sys.stderr,
        )
        return 2
    return run(args.route)


if __name__ == "__main__":
    raise SystemExit(main())
