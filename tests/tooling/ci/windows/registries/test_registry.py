"""The ``posix-only.json`` registry, the Windows test lists and the marker declaration.

Each test runs the checks of ``registry_gate`` against the real registry and
against edited copies of it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tests.support.windows_suite import collection


from tests.tooling.ci.windows.registries.registry_gate import (
    APPROVED_ENTRIES,
    EXPECTED_FAILURES,
    KNOWN_ID,
    KNOWN_PROBLEMS,
    LOCKED_REASONS,
    MARKER_DECLARATION,
    POSIX_ONLY,
    REASON_PATTERNS,
    REFUSED_REASONS,
    REGISTRIES,
    ROOT,
    approved_entries,
    covers,
    document,
    expected_failure_problems,
    known_problem_ids,
    list_problems,
    listed_test_paths,
    reason_problems,
    registered,
    registry_problems,
    windows_test_lists,
)

def test_the_allowed_reasons_are_the_locked_thirteen_and_never_killpg() -> None:
    value = document(POSIX_ONLY)
    assert value["version"] == 1
    assert tuple(value["allowed_needs"]) == LOCKED_REASONS
    assert len(LOCKED_REASONS) == 13
    assert len(set(LOCKED_REASONS)) == 13
    assert set(REFUSED_REASONS).isdisjoint(LOCKED_REASONS)
    assert set(REFUSED_REASONS).isdisjoint(value["allowed_needs"])
    assert set(REASON_PATTERNS) == set(LOCKED_REASONS)


def test_the_registry_names_exactly_the_four_approved_entries() -> None:
    value = document(POSIX_ONLY)
    assert approved_entries(value) == APPROVED_ENTRIES
    assert len(value["entries"]) == 4


def test_no_registered_path_is_a_test_list_path_or_under_a_registered_directory() -> None:
    value = document(POSIX_ONLY)
    assert windows_test_lists(), "no Windows test list found under scripts/ci/windows"
    assert list_problems(value, REGISTRIES) == []


def test_the_machine_lock_test_file_is_marked_not_registered() -> None:
    value = document(POSIX_ONLY)
    registered_paths = [path for path, _ in registered(value)]
    assert "tests/integration/e2e/harness/isolation/test_e2e_machine_lock.py" not in registered_paths
    assert not any("test_e2e_machine_lock" in path for path in registered_paths)


def test_the_registry_is_well_formed_and_its_evidence_holds() -> None:
    assert registry_problems(document(POSIX_ONLY)) == []


def test_a_registered_path_exists_with_the_kind_it_declares() -> None:
    for entry in document(POSIX_ONLY)["entries"]:
        target = ROOT / entry["path"]
        expected = "dir" if target.is_dir() else "file" if target.is_file() else "nothing"
        assert expected == entry["kind"], (entry["path"], entry["kind"], expected)


def test_killpg_is_refused_even_when_a_registry_lists_it_as_allowed() -> None:
    value = document(POSIX_ONLY)
    assert "killpg" not in value["allowed_needs"]
    listed = {**value, "allowed_needs": [*value["allowed_needs"], "killpg"]}
    unlocked = f"allowed_needs {[*LOCKED_REASONS, 'killpg']!r} is not the locked list of 13 reasons"
    assert set(registry_problems(listed)) == {unlocked, "allowed_needs contains the refused reason 'killpg'"}
    assert registry_problems({**listed, "entries": [{**value["entries"][0], "needs": ["killpg"]}]}) == [
        unlocked,
        "allowed_needs contains the refused reason 'killpg'",
        "entries[0]: reason 'killpg' is refused by name",
        "entries[0]: reason 'killpg' is not one of the locked reasons",
    ]


def test_a_reason_is_accepted_only_when_locked_and_allowed() -> None:
    assert reason_problems("fcntl", LOCKED_REASONS) == []
    assert reason_problems("AF_UNIX-path", LOCKED_REASONS) == []
    assert reason_problems("fcntl", ["pty"]) == ["reason 'fcntl' is not in allowed_needs"]
    assert reason_problems("pty", "pty") == ["reason 'pty' is not in allowed_needs"]
    assert reason_problems("pty", None) == ["reason 'pty' is not in allowed_needs"]
    assert reason_problems("killpg", [*LOCKED_REASONS, "killpg"]) == ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons"]
    assert reason_problems("killpg", LOCKED_REASONS) == ["reason 'killpg' is refused by name", "reason 'killpg' is not one of the locked reasons", "reason 'killpg' is not in allowed_needs"]
    for empty in ("", "   ", None, 7):
        assert reason_problems(empty, LOCKED_REASONS) == [f"reason {empty!r} is not a non-empty string"], empty


def test_a_registry_with_a_wrong_version_or_shape_is_refused() -> None:
    value = document(POSIX_ONLY)
    assert registry_problems(value) == []
    assert "version 2 is not 1" in registry_problems({**value, "version": 2})
    assert "version None is not 1" in registry_problems({k: v for k, v in value.items() if k != "version"})
    assert registry_problems([]) == ["registry is not an object"]
    assert registry_problems(None) == ["registry is not an object"]
    assert registry_problems({**value, "entries": {}}) == ["entries is not a list"]
    assert registry_problems({**value, "entries": "x"}) == ["entries is not a list"]


def test_an_allowed_list_that_is_not_the_locked_one_is_refused() -> None:
    value = document(POSIX_ONLY)
    refused = registry_problems({**value, "allowed_needs": [*value["allowed_needs"], "killpg"]})
    assert refused == [f"allowed_needs {[*LOCKED_REASONS, 'killpg']!r} is not the locked list of 13 reasons", "allowed_needs contains the refused reason 'killpg'"]
    for allowed in ([need for need in value["allowed_needs"] if need != "grp"], [*value["allowed_needs"][1:], value["allowed_needs"][0]]):
        assert [problem.endswith("is not the locked list of 13 reasons") for problem in registry_problems({**value, "allowed_needs": allowed})] == [True]
    assert registry_problems({**value, "allowed_needs": [*value["allowed_needs"], "fcntl"]}) == ["allowed_needs has duplicates", f"allowed_needs {[*LOCKED_REASONS, 'fcntl']!r} is not the locked list of 13 reasons"]
    assert "allowed_needs is not a list of strings" in registry_problems({**value, "allowed_needs": "fcntl"})
    assert "allowed_needs is not a list of strings" in registry_problems({**value, "allowed_needs": ["fcntl", 7]})
    assert "allowed_needs is not a list of strings" in registry_problems({**value, "allowed_needs": None})


def test_an_entry_with_a_bad_kind_need_or_evidence_is_refused() -> None:
    value = document(POSIX_ONLY)
    first = value["entries"][0]

    def problems_of(entry: object) -> list[str]:
        return registry_problems({**value, "entries": [entry]})

    assert any("kind 'module' is not one of" in problem for problem in problems_of({**first, "kind": "module"}))
    assert any("kind None is not one of" in problem for problem in problems_of({k: v for k, v in first.items() if k != "kind"}))
    assert any("needs [] is not a non-empty list" in problem for problem in problems_of({**first, "needs": []}))
    assert any("needs 'pwd' is not a non-empty list" in problem for problem in problems_of({**first, "needs": "pwd"}))
    assert any("needs None is not a non-empty list" in problem for problem in problems_of({k: v for k, v in first.items() if k != "needs"}))
    assert any("needs ['pty', 7] is not a non-empty list" in problem for problem in problems_of({**first, "needs": ["pty", 7]}))
    assert any("reason 'windows-only' is not one of the locked reasons" in problem for problem in problems_of({**first, "needs": ["windows-only"]}))
    without_termios = [need for need in value["allowed_needs"] if need != "termios"]
    assert "entries[0]: reason 'termios' is not in allowed_needs" in registry_problems(
        {**value, "allowed_needs": [*without_termios, "killpg"], "entries": [{**first, "needs": ["termios"]}]}
    )
    assert any("evidence '' is not a non-empty string" in problem for problem in problems_of({**first, "evidence": ""}))
    assert any("evidence '  ' is not a non-empty string" in problem for problem in problems_of({**first, "evidence": "  "}))
    assert any("evidence None is not a non-empty string" in problem for problem in problems_of({k: v for k, v in first.items() if k != "evidence"}))
    assert any("evidence 7 is not a non-empty string" in problem for problem in problems_of({**first, "evidence": 7}))


def test_an_entry_whose_need_its_evidence_does_not_show_is_refused() -> None:
    value = document(POSIX_ONLY)
    smoke = value["entries"][1]
    conftest = "tests/tooling/release/shell/signing/conftest.py"
    interrupt = "tests/tooling/release/shell/daemon/test_smoke_interrupt.py"
    bundle = "packaging/clients/smoke_bundle.py"
    hidden = "entries[0]: no cited line shows the stated need 'macos-bundle'"

    def problems_of(evidence: object) -> list[str]:
        return registry_problems({**value, "entries": [{**smoke, "evidence": evidence}]})

    for evidence in (f"{conftest}:14 imports pwd", f"{interrupt}:54 calls load", f"{bundle}:124 maps signal names"):
        assert problems_of(evidence) == [hidden], evidence
    for evidence in ("the macOS bundle tooling is only built on macOS", "it is loaded at :25"):
        assert problems_of(evidence) == ["entries[0]: evidence cites no path:line"], evidence
    assert problems_of(f"{conftest}:14 and :27 read pwd") == ["entries[0]: evidence cites a line without naming its file: :27", hidden]
    for cited in (f"{conftest}:99999 imports pwd", "tests/tooling/release/shell/absent.py:14 imports pwd"):
        assert problems_of(cited) == [f"entries[0]: evidence cites a line that does not exist: {cited.split(' ', 1)[0]}", hidden], cited
    for evidence in (f"{conftest}:14 imports pwd; {interrupt}:50 uses SIGHUP", f"{interrupt}:50 uses SIGHUP", f"{bundle}:123 reads SIGHUP"):
        assert problems_of(evidence) == [], evidence
    assert registry_problems({**value, "entries": [{**smoke, "needs": ["pwd"]}]}) == ["entries[0]: no cited line shows the stated need 'pwd'"]


def test_an_entry_with_a_missing_duplicate_or_misplaced_path_is_refused() -> None:
    value = document(POSIX_ONLY)
    smoke = value["entries"][1]

    def last_problem(entries: list) -> str:
        return registry_problems({**value, "entries": entries})[-1]

    assert "path 'tests/tooling/release/shell/daemon/test_absent.py' is not a file" in last_problem([{**smoke, "path": "tests/tooling/release/shell/daemon/test_absent.py"}])
    assert "is not a directory" in last_problem([{**smoke, "kind": "dir"}])
    assert "is not a file" in last_problem([{**value["entries"][0], "kind": "file", "evidence": "tests/tooling/release/shell/signing/conftest.py:14 imports pwd"}])
    assert "path 'src/hyprial/kernel/__init__.py' is not a repository-relative path under tests/" in last_problem([{**smoke, "path": "src/hyprial/kernel/__init__.py"}])
    assert "path '/tests/x' is not a repository-relative path under tests/" in last_problem([{**smoke, "path": "/tests/x"}])
    assert "path 'tests/../pyproject.toml' is not a repository-relative path under tests/" in last_problem([{**smoke, "path": "tests/../pyproject.toml"}])
    assert "path None is not a non-empty string" in last_problem([{k: v for k, v in smoke.items() if k != "path"}])
    assert "path '' is not a non-empty string" in last_problem([{**smoke, "path": ""}])
    assert "is already registered" in last_problem([*value["entries"], value["entries"][1]])
    assert "is already registered" in last_problem([*value["entries"], {**value["entries"][1], "path": value["entries"][1]["path"] + "/"}])
    assert "entries[4] is not an object" in last_problem([*value["entries"], "x"])
    assert registry_problems({**value, "entries": []}) == []


def test_expected_failures_are_absent_or_valid() -> None:
    if not EXPECTED_FAILURES.exists():
        assert collection.load_posix_only(EXPECTED_FAILURES) == {"dir": (), "file": ()}
        return
    problems = expected_failure_problems(document(EXPECTED_FAILURES), KNOWN_PROBLEMS.read_text(encoding="utf-8"), ROOT)
    assert problems == [], problems


def test_an_expected_failures_document_is_checked_against_known_problems_and_the_tree(tmp_path: Path) -> None:
    known = "## KP-001 first\n## KP-120a split\n"
    one = {"kp": "KP-001", "nodeid": "tests/a/test_b.py::test_one"}
    good = {"version": 1, "entries": [one]}
    tests = tmp_path / "tests/a"
    tests.mkdir(parents=True)
    (tests / "test_b.py").write_text("def helper():\n    pass\ndef test_one():\n    pass\nclass TestGroup:\n    def test_nested(self):\n        pass\n", encoding="utf-8")

    def problems_of(*entries: object, **top: object) -> list[str]:
        return expected_failure_problems({**good, **top, "entries": list(entries)}, known, tmp_path)

    assert problems_of(one, {"kp": "KP-120a", "nodeid": "tests/a/test_b.py::TestGroup::test_nested[x]"}) == [] and problems_of() == []
    assert expected_failure_problems([], known, tmp_path) == ["registry is not an object"]
    assert problems_of(version=2) == ["version 2 is not 1"] and expected_failure_problems({**good, "entries": {}}, known, tmp_path) == ["entries is not a list"]
    assert problems_of("x") == ["entries[0] is not an object"]
    for kp, why in (("KP-003", "is not a section of known-problems.md"), ("KP-120ab", "is not a known-problem id"), (None, "is not a known-problem id"), (7, "is not a known-problem id")):
        assert problems_of({**one, "kp": kp}) == [f"entries[0]: kp {kp!r} {why}"]
    for nodeid, problem in (("tests/a/test_b.py", "nodeid 'tests/a/test_b.py' is not a file path followed by :: and a test name"), ("tests/a/test_absent.py::test_one", "nodeid file 'tests/a/test_absent.py' does not exist"), ("tests/a/test_b.py::test_missing", "nodeid names 'test_missing', which is not defined in the file"), ("tests/a/test_b.py::helper", "nodeid names 'helper', which is not a test function"), ("tests/a/test_b.py::TestGroup", "nodeid names 'TestGroup', which is not a test function"), ("tests/a/test_b.py::TestGroup::Nope", "nodeid names 'Nope', which is not a test function"), ("tests/a/test_b.py::TestGroup::test_one", "nodeid names 'test_one', which is not defined in class 'TestGroup'"), ("tests/a/test_b.py::test_one::more", "nodeid names 'test_one', which is not a test class"), ("tests/a/test_b.py::TestGroup[p]::test_nested", "nodeid 'tests/a/test_b.py::TestGroup[p]::test_nested' parametrizes a class, not a test"), (5, "nodeid 5 is not a string")):
        assert problems_of({**one, "nodeid": nodeid}) == [f"entries[0]: {problem}"], nodeid
    assert problems_of(one, {**one, "kp": "KP-120a"}) == ["entries[1]: nodeid 'tests/a/test_b.py::test_one' is already expected"]


def test_every_known_problem_id_the_gate_accepts_is_a_section_of_the_document() -> None:
    ids = known_problem_ids(KNOWN_PROBLEMS.read_text(encoding="utf-8"))
    assert len(ids) >= 136 and {"KP-001", "KP-120a", "KP-120b"} <= ids and all(re.fullmatch(KNOWN_ID, kp) for kp in ids)
    assert known_problem_ids("## KP-120a split") == {"KP-120a"} and known_problem_ids("## KP-120") == {"KP-120"}
    assert known_problem_ids("## KP-001 x\n### KP-002 y\n## KP-003a-bad\n## KP-004! more\n## KP-005-z\n## KP-006ab x\n##KP-007\n") == {"KP-001"}


def test_the_marker_is_declared_to_pytest() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["markers"]
    assert [marker for marker in declared if marker.startswith("posix_only")] == [MARKER_DECLARATION]


def test_the_plugin_and_the_gate_read_the_same_registry_and_an_unreadable_one_is_empty(tmp_path: Path) -> None:
    value = document(POSIX_ONLY)
    assert collection.load_posix_only(POSIX_ONLY) == {"dir": tuple(path for path, kind in registered(value) if kind == "dir"), "file": tuple(path for path, kind in registered(value) if kind == "file")}
    path = tmp_path / "registry.json"
    for text in ("{not json", "[]", '{"version": 1}', "null", '{"entries": [{"path": 1, "kind": "file"}, {"path": "tests/x", "kind": "module"}, "x"]}', '{"entries": 5}'):
        path.write_text(text, encoding="utf-8")
        assert collection.load_posix_only(path) == {"dir": (), "file": ()}, text
    assert collection.load_posix_only(tmp_path / "absent.json") == {"dir": (), "file": ()}
    path.write_text('{"entries": [{"path": "tests/a/", "kind": "dir"}, {"path": "tests/b/", "kind": "file"}]}', encoding="utf-8")
    assert collection.load_posix_only(path) == {"dir": ("tests/a",), "file": ("tests/b",)}


def test_a_registered_path_may_not_appear_in_a_windows_test_list(tmp_path: Path) -> None:
    value = document(POSIX_ONLY)
    lists = tmp_path / "scripts/ci/windows"
    lists.mkdir(parents=True)
    assert windows_test_lists(lists) == [] and list_problems(value, lists, root=tmp_path) == []
    (lists / "tier9-tests.txt").write_text("# comment\n\ntests/kernel/test_a.py\n tests/tooling/release/shell/signing/bundle/test_x.py \ntests/tooling/release/shell/daemon/test_smoke_daemon.py\ntests/tooling/release/shell/signing\n", encoding="utf-8")
    (lists / "tier9-rejected.tsv").write_text("path\treason\ntests/tooling/release/shell/daemon/test_smoke_cleanup.py\twhy\t12\n", encoding="utf-8")
    (lists / "notes.txt").write_text("tests/tooling/release/shell/signing\n", encoding="utf-8")
    assert [path.name for path in windows_test_lists(lists)] == ["tier9-rejected.tsv", "tier9-tests.txt"] and listed_test_paths(lists / "tier9-rejected.tsv") == ["path", "tests/tooling/release/shell/daemon/test_smoke_cleanup.py"]
    assert list_problems(value, lists, root=tmp_path) == [
        "scripts/ci/windows/tier9-rejected.tsv:2 names the registered file 'tests/tooling/release/shell/daemon/test_smoke_cleanup.py'",
        "scripts/ci/windows/tier9-tests.txt:4 names the registered dir 'tests/tooling/release/shell/signing'",
        "scripts/ci/windows/tier9-tests.txt:5 names the registered file 'tests/tooling/release/shell/daemon/test_smoke_daemon.py'",
        "scripts/ci/windows/tier9-tests.txt:6 names the registered dir 'tests/tooling/release/shell/signing'",
    ]
    assert covers("tests/a", "dir", "tests/a/x.py") and not covers("tests/a", "dir", "tests/ax/x.py") and covers("tests/a/b.py", "file", "tests/a/b.py") and not covers("tests/a/b.py", "file", "tests/a/c.py")
    (lists / "tier9-tests.txt").write_text("tests/kernel/test_a.py\n", encoding="utf-8")
    (lists / "tier9-rejected.tsv").write_text("path\treason\n", encoding="utf-8")
    assert list_problems(value, lists, root=tmp_path) == [] and list_problems({"entries": []}, lists, root=tmp_path) == []
