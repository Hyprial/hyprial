"""Helper wire protocol and spawn-path pins.

The ``.mjs`` writer and this parser are two files in two languages; the
static pin below fails if either side renames a field.  The bogus-provider
run is the one real end-to-end of the spawn path that needs no network:
``ModelRuntime.login`` rejects an unknown provider before any flow starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from hyprial.identity.impl.provider_auth import (
    DeviceLoginRunner,
    HelperOutcome,
    announcement_from,
    find_pi_package_root,
    parse_helper_line,
)
from hyprial.identity.impl.provider_auth.helper import HELPER_SCRIPT

DEVICE_CODE_LINE = json.dumps(
    {
        "type": "device_code",
        "userCode": "ABCD-EFGH",
        "verificationUri": "https://auth.example.com/device",
        "intervalSeconds": 5,
        "expiresInSeconds": 900,
    }
)


def test_parse_device_code_line() -> None:
    record = parse_helper_line(DEVICE_CODE_LINE)
    assert record is not None
    announcement = announcement_from(record)
    assert announcement is not None
    assert announcement.user_code == "ABCD-EFGH"
    assert announcement.verification_uri == "https://auth.example.com/device"
    assert announcement.expires_in_seconds == 900


def test_parse_ok_line() -> None:
    record = parse_helper_line('{"type": "ok"}')
    assert record is not None and record["type"] == "ok"
    assert announcement_from(record) is None


def test_malformed_lines_are_not_events() -> None:
    for line in ("", "not json", "[1,2]", '{"type":"mystery"}', '{"no_type":1}'):
        assert parse_helper_line(line) is None, line


def test_announcement_requires_both_human_fields() -> None:
    for record in (
        {"type": "device_code", "userCode": "", "verificationUri": "https://x"},
        {"type": "device_code", "userCode": "ABCD"},
        {"type": "device_code", "verificationUri": "https://x"},
    ):
        assert announcement_from(record) is None, record


def test_mjs_writer_keys_match_parser_contract() -> None:
    """The writer's field names are the parser's — pinned literally."""
    source = HELPER_SCRIPT.read_text("utf-8")
    for key in ("userCode", "verificationUri", "intervalSeconds", "expiresInSeconds"):
        assert key in source
    # Every stdout payload is a JSON.stringify literal; assert each one's
    # keys stay inside the announced set — this is what keeps the polling
    # secret (device_code / device_auth_id) and tokens off the wire: they
    # cannot leak through a channel whose payload keys are pinned.
    writes = re.findall(r"process\.stdout\.write\(\s*JSON\.stringify\(\{(.*?)\}\)", source, re.DOTALL)
    assert writes, "helper must announce over stdout"
    allowed = {"type", "userCode", "verificationUri", "intervalSeconds", "expiresInSeconds"}
    for body in writes:
        keys = set(re.findall(r"(\w+)\s*:", body))
        assert keys <= allowed, keys
        assert "device_code" in body or "ok" in body  # event type present


def test_find_pi_package_root_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "pi-coding-agent"
    (root / "dist" / "bundle").mkdir(parents=True)
    binary = root / "dist" / "bundle" / "cli.js"
    binary.write_text("// cli", "utf-8")
    (root / "dist" / "index.js").write_text("// index", "utf-8")
    link = tmp_path / "bin" / "pi"
    link.parent.mkdir()
    link.symlink_to(binary)
    assert find_pi_package_root((str(link),)) == root


def test_find_pi_package_root_rejects_non_pi(tmp_path: Path) -> None:
    bogus = tmp_path / "pi"
    bogus.write_text("#!/bin/sh\n", "utf-8")
    assert find_pi_package_root((str(bogus),)) is None
    assert find_pi_package_root(()) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_helper_rejects_unknown_provider_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("pi not installed on this machine")
    # The helper spawns the REAL pi ModelRuntime, which reads/writes the
    # agent auth.json.  Point it at a tmp dir so a test can never touch the
    # operator's real credential file (L3).
    ambient_root = tmp_path / "ambient-pi-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(ambient_root))
    monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
    agent_root = tmp_path / "pi-agent"
    environment = dict(os.environ)
    environment["PI_CODING_AGENT_DIR"] = str(agent_root)
    environment["HOME"] = str(tmp_path / "home")
    runner = DeviceLoginRunner(
        pi_command=(pi,), timeout_seconds=30.0, environment=environment
    )
    announced: list[object] = []
    outcome = runner("no-such-provider", announced.append)
    assert outcome is HelperOutcome.FAILED
    assert announced == []
    # The helper touched only the tmp agent dir (an empty auth.json may be
    # created); it must never have written a credential.
    auth_file = agent_root / "auth.json"
    if auth_file.exists():
        assert auth_file.read_text(encoding="utf-8").strip() in ("", "{}")
    assert not (ambient_root / "auth.json").exists()
