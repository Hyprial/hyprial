"The packaged user-proxy worker, alone and under its real parent.\n\nRule (docs/design/design-user-proxy-harness.md §3): the person names a recipient with\na leading ``@``; everyone else is relayed to the person's DM route; nothing is\nguessed.  The parent turns a ``forward`` frame into a result the daemon SENDS\n(``forward_to``) -- tests/daemon/forwarding/seam/test_forward_seam.py covers what happens next.\n"

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker import (
    MISSING_RECIPIENT_HINT,
    decide,
    split_recipient,
)
from hyprial.daemon.impl.harnesses.python_worker import PythonHarnessProcess

ROUTE = "route:allen-feishu:allen-dm"
PERSON = "adapter:lark:allen-feishu"
PROXY = "agent:op:node-1:allen-proxy"


# --- the rule, as a pure function -------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("@alice hi", ("alice", "hi")),
        ("  @alice\n  lunch at noon", ("alice", "lunch at noon")),
        ("@route:x-feishu:team 你好\n第二行", ("route:x-feishu:team", "你好\n第二行")),
        ("@alice", None),
        ("@alice   ", None),
        ("@ hi", None),
        ("hi @alice", None),
        ("", None),
    ],
)
def test_split_recipient(message: str, expected: tuple[str, str] | None) -> None:
    assert split_recipient(message) == expected


def test_the_person_is_forwarded_to_the_named_recipient_without_the_marker() -> None:
    assert decide(PERSON, "@alice noon?", route=ROUTE) == {
        "type": "forward",
        "ok": True,
        "to": "alice",
        "message": "noon?",
    }


def test_the_person_without_a_recipient_is_told_how_not_guessed() -> None:
    assert decide(PERSON, "noon?", route=ROUTE) == {
        "type": "result",
        "ok": True,
        "output": {"message": MISSING_RECIPIENT_HINT},
    }


def test_a_malformed_markdown_recipient_returns_a_result_not_a_forward() -> None:
    captured = "agent:[x](mailto:y)[z](http://w)v"
    frame = decide(PERSON, f"@{captured} hi", route=ROUTE)

    assert frame["type"] == "result"
    message = frame["output"]["message"]
    assert captured in message
    assert "agent 名" in message
    assert "agent URI (agent:<owner>:<machine>:<actor>)" in message
    assert "route:<adapter>:<route>" in message


def test_a_markdown_wrapped_valid_recipient_is_unwrapped() -> None:
    frame = decide(PERSON, "@[alice](mailto:alice) hi", route=ROUTE)

    assert frame == {
        "type": "forward",
        "ok": True,
        "to": "alice",
        "message": "hi",
    }


def test_markdown_links_in_the_body_are_preserved_byte_for_byte() -> None:
    body = "看这个 [文档](https://example.com/x)"

    assert decide(PERSON, f"@alice {body}", route=ROUTE) == {
        "type": "forward",
        "ok": True,
        "to": "alice",
        "message": body,
    }


def test_everyone_else_is_relayed_to_the_person_unchanged() -> None:
    frame = decide("agent:op:node-1:alice", "@bob this is not for bob", route=ROUTE)
    assert frame == {
        "type": "forward",
        "ok": True,
        "to": ROUTE,
        "message": "@bob this is not for bob",
    }


def test_another_adapter_is_not_the_person() -> None:
    """Only THIS proxy's adapter speaks for the person."""

    frame = decide("adapter:lark:someone-else-feishu", "@alice hi", route=ROUTE)
    assert frame["to"] == ROUTE


# --- under the real parent ---------------------------------------------------


def _process(tmp_path: Path) -> PythonHarnessProcess:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "LANG", "PYTHONPATH"}
    }
    environment["HOME"] = str(tmp_path / "home")
    environment["HYPRIAL_SOURCE_COMMIT"] = "3137ed2ad92e5bb2c4369661d660815b63ae787b"
    environment["HARNESS_STATE_DIR"] = str(tmp_path / "state")
    spec = HarnessLaunchSpec(
        "user-proxy",
        "allen-proxy",
        True,
        cwd=str(Path.cwd()),
        command=(
            sys.executable,
            "-m",
            "hyprial.daemon.impl.harnesses.python_worker._user_proxy_worker",
            "--kind",
            "user-proxy",
            "--route",
            ROUTE,
        ),
    )
    return PythonHarnessProcess(spec, env=environment)


