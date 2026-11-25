"""Positive and negative controls for the client daemon-request architecture gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/check_daemon_request_architecture.py"
BASELINE = ROOT / "scripts/ci/daemon-request-architecture-baseline.json"
SPEC = importlib.util.spec_from_file_location("daemon_request_architecture", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _catalog(root: Path, *methods: str) -> None:
    names = methods or ("agent.list", "agent.create", "message.send")
    _write(
        root,
        "contract/daemon-request/openapi.json",
        json.dumps({"x-hyprial-methods": [{"name": name} for name in names]}),
    )


def _kinds(report: dict) -> list[str]:
    return [row["kind"] for row in report["violations"]]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "request-architecture",
            "GIT_AUTHOR_EMAIL": "request-architecture@example.invalid",
            "GIT_COMMITTER_NAME": "request-architecture",
            "GIT_COMMITTER_EMAIL": "request-architecture@example.invalid",
        },
    )
    return result.stdout.strip()


def test_registered_direct_and_wrapper_requests_do_not_raise_method_findings(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        '''import typer

app = typer.Typer()


def _call(method, params):
    return services.runtime.request(method, params)


@app.command("demo")
def demo():
    services.runtime.request("agent.list", {})
    _call("agent.create", {})
''',
    )

    kinds = _kinds(GATE.scan(tmp_path))

    assert "undeclared-method" not in kinds
    assert "dynamic-method" not in kinds
    assert "command-without-request" not in kinds


def test_unknown_dynamic_loop_and_fanout_requests_are_findings(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        '''import typer

app = typer.Typer()


@app.command("demo")
def demo(targets, method):
    for target in targets:
        services.runtime.request("agent.create", {"name": target})
    services.runtime.request("unknown.method", {})
    services.runtime.request(method, {})
''',
    )
    report = GATE.scan(tmp_path)

    assert set(_kinds(report)) >= {
        "request-in-loop",
        "request-fanout",
        "undeclared-method",
        "dynamic-method",
    }


def test_keyword_method_argument_is_checked(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        '''def lookup(method):
    services.runtime.request(method="agent.list", params={})
    services.runtime.request(method="unknown.method", params={})
    services.runtime.request(method=method, params={})
''',
    )
    report = GATE.scan(tmp_path)

    assert report["summary"]["undeclared-method"] == 1
    assert report["summary"]["dynamic-method"] == 1


def test_equivalent_import_mutation_and_process_forms_are_findings(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        '''import os
import shutil
import tempfile
from hyprial import daemon
from subprocess import Popen


def mutate():
    shutil.rmtree("old")
    tempfile.mkdtemp()
    tempfile.mkstemp()
    tempfile.TemporaryDirectory()
    os.link("source", "target")
    __import__("hyprial.identity")
    __import__(daemon.__name__)
    return daemon, Popen
''',
    )
    report = GATE.scan(tmp_path)

    assert report["summary"]["backend-import"] == 2
    assert report["summary"]["dynamic-import"] == 1
    assert report["summary"]["file-mutation"] == 5
    assert report["summary"]["process-invocation"] == 1


def test_backend_authority_file_and_process_sites_are_findings(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "src/hyprial/shell/impl/gui/live/backend.py",
        '''import sqlite3
import subprocess
from hyprial.daemon import PacReactor


def mutate(path):
    path.write_text("state")
    subprocess.Popen(["worker"])
    return PacReactor
''',
    )
    kinds = _kinds(GATE.scan(tmp_path))

    assert set(kinds) >= {
        "backend-import",
        "authority-import",
        "file-mutation",
        "process-invocation",
    }


def test_native_business_cli_checkpoint_and_process_sites_are_findings(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "clients/native/src-tauri/src/setup/mod.rs",
        '''//! invoke_json_stdin must not count this documentation line.
async fn prepare(io: &CliIo, home: &Path) {
    let _ = invoke_json_stdin(io, &["agent", "create"], "setup", "E", TIMEOUT, "", None).await;
    check.write(home)?;
    tokio::process::Command::new("worker");
}
''',
    )
    report = GATE.scan(tmp_path)
    kinds = _kinds(report)

    assert set(kinds) == {
        "native-cli-invocation",
        "native-checkpoint-write",
        "native-process-invocation",
    }
    assert sum(row["count"] for row in report["findings"]) == 3


def test_baseline_is_shrink_only_and_a_new_finding_cannot_be_absorbed(tmp_path: Path) -> None:
    _catalog(tmp_path)
    source = _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        'services.runtime.request("agent.list", {})\n',
    )
    baseline = tmp_path / GATE.BASELINE_RELATIVE_PATH
    assert GATE.write_baseline(tmp_path, baseline)["status"] == "PASS"
    assert GATE.check(tmp_path, baseline)["status"] == "PASS"

    source.write_text(
        source.read_text(encoding="utf-8")
        + 'services.runtime.request("unknown.method", {})\n',
        encoding="utf-8",
    )
    report = GATE.check(tmp_path, baseline)

    assert report["status"] == "FAIL"
    assert "undeclared-method" in _kinds(report)
    assert "new-architecture-finding" in _kinds(report)
    assert GATE.write_baseline(tmp_path, baseline)["status"] == "FAIL"

    source.write_text('services.runtime.request("agent.list", {})\n', encoding="utf-8")
    assert GATE.write_baseline(tmp_path, baseline)["status"] == "PASS"
    assert GATE.check(tmp_path, baseline)["status"] == "PASS"


def test_base_comparison_rejects_growth_even_when_the_head_ledger_is_edited(tmp_path: Path) -> None:
    _catalog(tmp_path)
    source = _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        'services.runtime.request("agent.list", {})\n',
    )
    baseline = tmp_path / GATE.BASELINE_RELATIVE_PATH
    GATE.write_baseline(tmp_path, baseline)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")

    source.write_text(
        source.read_text(encoding="utf-8") + "from hyprial.daemon import PacReactor\n",
        encoding="utf-8",
    )
    baseline.write_text(
        json.dumps(GATE._baseline_document(GATE.scan(tmp_path)["findings"]), indent=2) + "\n",
        encoding="utf-8",
    )
    report = GATE.check(tmp_path, baseline, base_ref="HEAD")

    assert report["status"] == "FAIL"
    assert "base-architecture-growth" in _kinds(report)
    assert "ledger-growth" in _kinds(report)


def test_missing_base_ledger_fails_closed_unless_first_seed_is_explicit(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "src/hyprial/shell/impl/cli/commands/demo.py",
        'services.runtime.request("agent.list", {})\n',
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base without architecture ledger")
    baseline = tmp_path / GATE.BASELINE_RELATIVE_PATH
    GATE.write_baseline(tmp_path, baseline)
    original = GATE.REVIEWED_BASELINE_SHA256
    GATE.REVIEWED_BASELINE_SHA256 = GATE._digest(baseline)
    try:
        report = GATE.check(tmp_path, baseline, base_ref="HEAD", allow_first_seed=False)
        assert report["status"] == "FAIL"
        assert "missing-base-ledger" in _kinds(report)

        seeded = GATE.check(
            tmp_path,
            baseline,
            base_ref="HEAD",
            base_branch="dev",
            allow_first_seed=True,
        )
        assert seeded["status"] == "PASS", seeded
        assert seeded["firstSeedUsed"] is True

        wrong_branch = GATE.check(
            tmp_path,
            baseline,
            base_ref="HEAD",
            base_branch="feature",
            allow_first_seed=True,
        )
        assert wrong_branch["status"] == "FAIL"
        assert "missing-base-ledger" in _kinds(wrong_branch)

        missing = GATE.check(
            tmp_path,
            baseline,
            base_ref="does-not-exist",
            base_branch="dev",
            allow_first_seed=True,
        )
        assert missing["status"] == "FAIL"
        assert "invalid-base-ref" in _kinds(missing)
    finally:
        GATE.REVIEWED_BASELINE_SHA256 = original


def test_real_repository_baseline_matches() -> None:
    report = GATE.check(ROOT, BASELINE)

    assert report["status"] == "PASS", report
    assert report["summary"]["backend-import"] > 0
    assert report["summary"]["request-fanout"] > 0


def test_real_repository_retired_gui_entries_do_not_reseed_the_ledger() -> None:
    document = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert document["version"] == 1
    assert all(not row["path"].startswith("src/hyprial/shell/impl/gui/live/")
               for row in document["findings"])
    assert GATE.REVIEWED_BASELINE_SHA256 == "37a29315ac33988c74e004f00e402256bb71342398d30a5aff3c1e06007ba4eb"
    assert GATE._first_seed_problems(BASELINE, "dev") == [
        {"kind": "seed-digest-mismatch", "path": GATE.BASELINE_RELATIVE_PATH},
    ]


def test_legitimate_shrink_passes_existing_base_but_cannot_be_a_new_first_seed(tmp_path: Path) -> None:
    _catalog(tmp_path)
    source = _write(
        tmp_path,
        "src/hyprial/shell/impl/gui/live/backend.py",
        "from hyprial.daemon import PacReactor\n",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base without ledger")
    without_ledger = _git(tmp_path, "rev-parse", "HEAD")
    baseline = tmp_path / GATE.BASELINE_RELATIVE_PATH
    GATE.write_baseline(tmp_path, baseline)
    original = GATE.REVIEWED_BASELINE_SHA256
    GATE.REVIEWED_BASELINE_SHA256 = GATE._digest(baseline)
    try:
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-qm", "reviewed ledger")
        source.unlink()
        assert GATE.write_baseline(tmp_path, baseline)["status"] == "PASS"
        report = GATE.check(tmp_path, baseline, base_ref="HEAD", base_branch="dev")
        assert report["status"] == "PASS", report
        assert report["firstSeedUsed"] is False
        reseeded = GATE.check(tmp_path, baseline, base_ref=without_ledger,
                             base_branch="dev", allow_first_seed=True)
        assert reseeded["status"] == "FAIL"
        assert "seed-digest-mismatch" in _kinds(reseeded)
    finally:
        GATE.REVIEWED_BASELINE_SHA256 = original


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        json.dumps({
            "version": 1,
            "findings": [
                {"kind": "backend-import", "path": "demo.py", "subject": "daemon", "count": 1},
                {"kind": "backend-import", "path": "demo.py", "subject": "daemon", "count": 1},
            ],
        }),
    ],
)
def test_invalid_baseline_is_rejected(tmp_path: Path, contents: str) -> None:
    _catalog(tmp_path)
    baseline = _write(tmp_path, GATE.BASELINE_RELATIVE_PATH, contents)
    report = GATE.check(tmp_path, baseline)

    assert report["status"] == "FAIL"
    assert "invalid-baseline" in _kinds(report)


def test_inline_cfg_test_items_are_not_counted(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "clients/native/src-tauri/src/runtime/launcher.rs",
        '''pub fn spawn() {
    let _ = std::process::Command::new("worker");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spawns() {
        let label = "braces } { inside a string";
        let _ = Command::new(label);
    }
}

#[cfg(test)]
fn helper() {
    let _ = tokio::process::Command::new("helper");
}

#[cfg(all(test, unix))]
#[test]
fn unix_only() {
    let _ = Command::new("unix");
}

#[cfg(test)]
mod external_tests;

pub fn resume() {
    let _ = Command::new("resume");
}
''',
    )

    report = GATE.scan(tmp_path)

    assert report["summary"].get("native-process-invocation") == 2


def test_reviewed_native_exceptions_are_exact_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    path = "clients/native/src-tauri/src/runtime/product/python.rs"
    subject = GATE.NATIVE_PROCESS_PATTERN
    monkeypatch.setitem(
        GATE.REVIEWED_NATIVE_EXCEPTIONS, ("native-process-invocation", path, subject), 1
    )

    absorbed, audit = GATE._reviewed_rows(
        [{"kind": "native-process-invocation", "path": path, "subject": subject, "count": 1}]
    )
    assert absorbed == []
    assert audit == [
        {
            "kind": "native-process-invocation",
            "path": path,
            "subject": subject,
            "allowed": 1,
            "observed": 1,
            "applied": 1,
        }
    ]

    bounded, audit = GATE._reviewed_rows(
        [{"kind": "native-process-invocation", "path": path, "subject": subject, "count": 2}]
    )
    assert len(audit) == 1
    assert audit[0]["applied"] == 1
    assert bounded == [
        {"kind": "native-process-invocation", "path": path, "subject": subject, "count": 1}
    ]

    unregistered = "clients/native/src-tauri/src/runtime/product/other.rs"
    other, audit = GATE._reviewed_rows(
        [{"kind": "native-process-invocation", "path": unregistered, "subject": subject, "count": 1}]
    )
    assert audit == []
    assert other == [
        {"kind": "native-process-invocation", "path": unregistered, "subject": subject, "count": 1}
    ]


def test_cfg_test_semicolon_items_do_not_swallow_production_code(tmp_path: Path) -> None:
    _catalog(tmp_path)
    source = '''use std::io;

#[cfg(test)]
use serde_json::Map;

pub(crate) struct Snapshot { rows: Vec<u8> }

pub fn production() {
    let _ = std::process::Command::new("p");
}

#[cfg(test)]
struct UnitMarker;

#[cfg(test)]
const MARKER: S = S { a: 1 };

pub fn later() {
    let _ = Command::new("l");
}
'''
    _write(tmp_path, "clients/native/src-tauri/src/runtime/semicolon.rs", source)
    stripped = GATE._strip_cfg_test_items(GATE._rust_code(source))

    assert "pub(crate) struct Snapshot" in stripped
    assert "fn production" in stripped
    assert "fn later" in stripped
    assert "use serde_json::Map;" in stripped
    assert "struct UnitMarker;" in stripped
    assert "const MARKER: S = S { a: 1 };" in stripped

    report = GATE.scan(tmp_path)

    assert report["summary"].get("native-process-invocation") == 2


def _native_rows(report: dict) -> list[dict]:
    return [
        row
        for row in report["findings"]
        if row["kind"] == "native-process-invocation" and row["path"].startswith("clients/native/")
    ]


def test_posix_spawn_calls_are_native_process_invocations(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _write(
        tmp_path,
        "clients/native/launcher/src/spawner.rs",
        '''pub fn start() {
    unsafe { libc::posix_spawnp(&mut pid, program, &actions, &attrs, argv, envp) };
    unsafe { posix_spawn (&mut pid, program, &actions, &attrs, argv, envp) };
    unsafe { libc::posix_spawn_file_actions_init(&mut actions) };
    unsafe { libc::posix_spawnattr_init(&mut attrs) };
}
''',
    )

    report = GATE.scan(tmp_path)

    assert report["summary"].get("native-process-invocation") == 2


def test_real_native_tree_has_exactly_the_reviewed_process_invocations() -> None:
    rows = _native_rows(GATE.scan(ROOT))
    registered = {
        key: count
        for key, count in GATE.REVIEWED_NATIVE_EXCEPTIONS.items()
        if key[0] == "native-process-invocation" and key[1].startswith("clients/native/")
    }

    assert {(row["kind"], row["path"], row["subject"]): row["count"] for row in rows} == registered
    assert registered == {
        ("native-process-invocation", "clients/native/launcher/src/platform/unix.rs", GATE.NATIVE_PROCESS_PATTERN): 2,
        ("native-process-invocation", "clients/native/launcher/src/platform/windows.rs", GATE.NATIVE_PROCESS_PATTERN): 1,
    }
    kept, _ = GATE._reviewed_rows(rows)
    assert kept == []


def test_reviewed_cli_exceptions_are_exactly_the_tiers_probe_rows() -> None:
    tiers = "src/hyprial/shell/impl/cli/commands/agent/runtime/tiers.py"

    assert GATE.REVIEWED_CLI_EXCEPTIONS == {
        ("backend-import", tiers, "hyprial.daemon:probe_capabilities"): 1,
        ("command-without-request", tiers, "tiers.py"): 1,
    }
    assert not set(GATE.REVIEWED_CLI_EXCEPTIONS) & set(GATE.REVIEWED_NATIVE_EXCEPTIONS)
    assert all(key[1].startswith("clients/native/") for key in GATE.REVIEWED_NATIVE_EXCEPTIONS)


def test_real_cli_tree_reports_exactly_the_reviewed_cli_rows() -> None:
    report = GATE.scan(ROOT)
    observed = {
        (row["kind"], row["path"], row["subject"]): row["count"]
        for row in report["findings"]
        if (row["kind"], row["path"], row["subject"]) in GATE.REVIEWED_CLI_EXCEPTIONS
    }

    assert observed == GATE.REVIEWED_CLI_EXCEPTIONS
    kept, audit = GATE._reviewed_rows(
        [{"kind": kind, "path": path, "subject": subject, "count": count}
         for (kind, path, subject), count in observed.items()]
    )
    assert kept == []
    assert len(audit) == len(GATE.REVIEWED_CLI_EXCEPTIONS)


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("clients/native/src-tauri/src/extra.rs", 'pub fn f() { let _ = std::process::Command::new("x"); }\n'),
        ("clients/native/launcher/src/extra.rs", "pub fn f() { unsafe { libc::posix_spawn(p, a, b, c, d, e) }; }\n"),
    ],
)
def test_one_more_native_process_site_is_a_violation(tmp_path: Path, relative: str, source: str) -> None:
    _catalog(tmp_path)
    for reviewed in (
        "clients/native/launcher/src/platform/unix.rs",
        "clients/native/launcher/src/platform/windows.rs",
    ):
        _write(tmp_path, reviewed, (ROOT / reviewed).read_text(encoding="utf-8"))
    baseline, _ = GATE._reviewed_rows(_native_rows(GATE.scan(tmp_path)))
    assert baseline == []

    _write(tmp_path, relative, source)
    kept, _ = GATE._reviewed_rows(_native_rows(GATE.scan(tmp_path)))

    assert kept == [
        {"kind": "native-process-invocation", "path": relative, "subject": GATE.NATIVE_PROCESS_PATTERN, "count": 1}
    ]


def test_a_second_site_in_the_reviewed_file_exceeds_its_registration(tmp_path: Path) -> None:
    _catalog(tmp_path)
    unix_path = "clients/native/launcher/src/platform/unix.rs"
    windows_path = "clients/native/launcher/src/platform/windows.rs"
    _write(tmp_path, unix_path, (ROOT / unix_path).read_text(encoding="utf-8"))
    _write(
        tmp_path,
        windows_path,
        (ROOT / windows_path).read_text(encoding="utf-8")
        + '\npub fn extra() { let _ = Command::new("again"); }\n',
    )

    kept, _ = GATE._reviewed_rows(_native_rows(GATE.scan(tmp_path)))

    assert [row["count"] for row in kept] == [1]
