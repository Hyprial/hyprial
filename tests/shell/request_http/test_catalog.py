"""Route filtering and deterministic contract digests."""

from __future__ import annotations

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http.catalog import (
    CatalogProblem,
    build_view,
    canonical_digest,
    declaration_digest,
    http_contract_digest,
    http_contract_document,
)

PING = {
    "version": __version__,
    "nodeId": "h01-node",
    "owner": "h01-owner",
    "epoch": "epoch-1",
    "phase": "serving",
}


def _entries(**states: str) -> list[dict]:
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = states.get(entry["name"], "disabled" if not entry["httpExposed"] else "available")
        item["callable"] = item["state"] == "available"
        item["reason"] = None if item["state"] != "not-ready" else "SERVICE_NOT_READY"
        entries.append(item)
    return entries


def _catalog(entries: list[dict], **overrides: object) -> dict:
    document = {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "epoch": "epoch-1",
        "packageVersion": __version__,
        "methods": entries,
    }
    document.update(overrides)
    return document


def _view(entries: list[dict] | None = None, *, local: list[dict] | None = None):
    selected = _entries() if entries is None else entries
    return build_view(
        PING,
        _catalog(selected),
        socket_path="/tmp/daemon.sock",
        local_entries=selected if local is None else local,
    )


def test_only_supported_safe_unary_operator_reads_become_routes() -> None:
    view = _view()
    assert set(view.routes) == {"link.list", "link.stats", "agent.list", "agent.secret.list", "agent.provider.default"}
    assert view.routes["link.list"].path == "/daemon/request/link.list"
    assert view.routes["link.list"].http_binding["identity"] == {
        "mode": "local-operator-selector",
        "param": "from",
    }
    assert "daemon.catalog" not in view.routes
    assert "link.add" not in view.routes


def test_eligible_declaration_without_a_selector_gets_the_no_selector_route() -> None:
    entries = _entries()
    source = next(entry for entry in entries if entry["name"] == "link.list")
    entries.append(
        {
            **source,
            "name": "synthetic.echo",
            "owner": "synthetic.probe",
            "httpBinding": {
                "identity": {"mode": "none", "param": None},
                "readiness": None,
            },
        }
    )
    view = _view(entries, local=entries)
    route = view.routes["synthetic.echo"]
    assert route.http_binding["identity"] == {"mode": "none", "param": None}
    assert route.state == "available"


def test_not_ready_keeps_the_route_but_is_not_callable() -> None:
    view = _view(_entries(**{"link.list": "not-ready"}))
    route = view.routes["link.list"]
    assert route.state == "not-ready"
    assert route.callable is False
    assert route.reason == "SERVICE_NOT_READY"


def _mutated(name: str, **changes: object) -> list[dict]:
    entries = _entries()
    for entry in entries:
        if entry["name"] == name:
            entry.update(changes)
    return entries


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"audience": ["internal"]}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"audience": ["bound-agent"]}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"mode": "cursor-poll"}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"mutates": True}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"retry": "never"}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"confirmation": "preview"}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"httpBinding": None}, "HTTP_BINDING_MISSING"),
        (
            {"httpBinding": {"identity": {"mode": "bound-agent-session", "param": "from"}}},
            "HTTP_CALL_PROFILE_UNSUPPORTED",
        ),
    ],
)
def test_http_enabled_but_out_of_profile_is_excluded(changes: dict, reason: str) -> None:
    view = _view(_mutated("link.list", **changes))
    assert "link.list" not in view.routes
    assert view.skipped["link.list"] == reason


def test_declaration_mismatch_fails_the_snapshot() -> None:
    entries = _entries()
    for entry in entries:
        if entry["name"] == "link.stats":
            entry["owner"] = "daemon.other"
    with pytest.raises(CatalogProblem, match="declarations differ"):
        build_view(
            PING,
            _catalog(entries),
            socket_path="/tmp/daemon.sock",
            local_entries=local_catalog()["methods"],
        )


def test_catalog_shape_and_duplicate_names_are_refused() -> None:
    entries = _entries()
    with pytest.raises(CatalogProblem, match="version"):
        build_view(
            PING,
            _catalog(entries, catalogVersion="2"),
            socket_path="/tmp/daemon.sock",
            local_entries=entries,
        )
    with pytest.raises(CatalogProblem, match="duplicate"):
        build_view(
            PING,
            _catalog(entries + [entries[-1]]),
            socket_path="/tmp/daemon.sock",
            local_entries=entries,
        )


def test_http_contract_digest_tracks_contract_and_ignores_state() -> None:
    base = _view().http_contract_digest
    assert base == http_contract_digest(sorted(_view().routes.values(), key=lambda r: r.name))
    assert base == _view(_entries(**{"link.list": "not-ready"})).http_contract_digest

    # A changed mode/mutates/audience/path enters the HTTP contract.
    changed = _view(_mutated("link.list", mutates=True))
    assert changed.http_contract_digest != base
    # The declaration digest includes the same mutation.
    assert declaration_digest(_entries()) == _view().declaration_digest
    assert changed.declaration_digest != _view().declaration_digest
    # Adding an unexposed method would change declarations only; the local
    # projection is fixed, so this test is covered by the binding unit test.
    document = http_contract_document(sorted(_view().routes.values(), key=lambda r: r.name))
    assert document["methods"][0]["requiredAudience"] == ["local-operator"]
    assert "readiness" not in document["methods"][0]["httpBinding"]


def test_canonical_digest_is_sorted_and_stable() -> None:
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})
    assert canonical_digest({"a": 2}) != canonical_digest({"a": 3})
