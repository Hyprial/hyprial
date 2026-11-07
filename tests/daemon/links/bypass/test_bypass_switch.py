"""Emergency bypass switch parsing and composition (``HYPRIAL_LINKS_BYPASS``).

Env wins over ``settings.json`` ``links.bypass``; default on; a malformed
value fails closed exactly like ``links.windowMode`` (composition fails, the
admission slot stays unbound, sends hold).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.composition.links import (
    BYPASS_ENV,
    LinkBypassError,
    LinkWindowModeError,
    close_link_service,
    compose_link_service,
    link_bypass,
    link_window_mode,
)
from hyprial.daemon.impl.links.service import LinkService
from tests.support.links.app import OrgFsWithMeta, stub_app
from tests.support.links.ledger import FakeInboxPort
from tests.support.links.org import LogRecorder


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", " Yes "])
def test_truthy_values_turn_the_switch_on(tmp_path: Path, raw: str) -> None:
    assert BYPASS_ENV == "HYPRIAL_LINKS_BYPASS"
    assert link_bypass(tmp_path, {BYPASS_ENV: raw}) is True


@pytest.mark.parametrize("raw", ["0", "false", "no"])
def test_explicit_falsy_values_turn_the_switch_off(tmp_path: Path, raw: str) -> None:
    assert link_bypass(tmp_path, {BYPASS_ENV: raw}) is False


def test_default_is_on(tmp_path: Path) -> None:
    assert link_bypass(tmp_path, {}) is True


@pytest.mark.parametrize("raw", ["maybe", "on", "2", "enable"])
def test_malformed_env_raises(tmp_path: Path, raw: str) -> None:
    with pytest.raises(LinkBypassError):
        link_bypass(tmp_path, {BYPASS_ENV: raw})


def test_settings_key_and_env_precedence(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {"bypass": True}}), encoding="utf-8"
    )
    assert link_bypass(tmp_path, {}) is True
    assert link_bypass(tmp_path, {BYPASS_ENV: ""}) is True
    assert link_bypass(tmp_path, {BYPASS_ENV: "  "}) is True
    assert link_bypass(tmp_path, {BYPASS_ENV: "0"}) is False
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {"bypass": False}}), encoding="utf-8"
    )
    assert link_bypass(tmp_path, {}) is False
    assert link_bypass(tmp_path, {BYPASS_ENV: ""}) is False
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {"windowMode": "observe"}}), encoding="utf-8"
    )
    assert link_bypass(tmp_path, {}) is True
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {"bypass": "perhaps"}}), encoding="utf-8"
    )
    with pytest.raises(LinkBypassError):
        link_bypass(tmp_path, {})
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LinkBypassError):
        link_bypass(tmp_path, {})


def test_links_section_without_bypass_uses_the_default(
    tmp_path: Path,
) -> None:
    """A missing or null ``bypass`` value means the default-on policy."""

    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {"windowMode": "enforce"}}), encoding="utf-8"
    )
    assert link_bypass(tmp_path, {}) is True
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {}}), encoding="utf-8"
    )
    assert link_bypass(tmp_path, {}) is True
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": {"bypass": None}}), encoding="utf-8"
    )
    assert link_bypass(tmp_path, {}) is True


def _org_fs() -> OrgFsWithMeta:
    fs = OrgFsWithMeta()
    fs.add_space("sp-0", name="group-test")
    return fs


def test_default_on_still_refuses_a_remote_non_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BYPASS_ENV, raising=False)
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    try:
        assert isinstance(service, LinkService)
        assert service.bypass is True
        assert service.sender_known(
            recipient="agent:o:n1:a", sender="agent:q:n2:stranger"
        ) is False
    finally:
        close_link_service(app)


def test_malformed_env_fails_composition_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BYPASS_ENV, "maybe")
    logs = LogRecorder()
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs, _log=logs)
    assert compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort()) is None
    assert app.link_admission.bound is False
    assert logs.fields_for("link.service_composed") == []
    assert logs.fields_for("link.service_failed")


@pytest.mark.parametrize("raw,expected", [("1", True), ("0", False)])
def test_compose_reports_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv(BYPASS_ENV, raw)
    logs = LogRecorder()
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs, _log=logs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    try:
        assert isinstance(service, LinkService)
        assert service.bypass is expected
        composed = logs.fields_for("link.service_composed")
        assert composed and composed[0]["bypass"] is expected
    finally:
        close_link_service(app)



@pytest.mark.parametrize("section", [5, True, "enforce", ["bypass"]])
def test_a_non_object_links_section_fails_closed(
    tmp_path: Path, section: object
) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": section}), encoding="utf-8"
    )
    with pytest.raises(LinkBypassError, match="links must be an object"):
        link_bypass(tmp_path, {})


def test_a_non_object_links_section_fails_composition_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BYPASS_ENV, raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "settings.json").write_text(
        json.dumps({"links": "enforce"}), encoding="utf-8"
    )
    logs = LogRecorder()
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs, _log=logs)
    assert compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort()) is None
    assert app.link_admission.bound is False
    assert logs.fields_for("link.service_composed") == []
    assert logs.fields_for("link.service_failed")


def test_missing_and_null_bypass_are_still_on(tmp_path: Path) -> None:
    for section in ({}, {"bypass": None}):
        (tmp_path / "settings.json").write_text(
            json.dumps({"links": section}), encoding="utf-8"
        )
        assert link_bypass(tmp_path, {}) is True


@pytest.mark.parametrize("section", [5, True, "observe", ["windowMode"]])
def test_link_window_mode_rejects_a_non_object_links_section(
    tmp_path: Path, section: object
) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"links": section}), encoding="utf-8"
    )
    with pytest.raises(LinkWindowModeError, match="links must be an object"):
        link_window_mode(tmp_path, {})


def test_compose_passes_bookkeeping_off_when_bypass_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.composition.links as composition

    monkeypatch.setenv(BYPASS_ENV, "1")
    captured: dict[str, object] = {}
    real_store = composition.LinkStore

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_store(*args, **kwargs)

    monkeypatch.setattr(composition, "LinkStore", spy)
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    try:
        assert isinstance(service, LinkService)
        assert captured["bookkeeping"] is False
    finally:
        close_link_service(app)
