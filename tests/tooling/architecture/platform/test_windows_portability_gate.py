"""Executable contract for the Windows-portability anti-pattern gate (W001-W015, W019).

Owner principle (0.5.5.3.4): no Windows-specific code paths in business code;
cross-platform libraries from the start, and CI stops the anti-patterns in
both directions (POSIX-only idioms and Windows-only APIs / platform branches).
The gate is ``scripts/ci/check_windows_portability.py`` with the reviewed,
only-out ``(path, rule)`` count map ``scripts/ci/windows-portability-baseline.json``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_windows_portability.py"
SPEC = importlib.util.spec_from_file_location("check_windows_portability", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

# Ceiling on the baseline's total finding count.  It may only be LOWERED (when
# findings are fixed); raising it to admit a new finding defeats the ratchet.
# The per-key ratchet against the PR base is ``--base-ref`` (gate-python.yml).
MAX_BASELINE_FINDINGS = 350
PROBE = "src/hyprial/daemon/impl/probe.py"


def rules(source: str, path: str = PROBE) -> list[str]:
    return sorted(row["rule"] for row in GATE.scan_source(source, path))


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        # W001 POSIX-only modules, also aliased and through importlib / __import__
        ("import fcntl\n", "W001"),
        ("def f():\n    import pwd\n", "W001"),
        ("import fcntl as f\n", "W001"),
        ("import importlib\nimportlib.import_module('fcntl')\n", "W001"),
        ("__import__('pwd')\n", "W001"),
        # W002 os POSIX-only attributes: plain, aliased, from-imported, getattr without default
        ("import os\nos.killpg(1, 15)\n", "W002"),
        ("import os\nflags = os.O_RDONLY | os.O_NOFOLLOW\n", "W002"),
        ("from os import getuid\n", "W002"),
        ("from os import fork as fk\n", "W002"),
        ("import os as o\no.fork()\n", "W002"),
        ("import os\ngetattr(os, 'fork')()\n", "W002"),
        ("import os\npid, status = os.wait()\n", "W002"),
        ("import os\nos.register_at_fork(after_in_child=f)\n", "W002"),
        ("import multiprocessing\nmultiprocessing.get_context('fork')\n", "W002"),
        # W003 POSIX-only signals and event-loop signal handlers
        ("import signal\nsignal.SIGHUP\n", "W003"),
        ("import signal\ngetattr(signal, 'SIGKILL')\n", "W003"),
        ("import signal\nsignal.Signals.SIGKILL\n", "W003"),
        ("loop.add_signal_handler(2, f)\n", "W003"),
        # W004 inherited fds
        ("import subprocess\nsubprocess.Popen(['x'], pass_fds=(3,))\n", "W004"),
        ("import subprocess\nsubprocess.run(['x'], preexec_fn=f)\n", "W004"),
        # W005 POSIX session knobs: 0 / 1 are real values; aliases, **kwargs, partial count
        ("import subprocess\nsubprocess.Popen(['x'], start_new_session=True)\n", "W005"),
        ("import subprocess\nsubprocess.Popen(['x'], start_new_session=1)\n", "W005"),
        ("import subprocess\nsubprocess.Popen(['x'], process_group=0)\n", "W005"),
        ("import subprocess\nsubprocess.Popen(['x'], process_group=False)\n", "W005"),
        ("from subprocess import Popen as P\nP(['x'], start_new_session=True)\n", "W005"),
        ("kw = {}\nkw['start_new_session'] = True\npopen(['x'], **kw)\n", "W005"),
        ("kw = {'start_new_session': True}\n", "W005"),
        ("kw = dict(process_group=0)\n", "W005"),
        ("import functools, subprocess\nfunctools.partial(subprocess.Popen, start_new_session=True)\n", "W005"),
        ("import subprocess\nsubprocess.run(['x'], user='nobody')\n", "W005"),
        # W006 AF_UNIX
        ("import socket\nsocket.socket(socket.AF_UNIX)\n", "W006"),
        ("import socket\ngetattr(socket, 'AF_UNIX')\n", "W006"),
        ("import asyncio\nasyncio.open_unix_connection('p')\n", "W006"),
        # W007 directory fsync, flag-aware (simple local constants resolved)
        ("import os\ndef f(p):\n    d = os.open(p, os.O_RDONLY)\n    os.fsync(d)\n", "W007"),
        ("import os\ndef f(p):\n    d = os.open(p, os.O_RDONLY | 0)\n    os.fsync(d)\n", "W007"),
        ("import os\ndef f(p):\n    fl = os.O_RDONLY\n    d = os.open(p, fl)\n    os.fsync(d)\n", "W007"),
        ("import os\nos.unlink('x', dir_fd=3)\n", "W008"),
        # W009 / W010 os.kill, also from-imported
        ("import os\nos.kill(pid, 0)\n", "W009"),
        ("from os import kill\nkill(pid, 0)\n", "W009"),
        ("import os, signal\nos.kill(pid, signal.SIGTERM)\n", "W010"),
        ("TMP = '/tmp/hyprial'\n", "W011"),
        ("import os\nos.chmod(path, 0o600)\n", "W012"),
        ("path.chmod(0o700)\n", "W012"),
        ("link.symlink_to(target)\n", "W013"),
        ("from os import symlink\nsymlink(a, b)\n", "W013"),
        # W014 POSIX-only multiplexing
        ("import os\nread, write = os.pipe()\n", "W014"),
        ("from os import pipe\npipe()\n", "W014"),
        ("import select\nselect.poll()\n", "W014"),
        ("loop.add_reader(fd, f)\n", "W014"),
        # W015 platform branches in any direction and spelling
        ("import os\nif os.name == 'nt':\n    pass\n", "W015"),
        ("import os\nif os.name == 'posix':\n    pass\n", "W015"),
        ("import sys\nWINDOWS = sys.platform.startswith('win')\n", "W015"),
        ("import sys\nIS_WIN = sys.platform in ('win32', 'cygwin')\n", "W015"),
        ("import sys\nif sys.platform == 'darwin':\n    pass\n", "W015"),
        ("import sys\nok = 'win' in sys.platform\n", "W015"),
        ("import platform\nok = platform.system().lower() == 'windows'\n", "W015"),
        ("import platform\nsystem = platform.system().lower()\nif system == 'windows':\n    pass\n", "W015"),
        ("import os\nok = getattr(os, 'name') == 'nt'\n", "W015"),
        ("from os import name\nok = name == 'nt'\n", "W015"),
        ("import sys\nmatch sys.platform:\n    case 'win32':\n        pass\n", "W015"),
        ("import sys\nf = {'win32': a}.get(sys.platform)\n", "W015"),
        # W019 Windows-only APIs outside the seam
        ("import msvcrt\n", "W019"),
        ("import winreg as reg\n", "W019"),
        ("import _winapi\n", "W019"),
        ("import win32api\n", "W019"),
        ("import pywintypes\n", "W019"),
        ("import ctypes.wintypes\n", "W019"),
        ("import ctypes\nctypes.windll.kernel32\n", "W019"),
        ("from ctypes import WinDLL\n", "W019"),
        ("import subprocess\nflags = subprocess.CREATE_NO_WINDOW\n", "W019"),
        ("import subprocess\nflags = subprocess.DETACHED_PROCESS\n", "W019"),
        ("import subprocess\ninfo = subprocess.STARTUPINFO()\n", "W019"),
        ("from subprocess import CREATE_NEW_PROCESS_GROUP\n", "W019"),
        ("import subprocess\nflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n", "W019"),
        ("import os\nos.startfile(path)\n", "W019"),
    ],
)
def test_each_unguarded_anti_pattern_is_found(source: str, rule: str) -> None:
    assert rule in rules(source), (rule, GATE.scan_source(source, PROBE))


@pytest.mark.parametrize(
    "source",
    [
        # capability form: the same module AND attribute as the finding
        "import os\nflags = getattr(os, 'O_NOFOLLOW', 0)\n",
        "import os\nif hasattr(os, 'killpg'):\n    os.killpg(1, 15)\n",
        "import os\nif hasattr(os, 'fork'):\n    os.fork()\n",
        "import signal\nsig = getattr(signal, 'SIGKILL', None)\n",
        "import signal\nif getattr(signal, 'SIGKILL', None) is not None:\n    signal.SIGKILL\n",
        "import os\nif all(hasattr(os, f) for f in ('O_NOFOLLOW', 'O_NONBLOCK')):\n"
        "    x = os.O_NOFOLLOW | os.O_NONBLOCK\n",
        "import os\nif os.unlink in os.supports_dir_fd:\n    os.unlink('x', dir_fd=3)\n",
        # handlers that match the finding and do not re-raise
        "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n",
        "import os\ntry:\n    uid = os.getuid()\nexcept AttributeError:\n    uid = None\n",
        "try:\n    loop.add_signal_handler(1, f)\nexcept NotImplementedError:\n    pass\n",
        # early exit before the POSIX-only call
        "import os\ndef f():\n    if not hasattr(os, 'getuid'):\n        return None\n"
        "    return os.getuid()\n",
        # platform guards with the right polarity (W015 is still counted for the test)
        "import os\nif os.name == 'posix':\n    os.fork()\n",
        "import os\nif os.name == 'nt':\n    pass\nelse:\n    os.fork()\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        return\n    os.fork()\n",
        "import os\ndef f():\n    if os.name != 'posix':\n        return\n    os.fork()\n",
        "import sys\nif sys.platform.startswith('linux'):\n    import fcntl\n",
        "import sys\nWIN = sys.platform == 'win32'\nif not WIN:\n    import fcntl\n",
        "import platform\nsystem = platform.system().lower()\nif system == 'linux':\n"
        "    p = '/etc/machine-id'\n",
        "import os, subprocess\nok = os.name != 'nt' and subprocess.Popen(['x'], start_new_session=True)\n",
        # disabled knobs: only None / False (and an empty fd tuple)
        "import subprocess\nsubprocess.Popen(['x'], pass_fds=(), start_new_session=False)\n",
        "import subprocess\nsubprocess.Popen(['x'], process_group=None, start_new_session=None)\n",
        "import os, subprocess\nsubprocess.Popen(['x'], start_new_session=hasattr(os, 'setsid'))\n",
        # select over sockets is fine (ipc/server.py); only os.pipe()/poll/epoll/kqueue are W014
        "import select\nselect.select([sock], [], [], 1.0)\n",
        # docstrings mentioning POSIX paths
        'def f():\n    """Writes under /tmp on POSIX."""\n',
        # a regular-file read in a function that fsyncs something else is not W007
        "import os\ndef f(p, other):\n    fd = os.open(p, os.O_RDONLY)\n    os.read(fd, 1)\n"
        "    os.fsync(other)\n",
        "import os\ndef f(p):\n    fd = os.open(p, os.O_WRONLY | os.O_CREAT)\n    os.fsync(fd)\n",
        # asyncio.run / runner.run with user= is not subprocess
        "import asyncio\nasyncio.run(main(), debug=True)\nrunner.run(user='a')\n",
        # a rebound from-import alias is no longer the os attribute
        "from os import name\ndef f(name):\n    return name == 'nt'\n",
    ],
)
def test_genuine_guards_and_portable_forms_pass(source: str) -> None:
    found = [rule for rule in rules(source) if rule != "W015"]
    assert found == [], GATE.scan_source(source, PROBE)


@pytest.mark.parametrize(
    "source",
    [
        # a hasattr/getattr on another object or attribute is not a capability guard
        "import os\nif hasattr(self, 'x'):\n    os.fork()\n",
        "import os\nif getattr(cfg, 'detach', False):\n    os.setsid()\n",
        "import os\nif hasattr(os, 'killpg'):\n    os.fork()\n",
        "import subprocess\nsubprocess.Popen(['x'], start_new_session=getattr(cfg, 'd', True))\n",
        # an AttributeError handler does not guard an import, nor ImportError an attribute
        "try:\n    import fcntl\nexcept (OSError, AttributeError):\n    pass\n",
        "import os\ntry:\n    os.fork()\nexcept ImportError:\n    pass\n",
        # wrong polarity: the POSIX call is on the Windows side
        "import os\ndef f():\n    if os.name == 'posix':\n        return\n    os.fork()\n",
        "import os\nif os.name == 'posix':\n    pass\nelse:\n    os.fork()\n",
        "import platform\np = '/private/tmp' if platform.system() == 'Darwin' else '/tmp'\n",
        "import select\nif hasattr(select, 'poll'):\n    select.poll()\nelse:\n    select.kqueue()\n",
        # a platform-named flag imported from elsewhere is not a guard
        "import os\nfrom hyprial.kernel import IS_POSIX\nif IS_POSIX:\n    os.fork()\n",
    ],
)
def test_a_guard_on_another_capability_or_the_wrong_side_is_not_a_guard(source: str) -> None:
    assert [rule for rule in rules(source) if rule != "W015"], source


def test_a_guarded_import_that_re_raises_is_not_a_guard() -> None:
    source = "try:\n    import fcntl\nexcept ImportError:\n    raise\n"
    assert rules(source) == ["W001"]


def test_platform_seams_are_exempt_but_windows_code_outside_is_always_counted() -> None:
    seam = "src/hyprial/kernel/impl/platform/file_lock.py"
    source = "import os\nif os.name == 'nt':\n    import msvcrt\nelse:\n    import fcntl\n"
    assert rules(source, seam) == []
    # Outside the seams the branch is itself a finding (W015) and so is the
    # Windows-only import (W019), even though the fcntl import is guarded.
    assert rules(source) == ["W015", "W019"]
    # A value-level guard (lark authorization/process.py) keeps start_new_session
    # out of W005; the os.name test it uses is still a W015 finding.
    value_guard = "import os, subprocess\nsubprocess.Popen(['x'], start_new_session=os.name != 'nt')\n"
    assert rules(value_guard) == ["W015"]
    # The seam's own capability-based directory flush is allowed there only.
    flush = (
        "import os\ndef f(p):\n    d = os.open(p, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))\n"
        "    os.fsync(d)\n"
    )
    assert rules(flush, "src/hyprial/kernel/impl/platform/filesystem/durability.py") == []
    assert rules(flush) == ["W007"]


def _tree(tmp_path: Path, body: str) -> Path:
    source = tmp_path / PROBE
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")
    return tmp_path


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "count": 1,
        "reason": "debt-0.5.5.4: kernel owned-process seam",
        "removal": "0.5.5.4",
    }
    entry.update(overrides)
    return entry


def _document(entries: dict[str, dict[str, object]]) -> str:
    return json.dumps({"version": GATE.BASELINE_VERSION, "entries": entries})


def _baseline(root: Path, entries: dict[str, dict[str, object]]) -> None:
    path = root / GATE.BASELINE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_document(entries), encoding="utf-8")


KILLPG = "import os\ndef f():\n    os.killpg(1, 15)\n"


def test_a_baselined_finding_passes_and_more_or_a_new_key_is_red(tmp_path: Path) -> None:
    root = _tree(tmp_path, KILLPG)
    _baseline(root, {PROBE: {"W002": _entry()}})
    assert GATE.scan(root)["status"] == "PASS"

    # The key is (path, rule): a move to another function is not churn ...
    (root / PROBE).write_text("import os\ndef g():\n    os.killpg(1, 15)\n", encoding="utf-8")
    assert GATE.scan(root)["status"] == "PASS"
    # ... but a second finding under the same key, or a new key, is red.
    (root / PROBE).write_text(KILLPG + "    os.getpgid(1)\n", encoding="utf-8")
    report = GATE.scan(root)
    assert report["status"] == "FAIL"
    assert (report["newFindings"][0]["count"], report["newFindings"][0]["baseline"]) == (2, 1)
    (root / PROBE).write_text(KILLPG + "    os.kill(1, 0)\n", encoding="utf-8")
    assert [row["rule"] for row in GATE.scan(root)["newFindings"]] == ["W009"]


def test_a_fixed_finding_leaves_a_stale_key_that_is_red(tmp_path: Path) -> None:
    root = _tree(tmp_path, "def f():\n    pass\n")
    _baseline(root, {PROBE: {"W002": _entry()}})
    report = GATE.scan(root)
    assert report["status"] == "FAIL"
    assert report["staleBaseline"][0]["actual"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {PROBE: {"W002": _entry(reason="")}},
        {PROBE: {"W002": _entry(reason="because")}},
        {PROBE: {"W002": _entry(removal="")}},
        {PROBE: {"W999": _entry()}},
        {PROBE: {"W002": _entry(count=0)}},
        {PROBE: {"W002": _entry(count=True)}},
        {PROBE: {"W002": {**_entry(), "scope": "f"}}},
        {PROBE: {}},
        {"src/hyprial/kernel/impl/platform/file_lock.py": {"W001": _entry()}},
    ],
)
def test_a_malformed_baseline_fails_closed(tmp_path: Path, bad: dict[str, dict[str, object]]) -> None:
    root = _tree(tmp_path, KILLPG)
    _baseline(root, bad)
    report = GATE.scan(root)
    assert report["status"] == "FAIL"
    assert report["baselineErrors"]


def test_duplicate_baseline_keys_fail_closed(tmp_path: Path) -> None:
    root = _tree(tmp_path, KILLPG)
    entry = json.dumps(_entry())
    text = (
        f'{{"version": {GATE.BASELINE_VERSION}, "entries": '
        f'{{"{PROBE}": {{"W002": {entry}, "W002": {entry}}}}}}}'
    )
    (root / GATE.BASELINE_RELATIVE_PATH).parent.mkdir(parents=True)
    (root / GATE.BASELINE_RELATIVE_PATH).write_text(text, encoding="utf-8")
    assert GATE.scan(root)["baselineErrors"]


def test_prune_only_lowers_the_baseline(tmp_path: Path) -> None:
    root = _tree(tmp_path, KILLPG + "    os.kill(1, 0)\n")
    _baseline(root, {PROBE: {"W002": _entry(count=3)}})
    GATE.prune_baseline(root)
    document = json.loads((root / GATE.BASELINE_RELATIVE_PATH).read_text(encoding="utf-8"))
    # Lowered to the actual count; the unbaselined W009 was NOT added.
    assert {path: {rule: e["count"] for rule, e in by_rule.items()}
            for path, by_rule in document["entries"].items()} == {PROBE: {"W002": 1}}
    assert GATE.scan(root)["status"] == "FAIL"


def test_the_base_comparison_rejects_new_keys_and_raised_counts() -> None:
    base = GATE.parse_baseline(_document({PROBE: {"W002": _entry(count=2)}}))
    same = GATE.parse_baseline(_document({PROBE: {"W002": _entry(count=1)}}))
    assert GATE.compare_with_base(same, base) == []
    raised = GATE.parse_baseline(_document({PROBE: {"W002": _entry(count=3)}}))
    assert "raised" in GATE.compare_with_base(raised, base)[0]
    # Swapping a fixed finding for a new one elsewhere keeps the total but is red.
    swapped = GATE.parse_baseline(
        _document({PROBE: {"W002": _entry(count=1), "W009": _entry(count=1)}})
    )
    assert "new baseline key" in GATE.compare_with_base(swapped, base)[0]


def test_the_base_ref_is_read_from_git(tmp_path: Path) -> None:
    root = _tree(tmp_path, KILLPG + "    os.kill(1, 0)\n")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    _baseline(root, {PROBE: {"W002": _entry()}})
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    _baseline(root, {PROBE: {"W002": _entry(), "W009": _entry()}})
    assert GATE.scan(root)["status"] == "PASS"
    report = GATE.scan(root, base_ref="HEAD")
    assert report["status"] == "FAIL"
    assert any("new baseline key" in error for error in report["baselineErrors"])
    assert GATE.scan(root, base_ref="no-such-ref")["baselineErrors"]


def test_the_repository_tree_has_no_new_or_stale_findings() -> None:
    report = GATE.scan(ROOT)
    assert report["parseErrors"] == []
    assert report["baselineErrors"] == []
    assert report["newFindings"] == [], (
        "new Windows-incompatible pattern(s); write them the portable way "
        "(see RULES in scripts/ci/check_windows_portability.py) instead of a "
        f"per-call Windows patch: {report['newFindings']}"
    )
    assert report["staleBaseline"] == [], (
        "fixed findings: run scripts/ci/check_windows_portability.py --prune-baseline "
        f"and lower MAX_BASELINE_FINDINGS: {report['staleBaseline']}"
    )


def test_the_baseline_only_shrinks_and_every_entry_is_reviewed() -> None:
    entries = GATE.load_baseline(ROOT / GATE.BASELINE_RELATIVE_PATH)
    assert sum(entry["count"] for entry in entries.values()) <= MAX_BASELINE_FINDINGS
    for (path, _rule), entry in entries.items():
        assert entry["reason"].startswith(GATE.REASON_PREFIXES), (path, entry)
        assert not GATE.in_seam(path), path


def test_rule_ids_agree_across_gate_ci_and_pr_template() -> None:
    assert sorted(GATE.AST_RULES) == [f"W{n:03d}" for n in (*range(1, 16), 19)]
    workflow = (ROOT / ".forgejo" / "workflows" / "gate-python.yml").read_text(encoding="utf-8")
    template = (ROOT / ".forgejo" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "W001-W015, W019" in workflow and "--base-ref" in workflow
    for rule in ("W015", "W017", "W018", "W019"):
        assert rule in template, rule
    assert "W001-W015 and W019" in (GATE.__doc__ or "")


def test_the_fixed_0_5_5_3_4_blockers_stay_out_of_the_baseline() -> None:
    """The four Windows start/doctor blockers are fixed, not grandfathered."""

    keys = set(GATE.load_baseline(ROOT / GATE.BASELINE_RELATIVE_PATH))
    for blocked in [
        ("src/hyprial/shell/impl/cli/commands/common/daemon_start.py", "W004"),
        ("src/hyprial/daemon/impl/upgrade/runner.py", "W007"),
        ("src/hyprial/daemon/impl/upgrade/steps/mcp_entry.py", "W007"),
        ("src/hyprial/shell/impl/request_http/config.py", "W001"),
    ]:
        assert blocked not in keys, blocked
