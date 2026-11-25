"""The ``hyprial gui --json`` startup stream contract and the code that emits it.

Decision record (2026-11-16, PR 6a): the corpus lives in ``contract/cli`` next to
the command-surface snapshot and is not part of ``contract/run-all.sh``, like
``contract/gui-session``: it needs no daemon, so pytest is its runner.  The tests
validate every example, the record the real ``startup_payload`` builds for
installed-shaped, source and dev sessions, and the failure objects the real
error path writes; a property test then looks for the grant and the bearer in
every string of the record.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

import jsonschema
import pytest

from hyprial.shell.impl.cli.output.errors import json_failure
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.errors import GuiError
from tests.shell.gui.backend import test_source_session as fixtures

CORPUS = Path(__file__).resolve().parents[4] / "contract" / "cli" / "gui-startup-record.v1.json"
FORBIDDEN_FRAGMENTS = ("#grant", "grant=", "Bearer", "Authorization")


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _validator(corpus: dict[str, Any], name: str) -> jsonschema.Draft202012Validator:
    schema = corpus[name]
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in (_strings(key) + _strings(child))]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def _consumer_checks(record: dict[str, Any]) -> None:
    parsed = urlsplit(record["url"])
    assert parsed.scheme == "http"
    assert parsed.path in ("", "/") and not parsed.query and not parsed.fragment
    assert parsed.username is None and parsed.password is None
    assert parsed.hostname == record["host"]
    assert parsed.port == record["port"]


def test_the_corpus_is_one_versioned_document(corpus: dict[str, Any]) -> None:
    assert corpus["contract"] == "hyprial-gui-startup-record"
    assert corpus["version"] == 1
    assert corpus["examples"]["startup"] and corpus["examples"]["failure"]


def test_every_startup_example_validates_and_is_self_consistent(corpus: dict[str, Any]) -> None:
    validator = _validator(corpus, "startupRecord")
    for record in corpus["examples"]["startup"]:
        validator.validate(record)
        _consumer_checks(record)
        assert not [s for s in _strings(record) if any(f in s for f in FORBIDDEN_FRAGMENTS)]


def test_every_failure_example_validates(corpus: dict[str, Any]) -> None:
    validator = _validator(corpus, "failure")
    for failure in corpus["examples"]["failure"]:
        validator.validate(failure)
        if "code" in failure:
            assert failure["code"] in corpus["codes"]


def test_the_schemas_refuse_what_a_consumer_must_not_accept(corpus: dict[str, Any]) -> None:
    start = _validator(corpus, "startupRecord")
    good = corpus["examples"]["startup"][0]
    for mutation in (
        {"port": 0},
        {"port": 70000},
        {"url": "http://example.com:41234/"},
        {"url": "http://127.0.0.1:41234/path"},
        {"url": "http://user@127.0.0.1:41234/"},
        {"host": "0.0.0.0"},
        {"mode": "start"},
        {"handoff_path": "relative/launch.url"},
        {"ok": False},
    ):
        assert not start.is_valid({**good, **mutation}), mutation
    assert not start.is_valid({key: value for key, value in good.items() if key != "record_version"})
    failure = _validator(corpus, "failure")
    assert not failure.is_valid({"ok": True, "error": "x"})
    assert not failure.is_valid({"ok": False})
    assert not failure.is_valid({"ok": False, "error": "x", "data": {"children": {"a": {"pid": 1}}}})


WINDOWS_HANDOFFS = (
    "C:\\Users\\someone\\AppData\\Local\\hyprial\\gui\\daemon\\launch.url",
    "d:/state/gui/daemon/launch.url",
    "\\\\server\\share\\hyprial\\gui\\daemon\\launch.url",
    "\\\\?\\C:\\Users\\someone\\hyprial\\gui\\daemon\\launch.url",
    str(PureWindowsPath("C:/Users/someone/hyprial/gui/daemon/launch.url")),
    str(PureWindowsPath("//server/share/hyprial/gui/daemon/launch.url")),
)
NON_ABSOLUTE_HANDOFFS = (
    "relative/launch.url",
    "launch.url",
    "\\gui\\daemon\\launch.url",
    "C:launch.url",
    "C:",
    "\\\\server",
    "\\\\server\\",
    "",
)


def test_handoff_path_accepts_posix_and_windows_absolute_paths(corpus: dict[str, Any]) -> None:
    start = _validator(corpus, "startupRecord")
    good = corpus["examples"]["startup"][0]
    for path in ("/var/tmp/launch.url", "//server/share/launch.url", *WINDOWS_HANDOFFS):
        assert start.is_valid({**good, "handoff_path": path}), path
    for path in NON_ABSOLUTE_HANDOFFS:
        assert not start.is_valid({**good, "handoff_path": path}), path


def test_the_corpus_has_a_windows_shaped_handoff_example(corpus: dict[str, Any]) -> None:
    handoffs = [r["handoff_path"] for r in corpus["examples"]["startup"] if r["handoff_path"]]
    assert any(path.startswith("/") for path in handoffs)
    assert any(path[1:3] == ":\\" for path in handoffs)


def test_pathlib_renders_windows_handoffs_with_backslashes() -> None:
    assert WINDOWS_HANDOFFS[-2] == "C:\\Users\\someone\\hyprial\\gui\\daemon\\launch.url"
    assert WINDOWS_HANDOFFS[-1] == "\\\\server\\share\\hyprial\\gui\\daemon\\launch.url"


def test_backend_owned_is_false_for_the_daemon_driver_and_true_for_the_mock(
    corpus: dict[str, Any],
) -> None:
    examples = corpus["examples"]["startup"]
    assert {r["backend_driver"] for r in examples} == {"daemon", "contract-mock"}
    for record in examples:
        assert record["backend_owned"] is (record["backend_driver"] != "daemon")


REQUEST_HTTP_BEARER = "request-http-private-bearer"


def _sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Contract-mock source and dev sessions, plus daemon-driver source and installed-shaped
    sessions that hold the known request-http bearer and the known launch grant."""
    count = 0
    for open_browser in (False, True):
        for kind in ("mock", "dev", "daemon", "installed"):
            count += 1
            home = tmp_path / f"session-{count}"
            home.mkdir()
            if kind == "installed":
                plan = fixtures._installed_plan(home, open_browser=open_browser)
                session, _events, _captured = fixtures._start_installed(monkeypatch, plan, [])
            elif kind == "daemon":
                plan = replace(fixtures._plan(home, driver="daemon"), open_browser=open_browser)
                assert plan.admission is not None
                plan.admission.home.mkdir()
                session, _events, _captured = fixtures._start_installed(monkeypatch, plan, [])
            else:
                plan = fixtures._plan(home) if kind == "mock" else fixtures._dev_plan(home)
                session, _events, _captured = fixtures._start(
                    monkeypatch, plan, open_browser=open_browser
                )
            yield session


