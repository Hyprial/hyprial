"""The generic human view (design §4.6)."""

from hyprial.shell.impl.cli.output.render import render_generic


def test_ok_true_is_hidden_and_an_empty_success_reads_ok():
    assert render_generic({"ok": True}) == "ok"
    assert render_generic({"ok": True, "name": "a"}) == "name: a"


def test_ok_false_stays_visible():
    assert render_generic({"ok": False, "reason": "x"}).splitlines() == ["ok:     false", "reason: x"]


def test_scalars_align_and_use_json_spelling():
    text = render_generic({"agent": "agent:alice", "running": False, "pid": None, "count": 3})
    assert text.splitlines() == [
        "agent:   agent:alice",
        "running: false",
        "pid:     -",
        "count:   3",
    ]


def test_a_list_of_objects_is_a_table_with_first_seen_columns():
    text = render_generic({"agents": [{"name": "a", "state": "idle"}, {"name": "bob", "extra": {"x": 1}}]})
    assert text.splitlines() == [
        "agents:",
        "  name  state  extra",
        "  a     idle   -",
        "  bob   -      {\"x\":1}",
    ]


def test_empty_lists_nested_objects_and_plain_lists():
    text = render_generic({"grants": [], "daemon": {"pid": 42}, "tags": ["x", "y"], "meta": {}})
    assert text.splitlines() == [
        "grants: (none)",
        "daemon:",
        "  pid: 42",
        "tags:",
        "  - x",
        "  - y",
        "meta:",
        "  (none)",
    ]


def test_values_are_never_cut_and_a_wide_table_becomes_records():
    url = "https://fs.example/" + "a" * 120
    text = render_generic({"rows": [{"name": "readme.md", "url": url}, {"name": "b", "url": "u"}]})
    assert url in text
    assert text.splitlines() == [
        "rows:",
        "  name: readme.md",
        f"  url:  {url}",
        "",
        "  name: b",
        "  url:  u",
    ]
