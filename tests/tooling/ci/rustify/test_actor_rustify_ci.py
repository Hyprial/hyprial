"""RED/GREEN contracts for the actor-rustify CI-I change-surface audit."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/audit_actor_rustify_ci.py"
SPEC = importlib.util.spec_from_file_location("actor_rustify_ci", SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)

NATIVE_PREFIX = "from .resident_metadata import HISTORICAL_ROWS as HISTORICAL_ROWS\n"


def native(live: str, assertion: str = "assert True") -> str:
    rows = "\n".join(f'    "{path}": ("resident", ("origin",)),' for path in live.split())
    return f"{NATIVE_PREFIX}NATIVE_ORIGINS = {{\n{rows}\n}}\n{assertion}\n"


def history(rows: str = "") -> str:
    body = ", ".join(f'"{path}"' for path in rows.split())
    return f'"""Doc."""\nHISTORICAL_ROWS = frozenset({{{body}}})\n' if body else '"""Doc."""\nHISTORICAL_ROWS = frozenset()\n'


PART_A = AUDIT.PARTS_DIR + "native_origins_a.py"
PART_B = AUDIT.PARTS_DIR + "native_origins_b_h.py"
UNION_MODULE = NATIVE_PREFIX + "NATIVE_ORIGINS = merge_parts(load_parts())\n"


def part(rows: str, doc: str = "Part.", tail: str = "") -> str:
    body = "\n".join(f'    "{path}": ("resident", ("origin",)),' for path in rows.split())
    return f'"""{doc}"""\nNATIVE_ORIGINS_PART = {{\n{body}\n}}\n{tail}'


def as_part(rows: str) -> str:
    return rows if rows.startswith('"""') else part(rows)


def parted(base: dict[str, str], head: dict[str, str], base_native=UNION_MODULE, head_native=UNION_MODULE) -> list[str]:
    return AUDIT.audit_registries(
        base_native, head_native, history(), history(),
        {path: as_part(rows) for path, rows in base.items()},
        {path: as_part(rows) for path, rows in head.items()},
    )


def relocate(head: dict[str, str], base_live: str = "d/a.py e/b.py f/c.py") -> list[str]:
    return AUDIT.audit_registries(
        native(base_live), UNION_MODULE, history(), history(), None,
        {path: as_part(rows) for path, rows in head.items()},
    )


def registries(base_live: str, head_live: str, base_hist: str = "", head_hist: str = "") -> list[str]:
    return AUDIT.audit_registries(native(base_live), native(head_live), history(base_hist), history(head_hist))


def test_append_only_native_entry_is_green():
    assert registries("d/a.py", "d/a.py d/b.py") == []


def test_changed_deleted_or_reordered_native_entry_is_red():
    errors = AUDIT.audit_registries(
        native("d/a.py"), native("d/a.py").replace('("origin",)', '("changed",)'), history(), history()
    )
    assert any("changed NATIVE_ORIGINS entry 'd/a.py'" in error for error in errors)
    assert any("deleted NATIVE_ORIGINS entry 'd/a.py'" in error for error in registries("d/a.py", ""))
    assert any("reordered NATIVE_ORIGINS" in error for error in registries("d/a.py d/b.py", "d/b.py d/a.py"))


@pytest.mark.parametrize(
    "suffix",
    (
        "NATIVE_ORIGINS.update({})\n",
        "NATIVE_ORIGINS['x'] = ()\n",
        "del NATIVE_ORIGINS['d/a.py']\n",
        "HISTORICAL_ROWS = frozenset()\n",
        'globals()["NATIVE_ORIGINS"] = {}\n',
        "def late():\n    global NATIVE_ORIGINS\n    NATIVE_ORIGINS = {}\n",
    ),
)
def test_any_code_change_outside_the_literal_is_red(suffix):
    errors = AUDIT.audit_registries(native("d/a.py"), native("d/a.py") + suffix, history(), history())
    assert errors, suffix


def test_assertion_change_is_red_even_with_registry_unchanged():
    errors = AUDIT.audit_registries(
        native("d/a.py", "assert one()"), native("d/a.py", "assert two()"), history(), history()
    )
    assert any("assertion removal" in error for error in errors)