def _one_result(process: PythonHarnessProcess) -> HarnessResult:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        results = process.drain_results()
        if results:
            assert len(results) == 1
            return results[0]
        time.sleep(0.02)
    raise AssertionError(f"no result; last_error={process.last_error}")


def test_parent_starts_the_packaged_worker_sequentially(tmp_path: Path) -> None:
    process = _process(tmp_path)
    try:
        assert process.running
        assert process.startup is not None
        assert process.startup["kind"] == "user-proxy"
        assert process.max_in_flight == 1
    finally:
        process.stop()


def test_a_message_for_the_person_becomes_a_forward_to_their_route(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path)
    try:
        assert process.enqueue(
            HarnessDelivery("d-1", "c-1", "agent:op:node-1:alice", PROXY, "done?")
        )
        result = _one_result(process)
    finally:
        process.stop()
    assert result == HarnessResult(
        "d-1", PROXY, HarnessResultStatus.COMPLETED, output="done?", forward_to=ROUTE
    )


def test_the_persons_reply_becomes_a_forward_to_whom_they_named(tmp_path: Path) -> None:
    process = _process(tmp_path)
    try:
        assert process.enqueue(HarnessDelivery("d-2", "c-2", PERSON, PROXY, "@alice yes"))
        result = _one_result(process)
    finally:
        process.stop()
    assert result.forward_to == "alice"
    assert result.output == "yes"


def test_the_hint_reaches_the_person_as_plain_text(tmp_path: Path) -> None:
    process = _process(tmp_path)
    try:
        assert process.enqueue(HarnessDelivery("d-3", "c-3", PERSON, PROXY, "yes"))
        result = _one_result(process)
    finally:
        process.stop()
    assert result.forward_to is None
    assert result.status is HarnessResultStatus.COMPLETED
    assert result.output == MISSING_RECIPIENT_HINT


def test_a_forward_frame_from_a_jev_worker_is_a_protocol_failure(tmp_path: Path) -> None:
    """Only user-proxy may ask the daemon to send; jev can only reply."""

    from tests.daemon.harnesses.python.test_python_harness_process import _process as jev_process

    process = jev_process(tmp_path)
    try:
        writer_seen: list[object] = []
        process._write_frame = writer_seen.append  # noqa: SLF001 - keep the child idle
        assert process.enqueue(HarnessDelivery("j-1", "c", "s", "route-chooser", "{}"))
        process._handle_frame(  # noqa: SLF001 - inject the child's line
            {"v": 1, "type": "forward", "id": "j-1", "ok": True, "to": "x", "message": "m"}
        )
        result = _one_result(process)
    finally:
        process.stop()
    assert result.status is HarnessResultStatus.FAILED
    assert result.forward_to is None
    assert result.failure_code == "HARNESS_TRANSIENT_FAILURE"


def test_a_ready_frame_from_another_script_is_refused(tmp_path: Path) -> None:
    """scriptSha256 pins the packaged relay: a different file must not start."""

    fake = tmp_path / "fake_proxy.py"
    fake.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'v':1,'type':'ready','kind':'user-proxy','pid':os.getpid(),"
        "'hyprialCommit':'3137ed2ad92e5bb2c4369661d660815b63ae787b','scriptSha256':'0'*64}), flush=True)\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    spec = HarnessLaunchSpec(
        "user-proxy", "allen-proxy", True, cwd=str(tmp_path), command=(sys.executable, str(fake))
    )
    from hyprial.daemon.impl.harnesses.protocol.common import HarnessStartError

    with pytest.raises(HarnessStartError, match="script hash"):
        PythonHarnessProcess(spec, env={"PATH": os.environ.get("PATH", "")})
