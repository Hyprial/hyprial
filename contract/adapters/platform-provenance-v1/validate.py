#!/usr/bin/env python3
"""Validate the 13 literal provenance arms, optionally selecting one platform."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
FILES = {
    "lark": "lark.jsonl",
    "weixin": "weixin.jsonl",
    "dingtalk": "dingtalk.jsonl",
    "wecom-bot": "wecom-bot.jsonl",
    "wecom-app": "wecom-app.jsonl",
    "qq": "qq.jsonl",
    "slack": "slack.jsonl",
    "telegram": "telegram.jsonl",
    "discord": "discord.jsonl",
    "whatsapp": "whatsapp.jsonl",
    "imessage": "imessage.jsonl",
    "matrix": "matrix.jsonl",
    "mail": "mail.jsonl",
}
SECRET_KEY = re.compile(r"(?:secret|token|password|private.?key|credential)", re.IGNORECASE)


def _reject_secrets(value: object, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if SECRET_KEY.search(key):
                raise SystemExit(f"secret-shaped key forbidden at {location}.{key}")
            _reject_secrets(nested, f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_secrets(nested, f"{location}[{index}]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=tuple(FILES))
    args = parser.parse_args()
    selected = {args.platform: FILES[args.platform]} if args.platform else FILES
    count = 0
    for arm, name in selected.items():
        rows = HERE.joinpath(name).read_text(encoding="utf-8").splitlines()
        if len(rows) != 1:
            raise SystemExit(f"{name} must contain exactly one golden row")
        row = json.loads(rows[0])
        if set(row) != {"schemaVersion", "platform", "variant", "conversation", "sender", "native"}:
            raise SystemExit(f"unknown or missing schema field in {name}")
        expected_platform = arm.split("-", 1)[0]
        if row["schemaVersion"] != 1 or row["platform"] != expected_platform:
            raise SystemExit(f"platform discriminator mismatch in {name}")
        for field in ("conversation", "sender", "native"):
            if not isinstance(row[field], dict) or not row[field]:
                raise SystemExit(f"{field} must be a bounded non-empty object in {name}")
        encoded = json.dumps(row, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 4096:
            raise SystemExit(f"row exceeds 4096 bytes in {name}")
        _reject_secrets(row)
        count += 1
    print(f"RESULT PASS platform-provenance-v1 arms={count} selection={args.platform or 'all'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
