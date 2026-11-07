"""Review 1104 publication retry follow-ups."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.identity import bindings as binding_module
from hyprial.daemon.impl.identity.bindings import OrgBindingCacheProjector
from hyprial.daemon.impl.org.network.ipc import _publish_identity_binding_after_join

from .test_withdraw_publish import _row, _write_settings


class _Cache:
    next_recheck_at = None

    def rebuild(self) -> bool:
        return True


class _Directory:
    def __init__(self) -> None:
        self.puts: list[str] = []
        self.published = threading.Event()

    def binding_watch_targets(self) -> list[tuple[str, str]]:
        return []

    def orgs(self) -> list[str]:
        return ["group-a"]

    def put_binding(self, org: str, _row: dict[str, str]) -> None:
        self.puts.append(org)
        self.published.set()


def _app(home: Path, directory: Any, projector: Any) -> DaemonApplication:
    app = object.__new__(DaemonApplication)
    app.state_dir = home / "state"
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app.owner = "owner@example"
    app._org_identity_binding_projector = projector
    app._org_binding_directory = lambda: directory  # type: ignore[method-assign]
    app._start_org_identity_binding_cache = lambda: None  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    return app


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda app: app._start_org_identity_bindings_safely(), id="startup"
        ),
        pytest.param(_publish_identity_binding_after_join, id="join"),
    ],
)
def test_directory_discovery_failure_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[Any], None],
) -> None:
    home = tmp_path / "home"
    _write_settings(home, _row())
    directory = _Directory()
    projector = OrgBindingCacheProjector(
        cache=_Cache(), directory=directory, facade=object()
    )
    app = _app(home, directory, projector)
    attempts = 0

    def discover() -> _Directory:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("directory unavailable")
        return directory

    app._org_binding_directory = discover  # type: ignore[method-assign]
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 0.02
    )
    try:
        projector.start()
        invoke(app)
        assert directory.published.wait(1.0)
    finally:
        projector.close()

    assert attempts == 2


def test_pending_retry_unions_orgs_instead_of_swallowing_second_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_settings(home, _row())
    completed: set[str] = set()
    both = threading.Event()

    class Directory(_Directory):
        def orgs(self) -> list[str]:
            return ["group-a", "group-b"]

        def put_binding(self, org: str, _row: dict[str, str]) -> None:
            self.puts.append(org)
            if self.puts.count(org) == 1:
                raise RuntimeError(f"{org} transient")
            completed.add(org)
            if completed == {"group-a", "group-b"}:
                both.set()

    directory = Directory()
    projector = OrgBindingCacheProjector(
        cache=_Cache(), directory=directory, facade=object()
    )
    app = _app(home, directory, projector)
    monkeypatch.setattr(
        binding_module, "IDENTITY_BINDING_PUBLISH_RETRY_SECONDS", 0.05
    )
    try:
        projector.start()
        with pytest.raises(RuntimeError):
            app._publish_identity_binding(orgs=("group-a",))
        with pytest.raises(RuntimeError):
            app._publish_identity_binding(orgs=("group-b",))
        assert both.wait(1.0)
    finally:
        projector.close()

    assert directory.puts == ["group-a", "group-b", "group-a", "group-b"]


def test_retry_attempt_climbs_through_real_projector_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []
    observed = threading.Event()

    def curve(_base: float, _cap: float, attempt: int) -> float:
        attempts.append(attempt)
        if len(attempts) >= 3:
            observed.set()
        return 0.01

    def fail() -> None:
        raise RuntimeError("permanent")

    monkeypatch.setattr(binding_module, "capped_exponential", curve)
    projector = OrgBindingCacheProjector(
        cache=_Cache(), directory=_Directory(), facade=object()
    )
    try:
        projector.start()
        projector.schedule_publish(time.time(), fail)
        assert observed.wait(1.0)
    finally:
        projector.close()

    assert attempts[:3] == [0, 1, 2]


def test_numeric_date_validation_has_one_identity_owned_helper() -> None:
    from hyprial.identity import binding_numeric_date

    assert binding_numeric_date(1, "issued-at") == 1.0
    for value in (True, 0, 1 << 53, 1.7e308, 10**400, "1"):
        with pytest.raises(ValueError, match="issued-at"):
            binding_numeric_date(value, "issued-at")

    source = Path(binding_module.__file__).read_text(encoding="utf-8")
    assert "_NUMERIC_DATE_LIMIT" not in source
    assert "binding_numeric_date" in source
