from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from hyprial.daemon.impl.adapters.lark.credentials.scopes import LarkScopeThrottleStore, LarkScopeRecovery

from ..support import (
    _claim_throttle_after_corruption,
    _claim_throttle_once,
)


def test_throttle_is_shared_per_app_across_instances_and_restart(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    path = tmp_path / "scope.json"
    calls: list[str] = []

    def recovery(scope: str) -> LarkScopeRecovery:
        return LarkScopeRecovery(
            app_id="same-app",
            apply=lambda: calls.append(scope) or {"code": 0, "msg": "ok"},
            throttle=LarkScopeThrottleStore(path, now=lambda: now[0]),
        )

    first = recovery("first").handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )
    second = recovery("second").handle(
        SimpleNamespace(
            missing_scopes=("im:message.group_msg",), authorization_url=None
        )
    )
    assert first is not None and first["status"] == "requested"
    assert second == {
        "handled": True,
        "status": "throttled",
        "scopes": ["im:message.group_msg"],
    }
    assert calls == ["first"]
    assert path.stat().st_mode & 0o777 == 0o600
    now[0] += 3600
    assert recovery("after-restart").handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )["status"] == "requested"
    assert calls == ["first", "after-restart"]


def test_throttle_claim_is_atomic_across_processes(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    path = tmp_path / "scope.json"
    start = context.Event()
    output = context.Queue()

    processes = [
        context.Process(target=_claim_throttle_once, args=(path, start, output))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        values = [output.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert sorted(values) == [False, True]
    finally:
        # A multiprocessing.Queue owns a pipe pair plus a feeder thread;
        # neither is released until close()+join_thread() (card #77).
        output.close()
        output.join_thread()


def test_throttle_quarantines_malformed_and_unsafe_timestamps(tmp_path: Path) -> None:
    import math

    now = 10_000.0
    path = tmp_path / "scope.json"
    store = LarkScopeThrottleStore(path, now=lambda: now)
    malformed = (
        b"{truncated",
        b"[]",
        b'{"version":1,"apps":{"key":{"attemptedAt":NaN}}}',
        json.dumps(
            {"version": 1, "apps": {"key": {"attemptedAt": math.inf}}}
        ).encode(),
    )
    for index, payload in enumerate(malformed):
        path.write_bytes(payload)
        assert store.claim("app", ("im:message",), 3600) is True
        quarantined = list(tmp_path.glob("scope.json.corrupt.*"))
        assert len(quarantined) == index + 1
        assert quarantined[-1].stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["version"] == 1

    future_key = __import__("hashlib").sha256(b"app").hexdigest()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "apps": {
                    future_key: {"attemptedAt": now + 86_400, "status": "pending"}
                },
            }
        )
    )
    assert store.claim("app", ("im:message",), 3600) is True


def test_clock_skew_boundary_is_bounded(tmp_path: Path) -> None:
    import hashlib

    now = 10_000.0
    path = tmp_path / "scope.json"
    key = hashlib.sha256(b"app").hexdigest()
    store = LarkScopeThrottleStore(path, now=lambda: now)
    for future, expected in ((now + 300, False), (now + 301, True)):
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "apps": {key: {"attemptedAt": future, "status": "pending"}},
                }
            )
        )
        assert store.claim("app", ("im:message",), 3600) is expected


def test_throttle_repeated_corruption_recovery_is_process_safe(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    path = tmp_path / "scope.json"
    path.write_text("not-json")
    start = context.Event()
    output = context.Queue()

    processes = [
        context.Process(
            target=_claim_throttle_after_corruption,
            args=(path, start, output),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    values = [output.get(timeout=5) for _ in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert sorted(values) == [("ok", False), ("ok", True)]
    assert len(list(tmp_path.glob("scope.json.corrupt.*"))) == 1


def test_persistence_failures_warn_without_hiding_apply_or_notification(
    tmp_path: Path,
) -> None:
    notifications: list[str] = []
    apply_calls: list[str] = []

    class ClaimFailure:
        def claim(self, *_args):  # type: ignore[no-untyped-def]
            raise OSError("path contains token=secret")

        def record(self, *_args):  # type: ignore[no-untyped-def]
            raise AssertionError("record should not run without a durable lease")

    recovery = LarkScopeRecovery(
        app_id="app",
        apply=lambda: apply_calls.append("apply") or {"code": 0, "msg": "ok"},
        notify=notifications.append,
        throttle=ClaimFailure(),  # type: ignore[arg-type]
    )
    result = recovery.handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )
    assert result is not None and result["status"] == "requested"
    assert result["persistenceWarning"] == "claim:OSError"
    assert result["duplicateApplyRisk"] is True
    assert apply_calls == ["apply"] and len(notifications) == 1
    assert "secret" not in json.dumps(result)

    class RecordFailure:
        def claim(self, *_args):  # type: ignore[no-untyped-def]
            return True

        def record(self, *_args):  # type: ignore[no-untyped-def]
            raise OSError("token=secret")

    notifications.clear()
    recovery = LarkScopeRecovery(
        app_id="app",
        apply=lambda: {"code": 0, "msg": "ok"},
        notify=notifications.append,
        throttle=RecordFailure(),  # type: ignore[arg-type]
    )
    result = recovery.handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )
    assert result is not None and result["status"] == "requested"
    assert result["persistenceWarning"] == "record:OSError"
    assert result["duplicateApplyRisk"] is True
    assert len(notifications) == 1