def test_second_module_binding_is_a_parse_failure():
    errors = AUDIT.audit_registries(native("d/a.py"), native("d/a.py") + "NATIVE_ORIGINS = {}\n", history(), history())
    assert any("registry parse failure" in error for error in errors)


def test_retiring_a_live_row_byte_identically_is_green():
    assert registries("d/old.py", "d/old.py", "", "d/old.py") == []


def test_history_must_be_a_byte_identical_unchanged_live_row():
    single = history("d/old.py").replace('"d/old.py"', "'d/old.py'")
    errors = AUDIT.audit_registries(native("d/old.py"), native("d/old.py"), history(), single)
    assert any("not byte-identical" in error for error in errors)
    errors = registries("d/live.py", "d/live.py", "", "d/unrelated.py")
    assert any("no unchanged live NATIVE_ORIGINS row" in error for error in errors)


def test_history_deletion_change_or_reorder_is_red():
    assert any("deleted HISTORICAL_ROWS" in error for error in registries("d/o.py", "d/o.py", "d/o.py", ""))
    assert any(
        "reordered HISTORICAL_ROWS" in error
        for error in registries("d/a.py d/b.py", "d/a.py d/b.py", "d/a.py d/b.py", "d/b.py d/a.py")
    )
    errors = AUDIT.audit_registries(
        native("d/o.py"), native("d/o.py"), history("d/o.py"), history("d/o.py") + "X = 1\n"
    )
    assert any("code outside HISTORICAL_ROWS" in error for error in errors)


def test_new_live_row_cannot_reuse_a_historical_row():
    assert any("reuses a historical row" in error for error in registries("", "d/o.py", "d/o.py", "d/o.py"))


def test_registry_config_is_append_only():
    base = '{"version": 1, "registries": [{"path": "a", "symbol": "X"}]}'
    added = '{"version": 1, "registries": [{"path": "a", "symbol": "X"}, {"path": "b", "symbol": "Y"}]}'
    assert AUDIT.audit_registry_config(base, added) == []
    assert "'symbol': 'X'" in AUDIT.audit_registry_config(base, base.replace('"X"', '"Z"'))[0]
    assert "deleted registry entry" in AUDIT.audit_registry_config(base, '{"version": 1, "registries": []}')[0]
    assert "non-registry policy" in AUDIT.audit_registry_config(base, base.replace('"version": 1', '"version": 2'))[0]


def test_relocating_the_identical_rows_into_parts_is_green():
    assert relocate({PART_A: "d/a.py", PART_B: "e/b.py f/c.py"}) == []
    single_quoted = native("d/a.py").replace('"d/a.py"', "'d/a.py'")
    moved = part("d/a.py").replace('"d/a.py"', "'d/a.py'")
    assert AUDIT.audit_registries(single_quoted, UNION_MODULE, history(), history(), None, {PART_A: moved}) == []


def test_relocation_that_changes_drops_adds_or_reorders_a_row_is_red():
    changed = part("e/b.py f/c.py").replace('("origin",)', '("changed",)', 1)
    assert any("relocation changed NATIVE_ORIGINS entry 'e/b.py'" in e for e in relocate({PART_A: "d/a.py", PART_B: changed}))
    assert any("relocation deleted NATIVE_ORIGINS entry 'f/c.py'" in e for e in relocate({PART_A: "d/a.py", PART_B: "e/b.py"}))
    assert any("relocation added NATIVE_ORIGINS entry 'z/new.py'" in e for e in relocate({PART_A: "d/a.py", PART_B: "e/b.py z/new.py f/c.py"}))
    assert any("relocation added NATIVE_ORIGINS entry 'z/new.py'" in e for e in relocate({PART_A: "z/new.py d/a.py", PART_B: "e/b.py f/c.py"}))
    assert any("relocation reordered" in e for e in relocate({PART_A: "d/a.py", PART_B: "f/c.py e/b.py"}))
    quoted = part("d/a.py").replace('"d/a.py"', "'d/a.py'")
    assert any("relocation changed NATIVE_ORIGINS entry 'd/a.py'" in e for e in relocate({PART_A: quoted, PART_B: "e/b.py f/c.py"}))