def test_the_real_startup_payload_validates_for_every_mode(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    validator = _validator(corpus, "startupRecord")
    seen = []
    for session in _sessions(monkeypatch, tmp_path):
        try:
            payload = launcher.startup_payload(session)
            assert payload["backend_owned"] is (payload["backend_driver"] != "daemon")
            validator.validate(json.loads(json.dumps(payload)))
            _consumer_checks(payload)
            assert payload["record_version"] == corpus["version"]
            seen.append((payload["mode"], payload["handoff_path"] is None))
        finally:
            session.stop()
    assert sorted(seen) == [("dev", False), ("dev", True)] + [("normal", False)] * 3 + [
        ("normal", True)
    ] * 3


def test_no_value_in_the_payload_carries_the_grant_or_the_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for session in _sessions(monkeypatch, tmp_path):
        try:
            payload = launcher.startup_payload(session)
            for text in _strings(payload):
                assert fixtures.GRANT not in text
                assert fixtures.MOCK_TOKEN not in text
                assert REQUEST_HTTP_BEARER not in text
                assert not any(fragment in text for fragment in FORBIDDEN_FRAGMENTS)
            if session.handoff_path is not None:
                assert "#grant=" in session.handoff_path.read_text(encoding="utf-8")
        finally:
            session.stop()


def test_real_failures_validate_with_stages_and_children(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    validator = _validator(corpus, "failure")
    plan = fixtures._dev_plan(tmp_path)
    events, _captured = fixtures._install_success_seams(monkeypatch, plan, assets_exit=1)
    with pytest.raises(GuiError) as caught:
        fixtures._run_start(replace(plan, open_browser=False), events, None)
    failure = json_failure(caught.value)
    validator.validate(json.loads(json.dumps(failure)))
    assert failure["data"]["stages_seconds"]
    assert failure["data"]["children"]["assets"]["exit_code"] == 1
    for error in (
        GuiError("GUI_BACKEND_EXITED", "x", {"backend_pid": 1, "backend_exit_code": 0}),
        GuiError("INVALID_ARGUMENT", "x"),
    ):
        validator.validate(json.loads(json.dumps(json_failure(error))))


STALE_TEXT = ("rotate", "m1", "idp login")
TEXT_KEYS = ("business", "note", "handoff_note")


def _record_texts(record: dict[str, Any]) -> list[str]:
    return [record[key] for key in TEXT_KEYS if isinstance(record.get(key), str)]


def test_record_text_names_no_rotation_or_m1(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    texts = [text for record in corpus["examples"]["startup"] for text in _record_texts(record)]
    for session in _sessions(monkeypatch, tmp_path):
        try:
            texts.extend(_record_texts(launcher.startup_payload(session)))
        finally:
            session.stop()
    assert texts
    stale = [t for t in texts if any(word in t.lower() for word in STALE_TEXT)]
    assert not stale


def test_record_has_no_m1_source_key(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert "m1_source" not in corpus["startupRecord"]["properties"]
    assert "m1_source" not in corpus["startupRecord"]["required"]
    assert all("m1_source" not in record for record in corpus["examples"]["startup"])
    for session in _sessions(monkeypatch, tmp_path):
        try:
            assert "m1_source" not in launcher.startup_payload(session)
        finally:
            session.stop()
