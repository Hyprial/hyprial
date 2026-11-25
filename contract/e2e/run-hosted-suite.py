#!/usr/bin/env python3
"""Serial CI/ops entry for the hosted-agent MVP; no real model credentials.

BOOTSTRAP_MODULES mirrors the isolated daemon imports in run-hosted-registry.py.
"""
import argparse
import hashlib
import importlib.util
from datetime import datetime
import json
import math
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_MODULES = ("shell.impl.cli.runtime.entry", "daemon.impl.application", "identity.impl.agents.registry")
LEGACY_BOOTSTRAP_MODULES = ("cli", "daemon.application", "daemon.owner_migration", "agents.registry")


def bootstrap_modules(tree):
    """Select one complete physical source layout; never retry individual imports."""
    native_entry = Path(tree) / "src/hyprial/shell/impl/cli/runtime/entry.py"
    return BOOTSTRAP_MODULES if native_entry.is_file() else LEGACY_BOOTSTRAP_MODULES
# Per-tree budgets; the whole hosted job has 20 minutes (mock-model-harness-e2e.yml).
# Each run_bounded may add REAP_TIMEOUT for reaping: at most 150+10+60+10 = 230 s
# per tree, 460 s for both, plus the census walks and any uninterruptible spawn.
COMPILE_TIMEOUT = 150
PROBE_TIMEOUT = 60
REAP_TIMEOUT = 10
# Suite exit code when the warm-up could not confirm its own children are gone:
# no scenario runs over them, and none is reported as PASS or FAIL.
INFRA_ABORT_EXIT = 6

#: Scenarios whose failure is a known product gap with an open fix, reported as
#: KNOWN-FAIL instead of reddening the job -- but ONLY when the failure carries
#: the recorded signature. Any other failure stays red, and a PASS is red too
#: (UNEXPECTED-PASS) until the entry is removed, so the list cannot go stale.
KNOWN_FAILURES = {}


def classify_known_failure(name, row, report):
    """Apply KNOWN_FAILURES to one scenario row; returns True when it stays red."""

    known = KNOWN_FAILURES.get(name)
    red = bool(row["exitCode"]) or row["result"] != "PASS"
    if known is None:
        return red
    if not red:
        row.update(result="UNEXPECTED-PASS", knownFailure=known)
        print(f"::error::{name} passed but is listed in KNOWN_FAILURES; remove the entry "
              f"(tracking {known['tracking']})", flush=True)
        return True
    if row["result"] == "FAIL" and known["signature"] in str((report or {}).get("traceback", "")):
        row.update(result="KNOWN-FAIL", knownFailure=known)
        print(f"::warning::{name}: KNOWN-FAIL ({known['reason']}; tracking {known['tracking']})", flush=True)
        return False
    return True