def test_relocation_part_must_be_a_docstring_and_one_literal_only():
    errors = relocate({PART_A: "d/a.py", PART_B: part("e/b.py f/c.py", tail="NATIVE_ORIGINS_PART.clear()\n")})
    assert any("docstring plus one NATIVE_ORIGINS_PART assignment" in e for e in errors)


def test_relocation_never_moves_the_inventory_back_into_one_literal():
    errors = AUDIT.audit_registries(UNION_MODULE, native("d/a.py"), history(), history(), {PART_A: part("d/a.py")}, None)
    assert any("back into one literal" in e for e in errors)


def test_duplicate_key_across_parts_is_red():
    errors = parted({PART_A: "d/a.py"}, {PART_A: "d/a.py", PART_B: "d/a.py"})
    assert any("duplicate NATIVE_ORIGINS entry 'd/a.py'" in e for e in errors)
    assert any("duplicate NATIVE_ORIGINS entry" in e for e in relocate({PART_A: "d/a.py e/b.py", PART_B: "e/b.py f/c.py"}))


def test_part_rows_are_append_only_over_the_union():
    base = {PART_A: "d/a.py d/b.py", PART_B: "e/c.py"}
    assert parted(base, {PART_A: "d/a.py d/b.py d/new.py", PART_B: "e/c.py"}) == []
    assert parted(base, {PART_A: "d/a.py d/b.py", PART_B: "e/c.py e/new.py", AUDIT.PARTS_DIR + "native_origins_i_o.py": "j/n.py"}) == []
    changed = part("d/a.py d/b.py").replace('("origin",)', '("changed",)', 1)
    assert any("changed NATIVE_ORIGINS entry 'd/a.py'" in e for e in parted(base, {PART_A: changed, PART_B: "e/c.py"}))
    assert any("deleted NATIVE_ORIGINS entry 'd/b.py'" in e for e in parted(base, {PART_A: "d/a.py", PART_B: "e/c.py"}))
    assert any("reordered NATIVE_ORIGINS" in e for e in parted(base, {PART_A: "d/b.py d/a.py", PART_B: "e/c.py"}))
    assert any("deleted NATIVE_ORIGINS entry 'e/c.py'" in e for e in parted(base, {PART_A: "d/a.py d/b.py e/c.py", PART_B: ""}))
    assert any("part removed" in e for e in parted(base, {PART_A: "d/a.py d/b.py"}))


def test_a_new_part_must_be_pure_data_and_the_union_module_is_frozen():
    base = {PART_A: "d/a.py"}
    bad = part("j/n.py", tail="import os\n")
    assert any("docstring plus one" in e for e in parted(base, {PART_A: "d/a.py", AUDIT.PARTS_DIR + "native_origins_i_o.py": bad}))
    assert any("code outside NATIVE_ORIGINS" in e for e in parted(base, base, head_native=UNION_MODULE + "NATIVE_ORIGINS.clear()\n"))


def test_registry_config_relocation_must_register_every_part_in_place():
    parts = (PART_A, PART_B)
    legacy = {"path": AUDIT.NATIVE_PATH, "symbol": "NATIVE_ORIGINS", "kind": "k", "note": "old"}
    other = {"path": "x", "symbol": "Y", "kind": "k"}
    entries = [{"path": p, "symbol": AUDIT.PART_SYMBOL, "kind": "k", "note": "new"} for p in parts]

    def config(rows):
        return json.dumps({"version": 1, "registries": rows})

    base = config([legacy, other])
    assert AUDIT.audit_registry_config(base, config([*entries, other]), parts) == []
    assert AUDIT.audit_registry_config(base, config([*entries, other, {"path": "z", "symbol": "Z"}]), parts) == []
    assert AUDIT.audit_registry_config(base, config([*entries[:1], other]), parts)
    assert AUDIT.audit_registry_config(base, config([*entries, other]))
    assert AUDIT.audit_registry_config(base, config([*entries, {**other, "symbol": "W"}]), parts)
    assert AUDIT.audit_registry_config(config(entries), config(entries[:1]), parts)
    assert AUDIT.audit_parts_registered(config(entries), parts) == []
    assert "not registered" in AUDIT.audit_parts_registered(config(entries[:1]), parts)[0]


