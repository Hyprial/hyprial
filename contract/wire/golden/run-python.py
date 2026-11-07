#!/usr/bin/env python3
"""Run every wire-golden case through the Python cross-module adapter."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_json(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def load_adapter() -> Callable[
    [dict[str, Any], dict[str, dict[str, Any]]], dict[str, Any]
]:
    try:
        from hyprial.daemon.impl.ipc.wire import evaluate_case
    except ImportError as error:
        raise RuntimeError(
            "Python wire adapter is not implemented: expected "
            "hyprial.contracts.wire.evaluate_case"
        ) from error
    return evaluate_case


def main() -> int:
    envelopes = load_json("envelopes.json")["envelopes"]
    cases = load_json("expectations.json")["cases"]
    by_id = {envelope["fixture_id"]: envelope for envelope in envelopes}
    try:
        evaluate_case = load_adapter()
    except RuntimeError as error:
        print(f"RESULT RED 0/{len(cases)} {error}")
        return 1

    failures: list[str] = []
    for case in cases:
        actual = evaluate_case(case, by_id)
        expected = case["expect"]
        if actual != expected:
            failures.append(case["id"])
            print(f"RED {case['id']} expected={expected!r} actual={actual!r}")
        else:
            print(f"GREEN {case['id']}")
    passed = len(cases) - len(failures)
    color = "GREEN" if not failures else "RED"
    print(f"RESULT {color} {passed}/{len(cases)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