def _load_start_probe():
    """The start-probe reader (AT04 card 6); None if it cannot load, which only
    blanks the observation field and never affects the suite."""
    try:
        spec = importlib.util.spec_from_file_location("hosted_start_probe", Path(__file__).with_name("_start_probe.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


_start_probe = _load_start_probe()


def probe_observation(evidence, key):
    """Observation-only summary of <key>-probe.jsonl for readiness.json; never raises and
    never feeds complete. End point per criteria §4 (_start_probe.observe_start): the
    parent's ready-mark for a ready start, the last sample for a timed-out one."""
    empty = {"installed": False, "note": None}
    try:
        if _start_probe is None:
            return dict(empty, note="start probe reader unavailable")
        observed = _start_probe.observe_start(evidence / (key + "-probe.jsonl"), evidence / (key + "-ready-mark.json"))
        return {name: value for name, value in observed.items() if not name.startswith("_")}
    except Exception as error:  # noqa: BLE001
        return dict(empty, note=f"probe observation failed: {type(error).__name__}: {error}")
EXPECTED_TREES = ("new", "old")
# Fields owned by the parent's run_bounded; the probe's own JSON may never set them.
PARENT_FIELDS = ("status", "returncode", "seconds", "survivors", "error", "stderr")
# Replaceable in tests (virtual time for slow spawn / slow ps / budgets).
clock = time.monotonic
sleep = time.sleep
# Imports BOOTSTRAP's modules in a fresh interpreter with the isolated daemon's own
# env and counts real source compiles: "compile" audit events raised from
# importlib's source_to_code, which runs only when the loader falls back from
# bytecode to source (missing, stale or wrong-magic .pyc alike). Other compile()
# calls on a module file (anyio's lazy importer parses its own source to an AST)
# are reported separately. Cache presence alone is reported separately too.
# Modules already imported when the hook is installed (site, .pth) are
# unobserved: listed as preloaded, never counted as bytecode adopted.
IMPORT_PROBE = r"""
import importlib, importlib.util, json, os, sys, time
tree = os.path.realpath(sys.argv[1])
names = sys.argv[2:]
compiled = set()
runtime_compiled = set()

def audit(event, args):
    if event != 'compile' or len(args) < 2 or not isinstance(args[1], str) or not args[1].endswith('.py'):
        return
    frame, names = sys._getframe(1), []
    while frame is not None and len(names) < 4:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    (compiled if 'source_to_code' in names else runtime_compiled).add(os.path.realpath(args[1]))

sys.addaudithook(audit)

def in_tree(module):
    origin = getattr(getattr(module, '__spec__', None), 'origin', None)
    if not origin or not origin.endswith('.py'):
        return None
    origin = os.path.realpath(origin)
    return origin if origin.startswith(tree + os.sep) else None

preloaded = set(p for p in map(in_tree, list(sys.modules.values())) if p)
started = time.perf_counter()
for name in names:
    provider = os.path.join(tree, 'src', 'hyprial', *name.split('.'))
    candidates = (provider + '.py', os.path.join(provider, '__init__.py'))
    providers = [candidate for candidate in candidates if os.path.isfile(candidate)]
    assert len(providers) == 1, (name, providers)
    expected_source = providers[0]
    assert not os.path.islink(expected_source), expected_source
    assert os.path.commonpath((tree, os.path.realpath(expected_source))) == tree, expected_source
    importlib.import_module('hyprial.' + name)
    assert in_tree(sys.modules['hyprial.' + name]) == os.path.realpath(expected_source), name
seconds = time.perf_counter() - started
loaded = sorted(p for p in map(in_tree, list(sys.modules.values())) if p)
source_compiled = sorted(p for p in compiled if p.startswith(tree + os.sep))
cached = [p for p in loaded if os.path.exists(importlib.util.cache_from_source(p))]
src = os.path.join(tree, 'src') + os.sep
bootstrap = {}
for name in names:
    origin = in_tree(sys.modules['hyprial.' + name]) or ''
    bootstrap[name] = {'origin': origin, 'underSrc': origin.startswith(src),
                       'compiledFromSource': origin in compiled, 'preloaded': origin in preloaded}
print(json.dumps({
    'seconds': round(seconds, 3), 'python': sys.version.split()[0], 'executable': sys.executable,
    'cacheTag': sys.implementation.cache_tag, 'optimize': sys.flags.optimize,
    'dontWriteBytecode': bool(sys.flags.dont_write_bytecode), 'pycachePrefix': sys.pycache_prefix,
    'modulesInTree': len(loaded), 'sourceCompiles': len(source_compiled),
    'bytecodeAdopted': len([p for p in loaded if p not in compiled and p not in preloaded]),
    'cachePresent': len(cached), 'cacheMissing': len(loaded) - len(cached),
    'preloadedInTree': sorted(preloaded), 'sourceCompiledPaths': source_compiled[:200], 'bootstrap': bootstrap,
    'runtimeCompiledPaths': sorted(p for p in runtime_compiled if p.startswith(tree + os.sep)),
}))
"""


def precompile_targets(tree):
    """The trees whose first import would otherwise compile inside a readiness budget."""
    root = tree.resolve()
    targets = [tree / "src", *sorted((tree / ".venv").glob("lib/python*/site-packages"))]
    return [target for target in targets if target.is_dir() and target.resolve().is_relative_to(root)]


def pyc_census(tree):
    """Counts .py/.pyc under src, site-packages and lark_oapi, for the evidence record."""
    census = {}
    sites = sorted((tree / ".venv").glob("lib/python*/site-packages"))
    for key, roots in (("src", [tree / "src"]), ("site", sites), ("larkOapi", [s / "lark_oapi" for s in sites])):
        census[key] = {suffix: sum(1 for root in roots if root.is_dir() for _ in root.rglob("*." + suffix))
                       for suffix in ("py", "pyc")}
    return census


def group_members(pgid, timeout):
    """Live (non-zombie) members of a process group; None when ps cannot tell."""
    try:
        listing = subprocess.run(["/bin/ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True,
                                 timeout=max(min(5, timeout), 0.001))
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = [line.split() for line in listing.stdout.splitlines() if line.strip()]
    if listing.returncode or not rows or any(
            # ps prints "?" for a process it cannot classify (e.g. mid-exec); that is a
            # valid row, conservatively counted as live below.
            len(row) != 3 or not row[0].isdigit() or not row[1].isdigit()
            or not (row[2][:1].isalpha() or row[2][:1] == "?")
            for row in rows):
        return None
    return [row[0] for row in rows if row[1] == str(pgid) and not row[2].startswith("Z")]


def run_bounded(command, env, cwd, timeout):
    """Run a child in its own process group within a budget, then reap the group.

    status is ok | nonzero | timeout | spawn-error | reap-failed. The work budget is
    an absolute deadline taken before Popen, so a slow spawn counts against it; Popen
    itself cannot be interrupted by these synchronous waits, so an over-budget spawn
    is reported as timeout once it returns. Reaping (wait, ps, sleep) then shares one
    REAP_TIMEOUT deadline. compileall -j 0 forks a worker pool, so the whole group is
    killed and verified empty; unknown or non-empty means reap-failed.
    """
    started = clock()
    deadline = started + timeout
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as error:
        return {"status": "spawn-error", "error": f"{type(error).__name__}: {error}", "stdout": "",
                "seconds": round(clock() - started, 3)}
    status, stdout, stderr = None, "", ""
    try:
        remaining = deadline - clock()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        stdout, stderr = process.communicate(timeout=remaining)
        status = "ok" if process.returncode == 0 else "nonzero"
        if clock() > deadline:
            status = "timeout"
    except subprocess.TimeoutExpired:
        status = "timeout"
    finally:
        reap_deadline = clock() + REAP_TIMEOUT
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            more_out, more_err = process.communicate(timeout=max(reap_deadline - clock(), 0.001))
            stdout, stderr = stdout or more_out or "", stderr or more_err or ""
        except subprocess.TimeoutExpired:
            status = "reap-failed"
        members = group_members(process.pid, reap_deadline - clock())
        while members and clock() < reap_deadline:
            sleep(min(0.1, max(reap_deadline - clock(), 0)))
            members = group_members(process.pid, reap_deadline - clock())
        if members is None or members:
            status = "reap-failed"
    return {"status": status, "returncode": process.returncode, "stdout": stdout or "",
            "stderr": (stderr or "")[-2000:], "survivors": members, "seconds": round(clock() - started, 3)}


def daemon_env(tree, scratch):
    """The isolated daemon's exact env (run-hosted-registry Scenario + use_build), under scratch."""
    spec = importlib.util.spec_from_file_location("hosted_suite_registry", ROOT / "contract/e2e/run-hosted-registry.py")
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    scenario = registry.Scenario(scratch, scratch / "no-production.sock")
    return dict(scenario.env, PYTHONPATH=str(tree / "src"), AT01_SOURCE=str(tree))


def import_probe(tree):
    """Imports BOOTSTRAP's modules in a fresh interpreter with the daemon's env."""
    scratch = Path(tempfile.mkdtemp(prefix="at04-warm-", dir="/tmp")).resolve()
    try:
        env = daemon_env(tree, scratch)
        ran = run_bounded([str(tree / ".venv/bin/python"), "-c", IMPORT_PROBE, str(tree), *bootstrap_modules(tree)],
                          env, tree, PROBE_TIMEOUT)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    # The parent's own view (status, survivors, ...) is authoritative: the child's
    # stdout may be anything, e.g. a line printed by a module stuck mid-import.
    probe = {key: ran[key] for key in PARENT_FIELDS if key in ran}
    probe["env"] = sorted(env)
    if ran.get("status") == "reap-failed":
        probe["stdoutTail"] = (ran.get("stdout") or "")[-500:]
        return probe
    try:
        measured = json.loads((ran.get("stdout") or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        measured = None
    if isinstance(measured, dict):
        probe.update({key: value for key, value in measured.items() if key not in PARENT_FIELDS and key != "env"})
    else:
        probe["measurement"] = "invalid"
        probe["stdoutTail"] = (ran.get("stdout") or "")[-500:]
    return probe


def warm_tree(tree):
    """Compile one build tree with the daemon's own interpreter, then import as the daemon does.

    mock-model-harness-e2e builds both venvs with ``uv sync`` (no bytecode) and the isolated
    daemon runs with PYTHONDONTWRITEBYTECODE=1, so each daemon and CLI start re-read
    and compiled hyprial and its dependencies from source inside its readiness window
    (task 37735: SIGUSR2 stack in ``get_code`` reading source). This moves that work,
    and one fresh-interpreter import, ahead of every scenario. Best effort: failures
    are recorded and never change a scenario verdict -- except an unconfirmed reap
    (``infraAbort``), after which nothing else may run on this host.
    """
    report = {"tree": str(tree), "python": str(tree / ".venv/bin/python")}
    try:
        report["before"] = pyc_census(tree)
        targets = precompile_targets(tree)
        if not targets:
            # compileall with no targets compiles sys.path, i.e. the shared interpreter.
            report["compile"] = {"status": "skipped-no-targets", "targets": []}
        else:
            # A clean env: an inherited PYTHONPYCACHEPREFIX/PYTHONOPTIMIZE would write
            # bytecode the daemon (clean env too) never reads.
            compiled = run_bounded([str(tree / ".venv/bin/python"), "-m", "compileall", "-q", "-j", "0",
                                    *map(str, targets)],
                                   {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}, tree,
                                   COMPILE_TIMEOUT)
            report["compile"] = {key: compiled[key] for key in ("status", "returncode", "seconds", "survivors")
                                 if key in compiled}
            report["compile"]["targets"] = [str(target) for target in targets]
            if compiled.get("status") == "reap-failed":
                report["infraAbort"] = f"compile reap unconfirmed: survivors={compiled.get('survivors')}"
                return report
        report["import"] = import_probe(tree)
        if report["import"].get("status") == "reap-failed":
            report["infraAbort"] = f"probe reap unconfirmed: survivors={report['import'].get('survivors')}"
            return report
        report["after"] = pyc_census(tree)
    except Exception as error:  # noqa: BLE001 - a warm-up must never fail the suite
        report["note"] = f"{type(error).__name__}: {error}"
    return report


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def tree_problems(name, report):
    """Every reason one tree's warm-up is not demonstrably effective; missing is never zero."""
    if not isinstance(report, dict):
        return [f"{name}: no warm-up record"]
    if report.get("skipped") or report.get("infraAbort") or report.get("note"):
        return [f"{name}: {report.get('skipped') or report.get('infraAbort') or report.get('note')}"]
    problems = []
    compiled = report.get("compile")
    if not isinstance(compiled, dict) or compiled.get("status") != "ok":
        problems.append(f"{name}: compile {compiled.get('status') if isinstance(compiled, dict) else 'missing'}")
    probe = report.get("import")
    if not isinstance(probe, dict):
        return problems + [f"{name}: probe missing"]
    if probe.get("status") != "ok":
        problems.append(f"{name}: probe {probe.get('status')}")
    if not _is_int(probe.get("sourceCompiles")) or probe["sourceCompiles"] != 0:
        problems.append(f"{name}: sourceCompiles={probe.get('sourceCompiles')!r}")
    for key in ("modulesInTree", "bytecodeAdopted"):
        if not _is_int(probe.get(key)) or probe[key] <= 0:
            problems.append(f"{name}: {key}={probe.get(key)!r}")
    if probe.get("dontWriteBytecode") is not True or probe.get("pycachePrefix", "missing") is not None \
            or not _is_int(probe.get("optimize")) or probe["optimize"] != 0:
        problems.append(f"{name}: probe interpreter flags differ from the daemon's")
    bootstrap = probe.get("bootstrap")
    selected = bootstrap_modules(report["tree"]) if report.get("tree") else next(
        (core for core in (BOOTSTRAP_MODULES, LEGACY_BOOTSTRAP_MODULES)
         if isinstance(bootstrap, dict) and set(bootstrap) == set(core)), ()
    )
    if not selected or not isinstance(bootstrap, dict) or set(bootstrap) != set(selected):
        return problems + [f"{name}: bootstrap modules {sorted(bootstrap) if isinstance(bootstrap, dict) else bootstrap!r}"]
    for module in selected:
        entry = bootstrap[module]
        if not isinstance(entry, dict) or entry.get("underSrc") is not True \
                or entry.get("compiledFromSource") is not False or entry.get("preloaded") is not False:
            problems.append(f"{name}: hyprial.{module} not observed loading as bytecode from {name}/src")
    return problems


def precompile_verdict(record):
    """Whether the warm-up demonstrably took effect for both trees (evidence, not a gate)."""
    try:
        if not isinstance(record, dict):
            problems = ["no warm-up record"]
        else:
            problems = [problem for name in EXPECTED_TREES for problem in tree_problems(name, record.get(name))]
    except Exception as error:  # noqa: BLE001 - an evidence summary must never fail the suite
        problems = [f"verdict error: {type(error).__name__}: {error}"]
    return {"effective": not problems, "problems": problems}


def warm_trees(trees, evidence, echo):
    """Warm every build tree before the first scenario; record it in the evidence.

    Stops at the first unconfirmed reap: the remaining trees are marked skipped."""
    record = {}
    for name, tree in trees.items():
        if any(report.get("infraAbort") for report in record.values()):
            record[name] = {"tree": str(tree), "skipped": "earlier warm-up reap unconfirmed"}
            continue
        record[name] = warm_tree(tree)
    verdict = precompile_verdict(record)
    (evidence / "precompile.json").write_text(json.dumps(dict(record, verdict=verdict), indent=2) + "\n")
    for name, report in record.items():
        probe = report.get("import") if isinstance(report.get("import"), dict) else {}
        compiled = report.get("compile") if isinstance(report.get("compile"), dict) else {}
        echo(f"PRECOMPILE {name} note={report.get('note') or report.get('infraAbort') or report.get('skipped') or '-'} "
             f"compile={compiled.get('status')} import={probe.get('status')} "
             f"sourceCompiles={probe.get('sourceCompiles')} bytecodeAdopted={probe.get('bytecodeAdopted')}")
    echo(f"PRECOMPILE effective={verdict['effective']} problems={len(verdict['problems'])}")
    return record


STATUSES = ("ready", "timeout", "exited", "spawn-error", "error")
SCENARIOS = (
    "hosted-registry",
    "capability-ledger",
    "hosted-start",
    "hosted-upgrade",
)
IMPORT_FIELDS = ("coreWall", "coreCpu", "allWall", "allCpu")


def _finite(value):
    """A finite, float-representable number; bool and 10**400 are not."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _duration(value, nullable=False):
    if value is None:
        return nullable
    return _finite(value) and value >= 0


ARITHMETIC_TOLERANCE = 1e-5  # records round durations to 1e-6
IMPORT_KEYS = ("coreWall", "coreCpu", "allWall", "allCpu", "modules", "note")
SHA = re.compile(r"^[0-9a-f]{40}$")


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def attempt_problems(record, label=None):
    """Why a terminal attempt record is not usable timing evidence (empty when valid).

    Checks the frozen field meanings, not just types: identity (label matches the
    file, tz-aware UTC times in order), per-status arithmetic (ready: popenToReady
    equals elapsed and margin equals budget - elapsed, possibly negative; timeout:
    overrun equals max(elapsed - budget, 0); others: ready-only fields null),
    exit codes that are ints but not bools, and an import block that has every
    field, with a reason whenever a total is null."""
    if not isinstance(record, dict):
        return ["not an object"]
    problems = []
    status = record.get("status")
    if status not in STATUSES:
        return [f"status {status!r}"]
    if label is not None and record.get("label") != label:
        problems.append(f"label {record.get('label')!r} is not {label!r}")
    if not isinstance(record.get("tree"), str):
        problems.append("tree missing")
    started, observed = _utc(record.get("startedUtc")), _utc(record.get("observedUtc"))
    if started is None or observed is None:
        problems.append("startedUtc/observedUtc not tz-aware ISO times")
    elif observed < started:
        problems.append("observed before started")
    budget, elapsed = record.get("budgetSeconds"), record.get("elapsedSeconds")
    if not _duration(budget) or not _duration(elapsed):
        return problems + ["budget/elapsed not a duration"]
    if status == "ready":
        popen, margin = record.get("popenToReadySeconds"), record.get("marginSeconds")
        if not _duration(popen) or not _finite(margin):
            problems.append("ready without popenToReady/margin")
        elif abs(popen - elapsed) > ARITHMETIC_TOLERANCE or abs(margin - (budget - elapsed)) > ARITHMETIC_TOLERANCE:
            problems.append("ready arithmetic does not match elapsed/budget")
    elif record.get("popenToReadySeconds") is not None or record.get("marginSeconds") is not None:
        problems.append(f"{status} carries ready-only fields")
    if status == "timeout":
        overrun = record.get("overrunSeconds")
        if not _duration(overrun) or abs(overrun - max(elapsed - budget, 0.0)) > ARITHMETIC_TOLERANCE:
            problems.append("timeout overrun does not match elapsed/budget")
    if status == "exited":
        code = record.get("exitCode")
        if not isinstance(code, int) or isinstance(code, bool):
            problems.append("exited without an integer exitCode")
    imported = record.get("import")
    if not isinstance(imported, dict) or any(key not in imported for key in IMPORT_KEYS):
        problems.append("import block incomplete")
    elif not all(_duration(imported[key], nullable=True) for key in IMPORT_FIELDS):
        problems.append("import totals invalid")
    elif any(imported[key] is None for key in IMPORT_FIELDS) and not (isinstance(imported["note"], str) and imported["note"]):
        problems.append("import totals null without a reason")
    return problems


def attempt_records(evidence):
    """Terminal attempt records and ledgers under the evidence root (nested scenarios included)."""
    return sorted(evidence.rglob("daemon-*-attempt.json")), sorted(evidence.rglob("attempts.json"))


def tree_identity(trees):
    identity = {}
    for name, tree in trees.items():
        entry = {"path": str(tree), "resolved": str(Path(tree).resolve()), "sha": None}
        try:
            entry["sha"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True,
                                          timeout=30, check=True).stdout.strip()
        except Exception as error:  # noqa: BLE001 - identity is evidence, not a gate
            entry["shaNote"] = f"{type(error).__name__}"
        identity[name] = entry
    return identity


def collect_readiness(evidence, trees, scenarios, echo, planned=None):
    """Join every attempt ledger with its terminal records into <evidence>/readiness.json.

    Expected attempts come from the ledgers each scenario's result.json lists (so a
    deleted or emptied nested ledger is caught, not silently absent); observed come
    from the terminal records, keyed by path relative to the evidence root. complete
    means the WHOLE planned set: every planned scenario ran and listed its ledgers,
    every ledger is referenced once, readable and non-empty, nothing is missing,
    duplicated, unexpected or bad, every row belongs to a known build tree and each
    tree has a valid HEAD sha. Evidence only: it never changes a scenario verdict,
    and a failure to write or print it is only noted."""
    planned = list(scenarios if planned is None else planned)
    report = {"trees": {}, "scenarios": {}, "expected": 0, "observed": 0, "missing": [], "duplicate": [],
              "unexpected": [], "bad": [], "missingLedgers": [], "emptyLedgers": [], "unknownTrees": [],
              "shaProblems": [], "rows": [], "notes": [], "complete": False}
    try:
        report["trees"] = tree_identity(trees)
        report["shaProblems"] = [name for name, entry in report["trees"].items()
                                 if not isinstance(entry.get("sha"), str) or not SHA.match(entry["sha"])]
        resolved = {name: Path(tree).resolve() for name, tree in trees.items()}
        records, _ = attempt_records(evidence)
        for scenario in planned:
            if scenario not in scenarios:
                report["scenarios"][scenario] = "not-run"
        expected, referenced = [], []
        for scenario in scenarios:
            try:
                listed = json.loads((evidence / scenario / "result.json").read_text()).get("ledgers")
                if not isinstance(listed, list) or not listed:
                    raise ValueError("no ledgers listed")
            except Exception as error:  # noqa: BLE001
                report["scenarios"][scenario] = f"unknown: result.json ledgers unavailable ({type(error).__name__}: {error})"
                continue
            report["scenarios"][scenario] = "ok"
            for relative in listed:
                ledger = evidence / scenario / relative
                reference = str(ledger.relative_to(evidence)) if ledger.is_relative_to(evidence) else str(ledger)
                if reference in referenced:
                    report["duplicate"].append(f"ledger referenced twice: {reference}")
                    continue
                referenced.append(reference)
                try:
                    entries = json.loads(ledger.read_text())
                    labels = [entry["label"] for entry in entries]
                except Exception as error:  # noqa: BLE001
                    report["missingLedgers"].append(f"{reference}: {type(error).__name__}")
                    report["scenarios"][scenario] = "incomplete"
                    continue
                if not labels:
                    report["emptyLedgers"].append(reference)
                    report["scenarios"][scenario] = "incomplete"
                base = ledger.parent.relative_to(evidence)
                keys = [str(base / label) for label in labels]
                report["duplicate"].extend(sorted({key for key in keys if keys.count(key) > 1}))
                expected.extend(keys)
        observed = {}
        for path in records:
            key = str(path.relative_to(evidence)).removesuffix("-attempt.json")
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                report["bad"].append(f"{key}: {type(error).__name__}")
                continue
            problems = attempt_problems(record, label=Path(key).name)
            if problems:
                report["bad"].append(f"{key}: {'; '.join(problems)}")
                continue
            observed[key] = record
        expected_set = set(expected)
        report["expected"], report["observed"] = len(expected_set), len(observed)
        report["missing"] = sorted(expected_set - set(observed) - {b.split(":")[0] for b in report["bad"]})
        report["unexpected"] = sorted(set(observed) - expected_set)
        for key, record in sorted(observed.items()):
            tree_path = Path(record["tree"]).resolve()
            tree = next((name for name, root in resolved.items() if tree_path == root), "unknown")
            row = {"key": key, "scenario": key.split("/")[0], "tree": tree, "treePath": record["tree"],
                   "treeReason": None if tree != "unknown" else f"{record['tree']} is neither build tree"}
            if tree == "unknown":
                report["unknownTrees"].append(key)
            row.update({field: record.get(field) for field in (
                "label", "status", "startedUtc", "observedUtc", "budgetSeconds", "elapsedSeconds",
                "popenToReadySeconds", "marginSeconds", "overrunSeconds", "exitCode", "diagnosticsSeconds")})
            row.update({"importCoreWall": record["import"].get("coreWall"), "importCoreCpu": record["import"].get("coreCpu"),
                        "importAllWall": record["import"].get("allWall"), "importAllCpu": record["import"].get("allCpu"),
                        "importNote": record["import"].get("note")})
            row["probe"] = probe_observation(evidence, key)
            report["rows"].append(row)
        report["complete"] = bool(report["expected"]) and not any(report[key] for key in (
            "missing", "duplicate", "unexpected", "bad", "missingLedgers", "emptyLedgers", "unknownTrees",
            "shaProblems")) and bool(planned) and all(report["scenarios"].get(name) == "ok" for name in planned)
    except Exception as error:  # noqa: BLE001 - evidence collection must never fail the suite
        report["notes"].append(f"collection failed: {type(error).__name__}: {error}")
        report["complete"] = False
    try:
        (evidence / "readiness.json").write_text(json.dumps(report, indent=2) + "\n")
    except Exception as error:  # noqa: BLE001
        report["notes"].append(f"readiness.json not written: {type(error).__name__}: {error}")
    try:
        ready = [row for row in report["rows"] if row["status"] == "ready"]
        echo(f"READINESS complete={report['complete']} expected={report['expected']} observed={report['observed']} "
             f"ready={len(ready)} minMargin={min((row['marginSeconds'] for row in ready), default=None)}")
    except Exception:  # noqa: BLE001
        pass
    return report


def publish_evidence(directory, results):
    """Keep synthetic raw files on the host; print only non-secret proof fields."""
    for row in results:
        report_path = directory / row["scenario"] / "result.json"
        summary = {key: row[key] for key in ("scenario", "result", "exitCode")}
        if report_path.exists():
            report = json.loads(report_path.read_text())
            summary.update({key: report[key] for key in (
                "head", "productionBefore", "productionAfter", "runnerSha256",
                # Every error the runner kept in its own field survives into the manifest.
                "traceback", "productionBeforeError", "productionCheckError", "cleanupError",
                "interrupted", "ledgersError", "resultWriteError",
            ) if key in report})
            summary["imports"] = {
                str(path.relative_to(directory)): {
                    module: {key: value[key] for key in ("path", "sha256")}
                    for module, value in json.loads(path.read_text()).items()
                }
                for path in sorted(report_path.parent.rglob("daemon-*-imports.json"))
            }
        print(json.dumps(summary), flush=True)
    manifest_path = directory / "manifest.json"
    manifest = {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink() and path != manifest_path
    }
    assert manifest, "no evidence collected"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    for relative, digest in json.loads(manifest_path.read_text()).items():
        assert hashlib.sha256((directory / relative).read_bytes()).hexdigest() == digest, relative
    print(json.dumps({"evidenceRoot": str(directory), "manifest": manifest,
                      "manifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-tree", type=Path, required=True)
    parser.add_argument("--production-socket", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=False)
    warmed = warm_trees({"new": ROOT, "old": args.old_tree}, args.evidence, lambda line: print(line, flush=True))
    if any(report.get("infraAbort") for report in warmed.values()):
        results = [{"scenario": "warm-up", "result": "NOT_RUN: warm-up reap unconfirmed",
                    "exitCode": INFRA_ABORT_EXIT}]
        (args.evidence / "suite.json").write_text(json.dumps(results, indent=2) + "\n")
        publish_evidence(args.evidence, results)
        return INFRA_ABORT_EXIT
    results = []
    code = 0
    for name in SCENARIOS:
        command = [sys.executable, str(ROOT / f"contract/e2e/run-{name}.py"),
                   "--production-socket", str(args.production_socket)]
        if name == "hosted-upgrade":
            command.extend(["--old-tree", str(args.old_tree)])
        with (args.evidence / f"{name}.stdout").open("w") as out, (args.evidence / f"{name}.stderr").open("w") as err:
            result = subprocess.run(command, cwd=ROOT, stdout=out, stderr=err)
        row = {"scenario": name, "exitCode": result.returncode, "command": command}
        report = None
        try:
            report = json.loads((args.evidence / f"{name}.stdout").read_text())
            root = Path(report["root"]).resolve()
            assert root.parent == Path("/tmp").resolve() and root.name.startswith("at01-e2e-")
            shutil.copytree(root, args.evidence / name, symlinks=True)
            row.update(result=report["result"], sourceRoot=str(root))
        except Exception as error:
            row.update(result="INCONCLUSIVE", collectionError=repr(error))
        results.append(row)
        (args.evidence / "suite.json").write_text(json.dumps(results, indent=2) + "\n")
        red = classify_known_failure(name, row, report)
        (args.evidence / "suite.json").write_text(json.dumps(results, indent=2) + "\n")
        if red:
            code = result.returncode or 1
            break
    try:
        collect_readiness(args.evidence, {"new": ROOT, "old": args.old_tree},
                          [row["scenario"] for row in results], lambda line: print(line, flush=True),
                          planned=SCENARIOS)
    except Exception as error:  # noqa: BLE001 - evidence only; keep the scenario exit code
        print(f"READINESS not collected: {type(error).__name__}: {error}", flush=True)
    publish_evidence(args.evidence, results)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