def test_workflow_deletions_are_red_but_additions_are_green():
    assert AUDIT.additions_only("@@ -1 +1,2 @@\n old\n+new\n", "w.yml") == []
    assert "removed workflow line 'old'" in AUDIT.additions_only("@@ -1,2 +1 @@\n-old\n keep\n", "w.yml")[0]


@pytest.mark.parametrize(
    ("head_ref", "paths", "audited"),
    (
        ("release/v0.5.5.4", ["crates/x/src/lib.rs", ".github/workflows/tests.yml"], True),
        ("release/v0.5.5.4", ["src/hyprial/kernel/x.py"], False),
        ("ci/rustify-native-job", [".github/workflows/tests.yml"], True),
        ("infra-ops/rustify-engine", [], True),
        ("refactor/actor-wheel-native", ["scripts/build/build-wheel.sh"], True),
        ("refactor/actor-port-p2", ["conformance/actor_engine/cases/x.py"], True),
        ("feat/unrelated", ["crates/hyprial-core/Cargo.toml"], True),
        ("refactor/actor-port-p2", ["src/hyprial/kernel/x.py", ".github/workflows/tests.yml"], False),
        ("intg/cell-model", ["src/hyprial/daemon/x.py"], False),
    ),
)
def test_scope_is_rustify_prefixes_and_paths_not_base_name(head_ref, paths, audited):
    assert AUDIT.decide_scope(head_ref, paths).audited is audited


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "intg/actor-rustify")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "CI")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _write(tmp_path, AUDIT.NATIVE_PATH, native("d/a.py"))
    _write(tmp_path, AUDIT.HISTORY_PATH, history())
    _write(tmp_path, AUDIT.UNION_PATH, '{"version": 1, "registries": []}\n')
    _write(tmp_path, AUDIT.HYGIENE_PATH, "RULES = 1\n")
    _write(tmp_path, AUDIT.AUDIT_PATH, SCRIPT.read_text(encoding="utf-8"))
    _write(tmp_path, AUDIT.WORKFLOW_PATHS[0], "jobs:\n  a: 1\n  b: 2\n")
    _commit(tmp_path, "base")
    return tmp_path


def _branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-q", "-b", name, "intg/actor-rustify")


def test_lagging_pr_is_not_blamed_for_base_progress(repo):
    _branch(repo, "ci/rustify-lag")
    _write(repo, AUDIT.WORKFLOW_PATHS[0], "jobs:\n  a: 1\n  b: 2\n  rust: 3\n")
    head = _commit(repo, "add rust job")
    _git(repo, "checkout", "-q", "intg/actor-rustify")
    _write(repo, AUDIT.WORKFLOW_PATHS[0], "jobs:\n  a: 1\n  b: 2\n  c: 4\n")
    _write(repo, AUDIT.HYGIENE_PATH, "RULES = 2\n")
    _commit(repo, "base moves on")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-lag", repo)
    assert scope.audited and errors == []
    two_dot = _git(repo, "diff", "--unified=0", "intg/actor-rustify", head, "--", AUDIT.WORKFLOW_PATHS[0])
    assert AUDIT.additions_only(two_dot, AUDIT.WORKFLOW_PATHS[0])
    assert _git(repo, "diff", "intg/actor-rustify", head, "--", AUDIT.HYGIENE_PATH)


