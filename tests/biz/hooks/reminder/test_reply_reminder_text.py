"""WP7 B8: the reminder text is exact for one id and bounded when truncated."""

from __future__ import annotations

from uuid import uuid4

from hyprial.biz.impl.hooks.reminder import reply_reminder_text

HINT = "；回复用 reply，无需回复用 ack"


def test_single_id_text_is_exact() -> None:
    assert reply_reminder_text(("d1",), max_bytes=512) == (
        "1 条请求未回复：d1" + HINT
    )


def test_truncated_text_stays_in_budget_keeps_hint_and_lists_a_prefix() -> None:
    ids = tuple(str(uuid4()) for _ in range(30))
    text = reply_reminder_text(ids, max_bytes=512)

    assert len(text.encode("utf-8")) <= 512
    # The action hint survives truncation (review A3).
    assert text.endswith(HINT)
    assert "（共 30 条）" in text
    listed_part = text.split("：", 1)[1].split(" 等（共 30 条）", 1)[0]
    listed = tuple(listed_part.split(", "))
    assert listed == ids[: len(listed)]
    assert len(listed) < len(ids)


def test_all_ids_are_kept_when_they_fit() -> None:
    ids = ("a" * 20, "b" * 20)
    text = reply_reminder_text(ids, max_bytes=512)
    assert text == "2 条请求未回复：" + ", ".join(ids) + HINT
    assert "（共" not in text
