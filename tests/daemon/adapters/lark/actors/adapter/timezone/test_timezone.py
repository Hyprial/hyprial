from __future__ import annotations

import os
import time
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from hyprial.daemon.impl.adapters.lark import ActorTarget, LarkStateStore
from hyprial.daemon.impl.adapters.lark.inbound.commands import _local_timezone, _time_in, _timezone
from hyprial.daemon.impl.adapters.lark.inbound import commands as lark_adapter_commands

from ..support import (
    actor as actor,
    make_adapter,
    sdk_event,
)


def test_timezone_commands_are_dm_only_persistent_and_consumed(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_tz_set",
        message_id="om_tz_set",
        text="/hyprial set timezone=Asia/Taipei",
    )
    assert "Timezone set" in (lark.last_reply_text or "")
    assert "Asia/Taipei" in (lark.last_reply_text or "")

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_tz_get",
        message_id="om_tz_get",
        text="/hyprial set timezone",
    )
    assert "Asia/Taipei" in (lark.last_reply_text or "")

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_tz_local",
        message_id="om_tz_local",
        text="/hyprial set timezone=local",
    )
    assert "machine default" in (lark.last_reply_text or "")
    assert harness.requests == []


def test_fixed_offset_timezone_roundtrips_through_conversation_state(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_fixed_tz_set",
        message_id="om_fixed_tz_set",
        text="/hyprial set timezone=UTC+08:30",
    )
    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_fixed_tz_get",
        message_id="om_fixed_tz_get",
        text="/hyprial set timezone",
    )

    assert harness.requests == []
    assert "UTC+08:30 (DM override)" in (lark.last_reply_text or "")
    assert "+0830" in (lark.last_reply_text or "")


def test_machine_timezone_uses_tz_environment_and_fixed_offset_is_persistable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()
    try:
        assert _local_timezone() == "Asia/Taipei"
        assert "+0800" in _time_in("Asia/Taipei")
        assert "+0830" in _time_in("UTC+08:30")
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_machine_timezone_serializes_a_non_utc_fixed_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TZ", raising=False)
    fixed = timezone(timedelta(hours=5, minutes=45))

    class FakeDatetime:
        @staticmethod
        def now():  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                astimezone=lambda: SimpleNamespace(tzinfo=fixed)
            )

    monkeypatch.setattr(lark_adapter_commands, "datetime", FakeDatetime)

    persisted = _local_timezone()

    assert persisted == "UTC+05:45"
    assert _timezone(persisted).utcoffset(None) == timedelta(
        hours=5, minutes=45
    )


@pytest.mark.parametrize(
    # Specs, not instances: a raised exception instance retains its
    # __traceback__, and pytest keeps parametrize values on the item's
    # callspec for the whole session -- instances here would pin every frame
    # the raise passed through, adapter and sqlite fds included (card #77).
    "error_spec",
    (
        (ValueError, "bad path"),
        (OverflowError, "too large"),
        (OSError, "unavailable tzdata"),
    ),
)
def test_invalid_timezone_library_errors_fail_locally(
    tmp_path: Path,
    actor: ActorTarget,
    monkeypatch: pytest.MonkeyPatch,
    error_spec: tuple[type[Exception], str],
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    def invalid_zone(_name: str):  # type: ignore[no-untyped-def]
        raise error_spec[0](error_spec[1])

    monkeypatch.setattr(lark_adapter_commands, "ZoneInfo", invalid_zone)
    sdk_event(
        adapter.handle_sdk_event,
        event_id=f"evt_bad_tz_{error_spec[0].__name__}",
        message_id=f"om_bad_tz_{error_spec[0].__name__}",
        text="/hyprial set timezone=Invalid/Zone",
    )

    assert harness.requests == []
    assert "Invalid timezone" in (lark.last_reply_text or "")


def test_timezone_group_command_is_rejected_without_state_change(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_tz_group",
        message_id="om_tz_group",
        chat_id="oc_group",
        chat_type="group",
        text="/hyprial set timezone=Asia/Taipei",
    )

    assert "direct message" in (lark.last_reply_text or "")
    assert LarkStateStore(tmp_path / "lark-state.sqlite3").conversation_timezone(
        "lark:oc_group:oc_group"
    ) is None
    assert harness.requests == []