@pytest.mark.parametrize(
    ("rel", "text", "needle"),
    (
        (AUDIT.WORKFLOW_PATHS[0], "jobs:\n  a: 1\n", "removed workflow line 'b: 2'"),
        (AUDIT.HYGIENE_PATH, "RULES = 0\n", "check_pr_hygiene.py"),
        (AUDIT.AUDIT_PATH, "print('weakened')\n", "byte-for-byte unchanged"),
        (AUDIT.NATIVE_PATH, native("d/a.py", "pass"), "code outside NATIVE_ORIGINS"),
        (AUDIT.UNION_PATH, '{"version": 2, "registries": []}\n', "non-registry policy"),
    ),
)
def test_own_weakening_is_red(repo, rel, text, needle):
    _branch(repo, "ci/rustify-weaken")
    _write(repo, rel, text)
    head = _commit(repo, "weaken")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-weaken", repo)
    assert scope.audited and any(needle in error for error in errors), errors


def test_release_preparation_cannot_bypass_rustify_workflow_guards(repo):
    _git(repo, "checkout", "-q", "-b", "release/v9", "intg/actor-rustify")
    _write(repo, AUDIT.WORKFLOW_PATHS[0], "jobs:\n  a: 1\n")
    _write(repo, "crates/x/Cargo.toml", "[package]\n")
    head = _commit(repo, "release work")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "release/v9", repo)
    assert scope.audited
    assert any(AUDIT.WORKFLOW_PATHS[0] in error for error in errors), errors
    _branch(repo, "refactor/actor-p5")
    _write(repo, "src/x.py", "X = 1\n")
    head = _commit(repo, "python only")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "refactor/actor-p5", repo)
    assert not scope.audited and errors == []


def test_git_relocation_into_parts_is_audited_exactly(repo):
    _branch(repo, "ci/rustify-relocate")
    _write(repo, AUDIT.NATIVE_PATH, UNION_MODULE)
    _write(repo, PART_A, part("d/a.py"))
    entries = [{"path": PART_A, "symbol": AUDIT.PART_SYMBOL, "kind": "k"}]
    _write(repo, AUDIT.UNION_PATH, json.dumps({"version": 1, "registries": entries}))
    head = _commit(repo, "relocate")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-relocate", repo)
    assert scope.audited and errors == [], errors
    _write(repo, PART_A, part("d/a.py x/new.py"))
    head = _commit(repo, "relocation also adds a row")
    _, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-relocate", repo)
    assert any("relocation added NATIVE_ORIGINS entry 'x/new.py'" in e for e in errors), errors
    _write(repo, PART_A, part("d/a.py"))
    _write(repo, AUDIT.UNION_PATH, '{"version": 1, "registries": []}\n')
    head = _commit(repo, "unregistered part")
    _, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-relocate", repo)
    assert any("part " + PART_A + " is not registered" in e for e in errors), errors


def _relocated_base(repo: Path) -> None:
    _write(repo, AUDIT.NATIVE_PATH, UNION_MODULE)
    _write(repo, PART_A, part("d/a.py"))
    entries = [{"path": PART_A, "symbol": AUDIT.PART_SYMBOL, "kind": "k"}]
    _write(repo, AUDIT.UNION_PATH, json.dumps({"version": 1, "registries": entries}))
    _commit(repo, "relocate on the base branch")


@pytest.mark.parametrize(
    "rel",
    [
        "native_origins_s_z/__init__.py",
        "__pycache__/native_origins_s_z.cpython-312.pyc",
        "__pycache__/native_origins_a.cpython-312.pyc",
        "native_origins_a.cpython-312-x86_64-linux-gnu.so",
        "native_origins_a.pyi",
        "native_origins_s_z.py.orig",
    ],
)
def test_an_unregistered_file_under_the_parts_directory_is_red(repo, rel):
    _relocated_base(repo)
    _branch(repo, "ci/rustify-shadow")
    _write(repo, AUDIT.PARTS_DIR + rel, "NATIVE_ORIGINS_PART = {}\n")
    head = _commit(repo, "plant a shadow")
    _, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-shadow", repo)
    assert any(AUDIT.PARTS_DIR + rel in e and "may live under" in e for e in errors), errors


def test_appending_a_row_to_a_registered_part_stays_green_and_the_package_init_is_allowed(repo):
    _relocated_base(repo)
    _branch(repo, "ci/rustify-append")
    _write(repo, PART_A, part("d/a.py d/b.py"))
    head = _commit(repo, "append a row")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-append", repo)
    assert scope.audited and errors == [], errors


