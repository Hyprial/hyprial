"""Executable contract for the Node platform-idiom gate.

``scripts/ci/check_node_platform_idioms.py`` scans first-party JS/TS files under
``src/hyprial`` over a real JS/TS token stream (stripping single-line and
multi-line comments, strings, regex literals and raw template text while
tokenising ``${...}`` interpolations) and enforces N001/N002/N003 with an
only-out baseline and a one-time first-seed ``--base-ref`` check.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ci" / "check_node_platform_idioms.py"
SPEC = importlib.util.spec_from_file_location("check_node_platform_idioms", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

PROBE = "src/hyprial/daemon/impl/harnesses/pi/resources/probe.ts"


def rules(source: str) -> list[str]:
    return [row["rule"] for row in GATE.scan_source(source, PROBE)]


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("if ((metadata.mode & 0o077) !== 0) return null;\n", "N001"),
        ("const open = (stat.mode & 0o77) === 0;\n", "N001"),
        ("const leaked = 0o077 & mode;\n", "N001"),
        ("const upper = mode & 0O077;\n", "N001"),
        ("const sep = mode & 0o0_77;\n", "N001"),
        ("const revSep = 0O0_77 & stat.mode;\n", "N001"),
        ("if ((stat.mode & 0o777) !== 0o600) return false;\n", "N001"),
        ("if (stat.mode !== 0o700) return false;\n", "N001"),
        ("if (0o600 === metadata.mode) return true;\n", "N001"),
        ("process.kill(-child.pid, 'SIGTERM');\n", "N002"),
        ("process . kill( -pid, 9);\n", "N002"),
        ("process?.kill(-pid);\n", "N002"),
        ("process?.kill?.(-pid, 'SIGKILL');\n", "N002"),
        ("process['kill'](-pid);\n", "N002"),
        ("process?.[\"kill\"]?.((-pid));\n", "N002"),
        ("const kill = process.kill;\nkill(-pid);\n", "N002"),
        ("const { kill } = process;\nkill(-pid);\n", "N002"),
        ("const { kill: killGroup } = process;\nkillGroup(-child.pid);\n", "N002"),
        ("const proc = process;\nproc.kill(-pid);\n", "N002"),
        ("return `${process.env.HOME}/.hyprial`;\n", "N003"),
        ("return `prefix ${flag ? `nested ${process.env.HOME}` : 'ok'} suffix`;\n", "N003"),
        ("const home = env.HOME ?? '';\n", "N003"),
        ("const home = process.env['HOME'];\n", "N003"),
        ("const home = process?.env?.HOME;\n", "N003"),
        ("const home = process['env'].HOME;\n", "N003"),
        ("const home = process[\"env\"][\"HOME\"];\n", "N003"),
        ("const home = process[`env`][`HOME`];\n", "N003"),
        ("const { HOME } = process.env;\n", "N003"),
        ("const { HOME: homeDir = '' } = env;\n", "N003"),
        ("const { env: { HOME } } = process;\n", "N003"),
        ("const e = process.env;\nconst home = e.HOME;\n", "N003"),
        ("if (process.env.HOME == expected) return true;\n", "N003"),
        ("if (process.env.HOME === expected) return true;\n", "N003"),
        ("if (process.env['HOME'] !== expected) return false;\n", "N003"),
        ("target.HOME = process.env.HOME;\n", "N003"),
        ("process.env.HOME = process.env.HOME ?? '/tmp';\n", "N003"),
        ("delete cleanup(process.env.HOME);\n", "N003"),
        ("delete process.env.HOME.sub;\n", "N003"),
        ("const x = process.env.HOME\n++counter;\n", "N003"),
        ("counter++\nconst x = process.env.HOME;\n", "N003"),
        ("const o = { HOME: process.env.HOME };\n", "N003"),
        ("const o = { a: { b: process.env.HOME } };\n", "N003"),
        ("const arr = [process.env.HOME];\n", "N003"),
        ("({ [process.env.HOME]: 1 } = src);\n", "N003"),
        ("({ [process.env.HOME]: target } = src);\n", "N003"),
        ("({ [process.env['HOME']]: target } = src);\n", "N003"),
        ("({ [process.env.HOME]: process.env.HOME } = src);\n", "N003"),
        ("class C { [process.env.HOME] = 1; }\n", "N003"),
        ("obj[process.env.HOME] = 1;\n", "N003"),
        ("[a[process.env.HOME]] = arr;\n", "N003"),
        ("fn()[process.env.HOME] = 1;\n", "N003"),
        ("[a = process.env.HOME] = arr;\n", "N003"),
        ("({ a = process.env.HOME } = src);\n", "N003"),
        ("({ a: b = process.env.HOME } = src);\n", "N003"),
        ("({ HOME: process.env.HOME = process.env.HOME } = src);\n", "N003"),
        ("({ a = { HOME: process.env.HOME } } = src);\n", "N003"),
        ("[a = [process.env.HOME]] = arr;\n", "N003"),
        ("({ a: cond ? fallback : process.env.HOME });\n", "N003"),
        ("if (process.env.HOME in obj) return true;\n", "N003"),
        ("const hasHome = (process.env.HOME in obj);\n", "N003"),
        ("for ((process.env.HOME in obj); cond; step()) {}\n", "N003"),
        ("for (const key in process.env.HOME) {}\n", "N003"),
        ("for (const ch of process.env.HOME) {}\n", "N003"),
        ("for await (const ch of [process.env.HOME]) {}\n", "N003"),
    ],
)
def test_each_posix_idiom_and_syntax_variant_is_found(source: str, rule: str) -> None:
    assert rules(source) == [rule], (source, GATE.scan_source(source, PROBE))


@pytest.mark.parametrize(
    "source",
    [
        "process.umask(0o077);\n",
        "chmodSync(path, 0o700);\n",
        "mkdirSync(parent, { recursive: true, mode: 0o700 });\n",
        "writeFileSync(target, data, { flag: 'wx', mode: 0o600 });\n",
        "process.kill(child.pid, 'SIGTERM');\n",
        "process.kill(basePid - offset, 'SIGTERM');\n",
        "child.kill('SIGKILL');\n",
        "function kill(x: number) { return x; }\nkill(-1);\n",
        "const home = os.homedir();\n",
        "const home = process.env.HOMEBREW_PREFIX;\n",
        "const root = process.env.HYPRIAL_HOME;\n",
        "process.env.HOME = options.homeRoot;\n",
        "process.env['HOME'] = options.homeRoot;\n",
        "process[\"env\"][\"HOME\"] = options.homeRoot;\n",
        "process?.env?.HOME = options.homeRoot;\n",
        "(process.env.HOME) = options.homeRoot;\n",
        "process.env.HOME += '/sub';\n",
        "process.env.HOME &&= nextHome;\n",
        "process.env.HOME ||= fallback;\n",
        "process.env.HOME ??= fallback;\n",
        "process.env.HOME++;\n",
        "process.env.HOME--;\n",
        "++process.env.HOME;\n",
        "--process.env.HOME;\n",
        "if (flag) ++process.env.HOME;\n",
        "delete process.env.HOME;\n",
        "delete process.env['HOME'];\n",
        "delete process?.env?.HOME;\n",
        "delete process?.env?.['HOME'];\n",
        "delete (process.env.HOME);\n",
        "const e = process.env;\ne.HOME = options.homeRoot;\n",
        "const e = process.env;\ne['HOME'] = options.homeRoot;\n",
        "const e = process.env;\ne.HOME ??= fallback;\n",
        "const e = process.env;\ndelete e.HOME;\n",
        "env.HOME = options.homeRoot;\n",
        "delete env.HOME;\n",
        "for (process.env.HOME in obj) {}\n",
        "for (process.env['HOME'] of iter) {}\n",
        "for (process[\"env\"][\"HOME\"] in obj) {}\n",
        "for (process?.env?.HOME of iter) {}\n",
        "for ((process.env.HOME) in obj) {}\n",
        "for await (process.env.HOME of gen) {}\n",
        "for await (process.env['HOME'] of gen) {}\n",
        "for (env.HOME of iter) {}\n",
        "const e = process.env;\nfor (e.HOME in obj) {}\n",
        "const e = process.env;\nfor (e['HOME'] of iter) {}\n",
        "for ([process.env.HOME] of iter) {}\n",
        "for ({ HOME: process.env.HOME } of iter) {}\n",
        "for await ({ HOME: process.env.HOME } of gen) {}\n",
        "[process.env.HOME] = arr;\n",
        "[, process.env.HOME] = arr;\n",
        "[a, process.env['HOME'] = defVal] = arr;\n",
        "[a, process?.env?.HOME] = arr;\n",
        "[(process.env.HOME)] = arr;\n",
        "([process.env.HOME] = arr);\n",
        "[...process.env.HOME] = arr;\n",
        "[env.HOME] = arr;\n",
        "const e = process.env;\n[e.HOME] = arr;\n",
        "if (cond) [process.env.HOME] = arr;\n",
        "({ HOME: process.env.HOME } = src);\n",
        "({ 'HOME': process.env['HOME'] } = src);\n",
        "({ [key]: process.env.HOME } = src);\n",
        "({ HOME: process?.env?.HOME } = src);\n",
        "({ HOME: (process.env.HOME) } = src);\n",
        "(({ HOME: process.env.HOME }) = src);\n",
        "({ a: { b: process.env.HOME = defVal } } = src);\n",
        "({ a: { b: process.env.HOME } } = src);\n",
        "({ a: { b: process.env.HOME } = defObj } = src);\n",
        "({ a: [b, { c: process.env['HOME'] }] } = src);\n",
        "({ ...process.env.HOME } = src);\n",
        "({ HOME: env.HOME } = src);\n",
        "const e = process.env;\n({ HOME: e.HOME } = src);\n",
        "// legacy: (mode & 0o077) and process.env.HOME\n",
        "const safe = 1; // process.kill(-pid) and mode & 0o077 and process.env.HOME\n",
        "/* multi-line comment\n   process.env.HOME\n   process.kill(-pid)\n   mode & 0o077\n*/\nconst ok = 1;\n",
        "const s1 = 'mode & 0o077 and process.env.HOME and process.kill(-pid)';\n",
        "const s2 = \"mode & 0O077 and process[\\\"env\\\"].HOME\";\n",
        "const tpl = `line 1\nmode & 0o077\nprocess.env.HOME\nprocess.kill(-pid)`;\n",
        "const tplExpr = `header ${1 + 2} process.env.HOME & 0o077 ${'ok'} process.kill(-pid)`;\n",
        "const pattern = /mode & 0o077|process\\.env\\.HOME|process\\.kill\\(-pid\\)/g;\n",
    ],
)
def test_portable_forms_comments_strings_templates_and_regexes_pass(source: str) -> None:
    assert rules(source) == [], (source, GATE.scan_source(source, PROBE))


def _tree(tmp_path: Path, source: str, entries: dict | None = None) -> Path:
    path = tmp_path / PROBE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    if entries is not None:
        baseline = tmp_path / GATE.BASELINE_RELATIVE_PATH
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps({"version": GATE.BASELINE_VERSION, "entries": entries}), encoding="utf-8")
    return tmp_path


def _entry(count: int = 1) -> dict:
    return {"count": count, "reason": "debt-0.5.5.4: W1E-1 fixes it", "removal": "0.5.5.4"}


def test_rules_are_enforced_by_default_and_advisory_mode_stays_tested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert GATE.ADVISORY_RULES == frozenset()
    source = "process.kill(-pid, 'SIGTERM');\n"
    assert GATE.scan(_tree(tmp_path / "missing", source))["status"] == "FAIL"
    assert GATE.scan(_tree(tmp_path / "admitted", source, {PROBE: {"N002": _entry()}}))["status"] == "PASS"
    more = GATE.scan(_tree(tmp_path / "more", source * 2, {PROBE: {"N002": _entry()}}))
    assert (more["status"], more["newFindings"][0]["lines"]) == ("FAIL", [1, 2])
    stale = GATE.scan(_tree(tmp_path / "stale", "child.kill();\n", {PROBE: {"N002": _entry()}}))
    assert (stale["status"], stale["staleBaseline"][0]["actual"]) == ("FAIL", 0)

    monkeypatch.setattr(GATE, "ADVISORY_RULES", frozenset({"N002"}))
    root = _tree(tmp_path / "advisory", source)
    report = GATE.scan(root)
    assert report["status"] == "PASS"
    assert [row["rule"] for row in report["advisoryFindings"]] == ["N002"]
    assert GATE.main(["--root", str(root)]) == 0
    assert f"ADVISORY N002 negative-pid-signal {PROBE}:1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "entries",
    [
        {PROBE: {"N999": _entry()}},
        {PROBE: {"N001": {**_entry(), "scope": "x"}}},
        {PROBE: {"N001": _entry(count=0)}},
        {PROBE: {"N001": {**_entry(), "reason": " "}}},
        {PROBE: {}},
    ],
)
def test_a_malformed_baseline_fails_closed(tmp_path: Path, entries: dict) -> None:
    report = GATE.scan(_tree(tmp_path, "const x = 1;\n", entries))
    assert report["status"] == "FAIL" and report["errors"]


def test_first_seed_passes_when_base_has_no_ledger_then_locks_strictly(tmp_path: Path) -> None:
    source = "if ((metadata.mode & 0o077) !== 0) throw new Error('x');\n"
    root = _tree(tmp_path, source)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base-without-ledger"], check=True)
    assert GATE.base_baseline(root, "HEAD") is None

    _tree(root, source, {PROBE: {"N001": _entry()}})
    seeded = GATE.scan(root, base_ref="HEAD")
    assert seeded["status"] == "PASS" and seeded["errors"] == []

    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "ledger-seeded"], check=True)
    assert GATE.compare_with_base({(PROBE, "N001"): 1}, GATE.base_baseline(root, "HEAD")) == []
    assert "raised" in GATE.compare_with_base({(PROBE, "N001"): 2}, {(PROBE, "N001"): 1})[0]
    assert "new baseline key" in GATE.compare_with_base({(PROBE, "N002"): 1}, {})[0]

    _tree(root, source + "const h = process.env.HOME;\n", {PROBE: {"N001": _entry(), "N003": _entry()}})
    blocked = GATE.scan(root, base_ref="HEAD")
    assert blocked["status"] == "FAIL"
    assert any("new baseline key" in err and "N003" in err for err in blocked["errors"])
    assert any("not available" in error for error in GATE.scan(root, base_ref="no-such-ref")["errors"])


def test_the_live_tree_matches_the_committed_baseline() -> None:
    report = GATE.scan(ROOT)
    assert report["errors"] == [] and report["newFindings"] == [] and report["staleBaseline"] == []
    assert report["summary"]["advisory"] == 0
    assert report["summary"]["findings"] == 0
    assert report["status"] == "PASS"


def test_the_gate_is_named_in_the_merge_gate_script_loop() -> None:
    """The merge gate names the script in its structural-gate loop.

    As of PR #78 (2026-11-26) that loop passes no ``--base-ref``, so the
    ratchet half (``test_first_seed_passes_when_base_has_no_ledger_then_locks_strictly``)
    is exercised only here, not on a pull request.
    """

    workflow = (ROOT / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
    block = workflow.split("for gate in", 1)[1].split("; do", 1)[0]
    named = block.replace("\\", " ").split()
    assert named.count("check_node_platform_idioms") == 1, named
