"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def run_headless_fifo_phase(_entry: dict[str, Any], /, daemon, root, case, report):
    """Headless busy FIFO (the E2E-008 cc TUI check's managed counterpart).

    While the worker is mid-turn on a deliberately slow first message (a
    shell ``touch <sentinel> && sleep`` witnesses the turn being in
    progress), a second message is delivered.  Both must be answered
    exactly once, in delivery order, with no loss — pending order is the
    daemon's ``arrival_id`` FIFO, the same order contract E2E-002 pins.
    """

    harness = case.harness
    busy_check, once_check, order_check = _entry['fifo_checks'](harness)
    nonce = f"N{_entry['time'].time_ns()}"
    conversation = f"e2e013-fifo-{harness}-{nonce}"
    first_marker = f"E2E013_FIFO_{harness.upper()}_FIRST_{nonce}"
    second_marker = f"E2E013_FIFO_{harness.upper()}_SECOND_{nonce}"
    sentinel = root / f"fifo-busy-{harness}-{nonce}"
    sent_ids: list[str] = []

    def send(prompt: str) -> None:
        sent = daemon.command(
            "message",
            "send",
            "--from",
            _entry['DRIVER'],
            "--to",
            case.nickname,
            "--conversation",
            conversation,
            "--summary",
            prompt,
            prompt,
            "--json",
        )
        if sent.get("ok") is not True or not isinstance(sent.get("messageId"), str):
            raise AssertionError(f"send was not accepted: {sent}")
        sent_ids.append(sent["messageId"])

    def replies() -> list[dict[str, Any]]:
        return [
            item
            for item in _entry['pending_for'](root, _entry['DRIVER'])
            if item.get("conversationId") == conversation
            and item.get("messageId") not in sent_ids
        ]

    try:
        send(
            f"Harness E2E-013 FIFO probe 1 {nonce}. First use your shell "
            f"tool to run exactly `touch {sentinel} && sleep 6`, then reply "
            f"with exactly {first_marker} and nothing else."
        )
        daemon.wait(
            lambda: sentinel.is_file(),
            lambda found: found is True,
            timeout=_entry['REPLY_TIMEOUT'],
        )
        if replies():
            raise AssertionError(
                "the first reply arrived before the busy sentinel: there is "
                "no mid-turn window to deliver the second message into"
            )
        send(
            f"Harness E2E-013 FIFO probe 2 {nonce}. Reply with exactly "
            f"{second_marker} and nothing else."
        )
        if replies():
            raise AssertionError(
                "the first reply arrived before the second send completed: "
                "mid-turn delivery is not established"
            )
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", busy_check, str(error))
        report.block(
            (once_check, order_check), "busy overlap was not established"
        )
        return
    report.add(
        "PASS",
        busy_check,
        "second message delivered while the first turn was mid-sleep "
        f"(sentinel {sentinel.name} present, no first reply yet)",
    )

    try:
        collected = daemon.wait(
            replies, lambda found: len(found) >= 2, timeout=_entry['REPLY_TIMEOUT'] * 2
        )
    except (AssertionError, OSError) as error:
        report.add(
            "FAIL", once_check, f"both replies never arrived (loss): {error}"
        )
        report.add("BLOCKED", order_check, "fewer than two replies arrived")
        return

    texts = [str(item.get("summary") or "") for item in collected]
    first_hits = [index for index, text in enumerate(texts) if first_marker in text]
    second_hits = [
        index for index, text in enumerate(texts) if second_marker in text
    ]
    problems: list[str] = []
    if len(collected) != 2:
        problems.append(f"{len(collected)} replies arrived for two probes")
    if len(first_hits) != 1:
        problems.append(f"first marker appears in {len(first_hits)} replies")
    if len(second_hits) != 1:
        problems.append(f"second marker appears in {len(second_hits)} replies")
    try:
        for item in collected:
            ack = daemon.command(
                "message", "ack", "--from", _entry['DRIVER'], str(item["messageId"]), "--json"
            )
            if ack.get("acknowledged") is not True:
                problems.append(f"acknowledgement failed: {ack}")
        # A duplicated turn output would surface as a NEW pending message
        # after the two known replies are acknowledged; hold a grace window.
        _entry['time'].sleep(3.0)
        extras = replies()
        if extras:
            problems.append(
                f"{len(extras)} extra replies appeared after ack "
                "(a probe was answered more than once)"
            )
        if problems:
            raise AssertionError("; ".join(problems))
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", once_check, str(error))
    else:
        report.add(
            "PASS",
            once_check,
            "exactly two replies, each probe answered exactly once, none "
            "lost, no extras after ack",
        )

    if len(first_hits) == 1 and len(second_hits) == 1:
        if first_hits[0] < second_hits[0]:
            report.add(
                "PASS",
                order_check,
                "reply order matches delivery order "
                f"(first at index {first_hits[0]}, second at {second_hits[0]} "
                "of the arrival-ordered pending list)",
            )
        else:
            report.add(
                "FAIL",
                order_check,
                "replies are out of delivery order "
                f"(first at index {first_hits[0]}, second at {second_hits[0]})",
            )
    else:
        report.add(
            "BLOCKED", order_check, "marker attribution failed; order unknowable"
        )
