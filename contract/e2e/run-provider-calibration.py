#!/usr/bin/env python3
"""Ask the real provider and the fake the same thing, and report the difference.

Not a pass/fail check. Run as one it would mostly report whether the vendor
was reachable today, which is the vendor's business. What we need to know is
whether our fake still resembles them, and only a field-by-field comparison
answers that.

Exit status is about whether the comparison *ran*, never about what it found:
a drift is a finding for a person to read, not a broken build. The one thing
that does fail is being unable to compare at all -- a calibration that cannot
reach the real side must say so rather than reporting no drift.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _calibration import (  # noqa: E402
    compare_contract_versions,
    compare_payloads,
    render_report,
)
from _deepseek import DeepSeekUnavailable, deepseek_api_key  # noqa: E402
from _fake_provider import CONTRACT_VERSIONS, FakeProvider  # noqa: E402

REAL_OPENAI_URL = "https://api.deepseek.com/v1/chat/completions"
PROMPT = {
    "model": "deepseek-flash",
    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    "stream": False,
    "max_tokens": 16,
}


def _post(url: str, payload: dict, *, key: str | None) -> dict:
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def main() -> int:
    try:
        key = deepseek_api_key()
    except DeepSeekUnavailable as error:
        # Not "no drift": we could not look.  Saying the reassuring thing when
        # the check itself failed is the defect this whole line guards against.
        print(f"CALIBRATION DID NOT RUN: {error}", file=sys.stderr)
        return 1

    try:
        real = _post(REAL_OPENAI_URL, PROMPT, key=key)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"CALIBRATION DID NOT RUN: real provider unreachable: {error}",
              file=sys.stderr)
        return 1

    with FakeProvider(reply="ok") as provider:
        mock = _post(
            f"{provider.openai_base_url}/chat/completions", PROMPT, key=None
        )

    # Version first: one changed version explains any number of field
    # differences, and reporting the fields first would bury it.
    live_versions = {
        "openai.chat.completions": str(real.get("object", "unknown")),
    }
    recorded = {"openai.chat.completions": CONTRACT_VERSIONS[
        "openai.chat.completions"
    ]}

    version_drifts = compare_contract_versions(recorded, live_versions)
    payload_drifts = compare_payloads(real, mock)
    print(render_report(version_drifts, payload_drifts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
