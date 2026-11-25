# Decision record (hyprial gui --with-dev-tools, PR 4b-C, 2026-11-17):
# - The startup record gains two optional booleans, dev_tools and api_docs.
#   dev_tools is the plan's; api_docs is what the host said on its ready line
#   (the launcher does not probe /docs). record_version stays 1: no required
#   key changed meaning, and a record without the two keys still validates.
# - The cases start real sessions through the fake-child seams of the session
#   tests, with a ready line that reports the tools, and validate the emitted
#   record against contract/cli/gui-startup-record.v1.json.
"""``dev_tools`` and ``api_docs`` in the startup record and its human rendering."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import source as backend_source
from tests.shell.gui.backend import test_source_session as fixtures

CORPUS = Path(__file__).resolve().parents[4] / "contract" / "cli" / "gui-startup-record.v1.json"
BEARER = "request-http-private-bearer"


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def validator(corpus: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(corpus["startupRecord"])


def report_tools(monkeypatch: pytest.MonkeyPatch, *, api_docs: bool) -> None:
    plain = fixtures._ready_line

    def line(plan: Any) -> str:
        payload = json.loads(plain(plan)[len(backend_source.HOST_READY_PREFIX) :])
        payload.update({"devTools": plan.dev_tools, "apiDocs": plan.dev_tools and api_docs})
        return backend_source.HOST_READY_PREFIX + json.dumps(payload)

    monkeypatch.setattr(fixtures, "_ready_line", line)


def installed_session(monkeypatch: pytest.MonkeyPatch, home: Path, *, dev_tools: bool, api_docs: bool) -> Any:
    report_tools(monkeypatch, api_docs=api_docs)
    plan = replace(fixtures._installed_plan(home, open_browser=False), dev_tools=dev_tools)
    session, _events, _captured = fixtures._start_installed(monkeypatch, plan, [])
    return session


def test_the_corpus_declares_both_keys_as_optional_booleans(corpus: dict[str, Any]) -> None:
    schema = corpus["startupRecord"]
    assert schema["properties"]["dev_tools"] == {"type": "boolean"}
    assert schema["properties"]["api_docs"] == {"type": "boolean"}
    assert "dev_tools" not in schema["required"] and "api_docs" not in schema["required"]
    assert corpus["version"] == 1
    checker = validator(corpus)
    examples = corpus["examples"]["startup"]
    assert any(example.get("dev_tools") is True and example.get("api_docs") is True for example in examples)
    assert any(example.get("dev_tools") is False for example in examples)
    for example in examples:
        without = {key: value for key, value in example.items() if key not in ("dev_tools", "api_docs")}
        checker.validate(without)
        assert not checker.is_valid({**example, "dev_tools": "yes"})
        assert not checker.is_valid({**example, "api_docs": 1})


def test_a_default_session_reports_no_developer_tools(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = installed_session(monkeypatch, tmp_path, dev_tools=False, api_docs=True)
    try:
        payload = launcher.startup_payload(session)
        validator(corpus).validate(json.loads(json.dumps(payload)))
        assert payload["dev_tools"] is False and payload["api_docs"] is False
        assert "  dev tools: not used" in launcher.render_startup(payload).splitlines()
    finally:
        session.stop()


@pytest.mark.parametrize(
    ("api_docs", "line"),
    [(True, "  dev tools: enabled (API docs available)"), (False, "  dev tools: enabled (no API docs)")],
)
def test_a_session_with_the_flag_reports_what_the_host_confirmed(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, api_docs: bool, line: str
) -> None:
    session = installed_session(monkeypatch, tmp_path, dev_tools=True, api_docs=api_docs)
    try:
        payload = launcher.startup_payload(session)
        validator(corpus).validate(json.loads(json.dumps(payload)))
        assert payload["dev_tools"] is True and payload["api_docs"] is api_docs
        assert payload["mode"] == "normal" and payload["hot_reload"] is False
        assert line in launcher.render_startup(payload).splitlines()
        text = json.dumps(payload) + launcher.render_startup(payload)
        assert fixtures.GRANT not in text and BEARER not in text
        assert "Bearer" not in text and "#grant" not in text
    finally:
        session.stop()


def test_a_dev_session_reports_the_tools_without_the_docs_page(
    corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report_tools(monkeypatch, api_docs=False)
    plan = replace(fixtures._dev_plan(tmp_path), dev_tools=True)
    session, _events, captured = fixtures._start(monkeypatch, plan, open_browser=False)
    try:
        payload = launcher.startup_payload(session)
        validator(corpus).validate(json.loads(json.dumps(payload)))
        assert payload["mode"] == "dev" and payload["dev_tools"] is True and payload["api_docs"] is False
        assert "--live-upstream" in captured["frontend"]["argv"]
    finally:
        session.stop()


def test_the_rendering_tolerates_a_record_without_the_keys(corpus: dict[str, Any]) -> None:
    example = {k: v for k, v in corpus["examples"]["startup"][0].items() if k not in ("dev_tools", "api_docs")}
    assert "  dev tools: not used" in launcher.render_startup(example).splitlines()
