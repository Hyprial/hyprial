"""Acting-owner identity uses the same home as the public CLI; never real ~/.hyprial.

The acting principal is the LOCAL HUMAN identity (``resolve_node_owner``),
surfaced to PAC as a full ``user:<owner>`` URI.  Missing identity is a loud
error on identity-bearing operations (activate/close/flag); drafting with
full principal URIs needs no local identity at all -- the write boundary
validates shape, never local knowledge.
"""

import json

import pytest
from hyprial.identity import PacError
from hyprial.daemon.impl.configuration.identity import resolve_node_owner
from hyprial.biz.impl.workflow.identity import actor_owner as _actor_owner
from hyprial.kernel import canonical_user_uri
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_node, close_graph, create_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.views.context import node_context


@pytest.mark.parametrize("selection", ["default", "explicit", "empty"])
def test_owner_activate_and_close_use_the_selected_home(monkeypatch, tmp_path, selection):
    fake_home = tmp_path / "fake-login-home"
    default = fake_home / ".hyprial"
    default.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    if selection == "default":
        monkeypatch.delenv("HYPRIAL_HOME", raising=False)
        home = default
    else:
        home = tmp_path / "explicit-profile"
        home.mkdir()
        monkeypatch.setenv("HYPRIAL_HOME", str(home) if selection == "explicit" else "")
        if selection == "empty":
            monkeypatch.chdir(home)  # unified home resolver treats explicit '' as cwd
    (default / "state").mkdir()
    (home / "state").mkdir(exist_ok=True)
    (default / "settings.json").write_text(json.dumps({"owner": "wrong-default"}))
    (home / "settings.json").write_text(json.dumps({"owner": "home-owner"}))
    # Drafting with full principal URIs needs no local identity; the owner is
    # a stored address, not a claim about who is typing.
    monkeypatch.setenv("HYPRIAL_OWNER", "home-owner")
    store = PacGraphStore(default_database_path(home / "state"))
    graph = create_graph(store, name="home-route", created_by="user:home-owner")
    add_node(store, home / "state", graph_id=graph["graphId"], node_id="task", owner="user:home-owner",
             brief_ref="artifact#brief", expect_version=graph["version"])
    assert (home / "state" / "pac-graph.sqlite3").is_file()
    # Identity-bearing operations resolve the local human from the SELECTED
    # home and act under its full user: URI.
    monkeypatch.delenv("HYPRIAL_OWNER")
    activate_graph(store, graph["graphId"], actor="user:home-owner")
    close_graph(store, graph["graphId"], actor="user:home-owner")
    context = node_context(store.path, graph["graphId"], "task")
    assert context["closed"]["by"] == "user:home-owner"
    assert context["completedCount"] == 0 and "actor" not in context
    store.close()
    monkeypatch.setenv("HYPRIAL_OWNER", "environment-wins")
    assert _actor_owner() == "user:environment-wins"


@pytest.mark.parametrize("settings, override", [
    ('{"owner": "  config-owner  "}', None),
    ('{"owner": "config-owner"}', '  environment-owner  '),
    ('{"owner": "config-owner"}', '   '),
    ('{}', None),
    ('not-json', 'environment-owner'),
    ('{"owner": "bad:owner"}', None),
    ('{"owner": ""}', None),
    ('[]', None),
    ('not-json', None),
    (None, None),
])
def test_owner_uses_canonical_normalization_and_validation(monkeypatch, tmp_path, settings, override):
    home = tmp_path / "selected-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    if settings is not None:
        (home / "settings.json").write_text(settings)
    if override is not None:
        monkeypatch.setenv("HYPRIAL_OWNER", override)
    try:
        expected = canonical_user_uri(resolve_node_owner(hyprial_home=home))
    except ValueError as error:
        with pytest.raises(PacError) as rejected:
            _actor_owner()
        assert rejected.value.code == "PAC_OWNER_UNKNOWN"
        assert str(rejected.value) == str(error)
    else:
        assert _actor_owner() == expected


def test_owner_delegates_to_real_shared_resolver(monkeypatch, tmp_path):
    from hyprial.daemon.impl.configuration import identity

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_OWNER", "shared-owner")
    calls = []
    real = identity.resolve_node_owner

    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)  # actual resolver, no fabricated response

    monkeypatch.setattr(identity, "resolve_node_owner", observed)
    assert _actor_owner() == "user:shared-owner"
    assert calls == [((), {"hyprial_home": tmp_path.resolve()})]


@pytest.mark.parametrize("settings", [None, '{}', '{broken', '[]', '{"owner": "bad:owner"}',
                                      '{"owner": "   "}', '{"owner": 1}'])
def test_missing_identity_differs_from_invalid_and_from_drafting(monkeypatch, tmp_path, settings):
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    # Drafting never needed the identity: full URIs are addresses, not claims.
    store = PacGraphStore(default_database_path(state))
    graph = create_graph(store, name="identity-paths", created_by="user:graph-owner")["graphId"]
    add_node(store, home / "state", graph_id=graph, node_id="task", owner="user:graph-owner",
             brief_ref="brief#task", expect_version=1)
    # Identity-bearing operations stay loud about a missing/invalid identity.
    path = home / "settings.json"
    if settings is not None:
        path.write_text(settings)
    missing = settings in (None, '{}')
    for operation in (activate_graph, close_graph):
        with pytest.raises(PacError) as raised:
            operation(store, graph, actor=_actor_owner())
        error = raised.value
        assert error.code == "PAC_OWNER_UNKNOWN", error
        if missing:
            assert "user identity is not set" in str(error) and "hyprial login" in str(error)
        elif settings == '{broken':
            assert str(path) in str(error) and "cannot parse" in str(error)
    store.close()


def test_env_only_and_disagreeing_settings_keep_the_same_principal(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    monkeypatch.setenv("HYPRIAL_OWNER", "  environment-owner  ")
    assert _actor_owner() == "user:environment-owner"
    (tmp_path / "settings.json").write_text('{"owner": "file-owner"}')
    assert _actor_owner() == "user:environment-owner"
    monkeypatch.setenv("HYPRIAL_OWNER", "   ")
    assert _actor_owner() == "user:file-owner"