def test_deleting_the_audit_is_red(repo):
    _branch(repo, "ci/rustify-drop")
    (repo / AUDIT.AUDIT_PATH).unlink()
    head = _commit(repo, "drop audit")
    _, errors = AUDIT.run("intg/actor-rustify", head, "ci/rustify-drop", repo)
    assert any("byte-for-byte unchanged" in error for error in errors)


def test_pr_branched_before_the_audit_landed_is_not_blamed(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "intg/actor-rustify")
    for key, value in (("user.email", "ci@example.invalid"), ("user.name", "CI"), ("commit.gpgsign", "false")):
        _git(tmp_path, "config", key, value)
    _write(tmp_path, "README", "x\n")
    _commit(tmp_path, "pre-audit base")
    _git(tmp_path, "checkout", "-q", "-b", "refactor/actor-wheel-native")
    _write(tmp_path, "scripts/build/build-wheel.sh", "echo native\n")
    head = _commit(tmp_path, "native stage")
    _git(tmp_path, "checkout", "-q", "intg/actor-rustify")
    _write(tmp_path, AUDIT.AUDIT_PATH, SCRIPT.read_text(encoding="utf-8"))
    _commit(tmp_path, "CI-I lands")
    scope, errors = AUDIT.run("intg/actor-rustify", head, "refactor/actor-wheel-native", tmp_path)
    assert scope.audited and errors == []


def test_unresolved_refs_are_not_evaluable(repo):
    assert AUDIT.run("missing-base", "HEAD", "ci/rustify-x", repo)[1][0].startswith("NOT EVALUABLE:")
    assert AUDIT.run("intg/actor-rustify", "0" * 40, "ci/rustify-x", repo)[1][0].startswith("NOT EVALUABLE:")


def test_cli_exit_codes(repo):
    def code(*args: str) -> int:
        return subprocess.run(
            ["python3", str(SCRIPT), "--root", str(repo), *args], capture_output=True, text=True
        ).returncode

    assert code("--base-ref", "intg/actor-rustify", "--head", "HEAD", "--head-ref-name", "ci/rustify-x") == 0
    assert code("--base-ref", "nope", "--head", "HEAD", "--head-ref-name", "ci/rustify-x") == 2
    _branch(repo, "ci/rustify-red")
    _write(repo, AUDIT.HYGIENE_PATH, "RULES = 0\n")
    _commit(repo, "red")
    assert code("--base-ref", "intg/actor-rustify", "--head", "HEAD", "--head-ref-name", "ci/rustify-red") == 1


def test_trusted_temp_copy_uses_the_explicit_repository_root(repo):
    trusted = repo / "ci-i-audit-test.py"
    trusted.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(trusted),
            "--root",
            str(repo),
            "--base-ref",
            "intg/actor-rustify",
            "--head",
            "HEAD",
            "--head-ref-name",
            "fix/dev-closeout",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OUT OF SCOPE" in completed.stdout


def test_live_tree_registries_parse_as_the_closed_shape():
    native_source = (ROOT / AUDIT.NATIVE_PATH).read_text(encoding="utf-8")
    history_source = (ROOT / AUDIT.HISTORY_PATH).read_text(encoding="utf-8")
    part_sources = {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / AUDIT.PARTS_DIR).glob("native_origins_*.py"))
    }
    assert part_sources and all(AUDIT.is_part_path(path) for path in part_sources)
    assert AUDIT.audit_registries(
        native_source, native_source, history_source, history_source, part_sources, part_sources
    ) == []
    inventory = AUDIT.parse_inventory(native_source, part_sources)
    assert all(literal.order for literal in inventory.parts.values())
    registered = json.loads((ROOT / AUDIT.UNION_PATH).read_text(encoding="utf-8"))
    assert AUDIT.audit_parts_registered(json.dumps(registered), tuple(part_sources)) == []
    assert AUDIT.parse_literal(history_source, "HISTORICAL_ROWS", "set").order
