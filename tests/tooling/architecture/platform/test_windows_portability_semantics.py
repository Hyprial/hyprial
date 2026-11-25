"""Executable contract for the meaning-level rules of the Windows-portability gate.

``scripts/ci/windows_portability_semantics.py`` recognises interface example
data (W011), read-only toggles (W012) and platform rejections (W021) by what
the code means, never by file or line; ``getattr(os, "O_BINARY", 0)`` is the
capability form of a Windows-only open flag (W019).  W021 is enforced with an
only-out baseline and exempt only inside ``src/hyprial/kernel/impl/manysys/``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_windows_portability.py"
SPEC = importlib.util.spec_from_file_location("check_windows_portability_semantics_probe", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

PROBE = "src/hyprial/daemon/impl/probe.py"
EXAMPLES = "src/hyprial/daemon/impl/request_catalog/probe/examples.py"
SEAM = "src/hyprial/kernel/impl/manysys/probe.py"
OLD_PLATFORM = "src/hyprial/kernel/impl/platform/probe.py"
OLD_OWNED_PROCESS = "src/hyprial/kernel/impl/processes/owned_process.py"


def rules(source: str, path: str = PROBE) -> list[str]:
    return sorted(row["rule"] for row in GATE.scan_source(source, path))


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("E = [MethodExample(name='x', params={'path': '/tmp'})]\n", PROBE),
        ("E = (MethodExampleError(1, 'm', data={'p': ['/tmp/hyprial-x']}),)\n", PROBE),
        ("P = '/tmp/hyprial-example/state/agents.sqlite3'\n", EXAMPLES),
        ("ROWS = [{'destination': '/tmp/hyprial-write-example/output.bin'}]\n", EXAMPLES),
    ],
    ids=["constructor", "nested-constructor", "example-module-constant", "example-module-container"],
)
def test_interface_example_data_is_not_a_posix_path(source: str, path: str) -> None:
    assert "W011" not in rules(source, path)


@pytest.mark.parametrize(
    ("source", "path"),
    [
        ("P = '/tmp/hyprial-example/x'\n", PROBE),
        ("P = '/tmp/real-state'\n", EXAMPLES),
        ("def f():\n    return '/tmp/hyprial-example/x'\n", EXAMPLES),
        ("E = MethodExample(params=Path('/tmp/x'))\n", PROBE),
        ("E = {'path': '/tmp/x'}\n", PROBE),
        ("'/tmp/hyprial-example/x'.strip()\n", EXAMPLES),
    ],
    ids=["outside-example-module", "not-self-identified", "inside-function", "other-call", "plain-dict",
         "expression"],
)
def test_a_real_posix_path_still_counts(source: str, path: str) -> None:
    assert "W011" in rules(source, path)


TOGGLE = (
    "import os\n"
    "class Tree:\n"
    "    def read_only(self):\n"
    "        for current, dirs, files in os.walk(self.root):\n"
    "            os.chmod(current, 0o555)\n"
    "    def writable(self):\n"
    "        for current, dirs, files in os.walk(self.root):\n"
    "            os.chmod(current, 0o700)\n"
)


def test_the_write_restore_half_of_a_read_only_toggle_is_not_a_privacy_mode() -> None:
    assert "W012" not in rules(TOGGLE)


@pytest.mark.parametrize(
    "source",
    [
        TOGGLE.replace("0o555", "0o600"),
        TOGGLE.replace("for current, dirs, files in os.walk(self.root):\n            os.chmod(current, 0o700)",
                       "os.chmod(self.root, 0o700)"),
        "import os\ndef writable(root):\n    for c, d, f in os.walk(root):\n        os.chmod(c, 0o700)\n",
        "import os\ndef same_function(root):\n    for c, d, f in os.walk(root):\n"
        "        os.chmod(c, 0o555)\n        os.chmod(c, 0o700)\n",
        TOGGLE + "    def mixed(self):\n        for c, d, f in os.walk(self.root):\n"
        "            os.chmod(c, 0o555)\n            os.chmod(c, 0o700)\n",
    ],
    ids=["sibling-not-read-only", "restore-without-walk", "no-sibling", "same-function-bypass",
         "mixed-function-despite-sibling"],
)
def test_a_private_chmod_without_a_distinct_toggle_sibling_still_counts(source: str) -> None:
    assert "W012" in rules(source)


def test_a_windows_only_open_flag_read_as_a_zero_bit_is_the_capability_form() -> None:
    assert rules("import os\nflags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)\n") == []
    assert rules("import os\nflags = os.O_BINARY\n") == ["W019"]
    assert rules("import os\nflags = getattr(os, 'O_BINARY', 1)\n") == ["W019"]
    assert rules("import subprocess\nflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n") == ["W019"]


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef f():\n    if os.name == 'nt':\n        raise RuntimeError('x')\n",
        "import os\ndef f(flag):\n    if os.name == 'nt':\n        if flag:\n            raise RuntimeError('x')\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        raise RuntimeError('x')\n        cleanup()\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        try:\n            prep()\n            raise RuntimeError('x')\n        finally:\n            done()\n",
        "import os\ndef f():\n    if getattr(os, 'name', None) == 'nt':\n        raise RuntimeError('x')\n",
        "import sys\ndef f():\n    if getattr(sys, 'platform', '') == 'win32':\n        raise OSError('x')\n",
        "import sys\ndef f():\n    if sys.platform == 'win32':\n        raise OSError('x')\n",
        "import sys\ndef f():\n    if sys.platform.startswith('win'):\n        raise OSError('x')\n",
        "import os\ndef f():\n    if os.name != 'posix':\n        raise RuntimeError('x')\n",
        "import os\ndef f():\n    if os.name == 'posix':\n        return 1\n    else:\n        raise RuntimeError('x')\n",
        "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n"
        "def f():\n    if fcntl is None:\n        raise RuntimeError('x')\n",
        "try:\n    import fcntl as _f\nexcept ImportError:\n    _f = None\ndef f():\n    if not _f:\n        raise RuntimeError('x')\n",
        "import os\ndef f():\n    if not hasattr(os, 'fork'):\n        raise RuntimeError('x')\n",
        "import os\ndef f():\n    getuid = getattr(os, 'getuid', None)\n    if getuid is None:\n        raise RuntimeError('x')\n",
        "import signal\nKILL = getattr(signal, 'pthread_kill', None)\ndef f():\n    if KILL is None:\n        raise RuntimeError('x')\n",
        "import os\ndef f():\n    if not all(hasattr(os, n) for n in ('O_NOFOLLOW', 'O_DIRECTORY')):\n        raise ValueError('x')\n",
        "try:\n    import pwd\nexcept ImportError:\n    raise SystemExit('x')\n",
        "import os\nassert os.name == 'posix'\n",
        "def f():\n    raise Error(ipc_errors.UNSUPPORTED, 'descriptors are not supported on Windows')\n",
        "def f():\n    raise RuntimeError('Windows unsupported')\n",
        "def f():\n    raise RuntimeError('Windows is unsupported')\n",
        "def f():\n    raise RuntimeError('feature requires a POSIX host')\n",
        "def f():\n    raise RuntimeError('operation is unsupported on this host')\n",
        "import sys\ndef f():\n    raise ServicePlatformUnsupported(sys.platform)\n",
        "def f():\n    raise RuntimeError('bootstrap identity descriptor handoff requires POSIX')\n",
        "def f():\n    raise CliError('SERVICE_PLATFORM_UNSUPPORTED', 'x')\n",
        "def f():\n    raise Error(SERVICE_PLATFORM_UNSUPPORTED)\n",
        "def f():\n    raise Error(errors.SERVICE_PLATFORM_UNSUPPORTED, 'x')\n",
        "def f():\n    raise Error(WINDOWS_UNSUPPORTED)\n",
        "def f():\n    raise Error(ErrorCode.WINDOWS_UNSUPPORTED.value)\n",
        "def f():\n    raise Error(POSIX_REQUIRED)\n",
        "def f():\n    raise Error('x', code=SERVICE_PLATFORM_UNSUPPORTED)\n",
        "def f():\n    raise WindowsUnsupportedError\n",
        "def f():\n    raise PosixRequiredError('x')\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        raise Error(ipc_errors.UNSUPPORTED)\n",
        "import sys\ndef f():\n    if sys.platform != 'win32':\n        return 1\n    else:\n        raise Error(ipc_errors.UNSUPPORTED)\n",
        "import sys\ndef f():\n    if sys.platform != 'win32':\n        return 1\n    raise Error(ipc_errors.UNSUPPORTED)\n",
        "def f():\n    raise E(f'privacy is unsupported on this platform: {p}')\n",
        "def f():\n    raise E('needs no-follow I/O that this platform does not provide')\n",
        "def f():\n    raise GuiError(INVALID, '--stdin-stop is only available on POSIX systems')\n",
    ],
    ids=[
        "os-name-nt", "nested-if-in-nt", "non-tail-raise-in-nt", "try-raise-in-nt",
        "getattr-os-name-default", "getattr-sys-platform-default",
        "sys-platform-win32", "startswith-win", "os-name-not-posix", "else-of-posix",
        "module-is-none", "module-falsy-alias", "missing-hasattr", "missing-getattr-copy", "module-level-copy",
        "all-capabilities", "import-error-raise", "assert-posix", "message-not-supported-on-windows",
        "message-windows-unsupported", "message-windows-is-unsupported", "message-requires-posix-host",
        "message-unsupported-on-this-host",
        "platform-unsupported-name", "message-requires-posix", "platform-unsupported-code",
        "platform-unsupported-symbol", "platform-unsupported-attr-symbol",
        "windows-unsupported-symbol", "windows-unsupported-enum-value",
        "posix-required-symbol", "platform-unsupported-kwarg-symbol",
        "bare-windows-unsupported-exc", "posix-required-exc",
        "guarded-nt-ipc-unsupported", "guarded-else-win32-ipc-unsupported",
        "guarded-early-return-win32-ipc-unsupported",
        "message-on-this-platform", "message-platform-does-not-provide", "message-only-on-posix",
    ],
)
def test_a_raise_that_turns_windows_away_is_w021(source: str) -> None:
    assert rules(source).count("W021") == 1, GATE.scan_source(source, PROBE)


@pytest.mark.parametrize(
    "source",
    [
        "import sys\ndef f():\n    if sys.platform == 'darwin':\n        raise RuntimeError('x')\n",
        "import sys\ndef f():\n    if getattr(sys, 'platform', '') == 'darwin':\n        raise RuntimeError('x')\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        return 1\n    return 2\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        return 1\n        raise RuntimeError('unreachable')\n",
        "import os\ndef f():\n    if hasattr(os, 'fork'):\n        raise RuntimeError('already forked')\n",
        "def f(resource):\n    if resource is None:\n        raise KeyError('x')\n",
        "def f():\n    raise ConnectionError('Windows SDK requires a Job-owned launch')\n",
        "def f():\n    raise RuntimeError('messaging platform provenance requires POSIX credentials')\n",
        "def f(requires_posix_token):\n    raise RuntimeError(requires_posix_token)\n",
        "def f():\n    raise Error('managed inbound TCP is only supported on Windows')\n",
        "def f():\n    raise TypeError('platform provenance encoder requires an authoritative typed arm')\n",
        "def f():\n    raise ValueError('unsupported Slack event type')\n",
        "def f():\n    raise Error(ipc_errors.UNSUPPORTED)\n",
        "def f():\n    raise Error(ipc_errors.UNSUPPORTED, 'unknown method')\n",
        "def f():\n    raise Error(ipc_errors.UNSUPPORTED_TARGET, 'group target')\n",
        "def f():\n    raise Error(ipc_errors.UNSUPPORTED_CAPABILITY)\n",
        "def f():\n    raise Error(UNSUPPORTED, 'unknown method')\n",
        "import sys\ndef f(bad):\n    if sys.platform == 'darwin':\n        return 1\n    if bad:\n        raise Error(ipc_errors.UNSUPPORTED)\n    return 2\n",
        "def f():\n    raise E('requires a private Unix listener or a Windows loopback TCP listener')\n",
        "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n",
        "import os\ndef f():\n    if os.name == 'nt':\n        log('x')\n    raise RuntimeError('y')\n",
    ],
    ids=[
        "rejects-a-posix-platform", "getattr-rejects-darwin", "platform-branch-without-raise",
        "dead-raise-after-return", "capability-present", "unimported-name",
        "windows-positive-precondition", "messaging-platform-posix-credentials", "dynamic-var-name",
        "windows-only-supported-positive", "messaging-platform-noun", "domain-unsupported",
        "unguarded-ipc-unsupported", "unguarded-ipc-unsupported-with-msg",
        "unguarded-ipc-unsupported-target", "unguarded-ipc-unsupported-capability",
        "unguarded-bare-unsupported", "darwin-early-return-then-domain-unsupported",
        "windows-listener-option", "guarded-import-fallback", "raise-after-the-branch",
    ],
)
def test_a_raise_that_does_not_refuse_windows_is_not_w021(source: str) -> None:
    assert "W021" not in rules(source), GATE.scan_source(source, PROBE)


def test_one_raise_is_one_w021_and_only_manysys_is_exempt() -> None:
    source = "import os\ndef f():\n    if os.name == 'nt':\n        raise RuntimeError('not supported on Windows')\n"
    assert GATE.PLATFORM_SEAMS == ("src/hyprial/kernel/impl/manysys/",)
    assert rules(source) == ["W015", "W021"]
    assert rules(source, SEAM) == []
    assert rules(source, OLD_PLATFORM) == ["W015", "W021"]
    assert rules(source, OLD_OWNED_PROCESS) == ["W015", "W021"]


def _tree(tmp_path: Path, entries: dict[str, dict[str, object]]) -> Path:
    source = tmp_path / PROBE
    source.parent.mkdir(parents=True)
    source.write_text("import os\ndef f():\n    if not hasattr(os, 'fork'):\n        raise RuntimeError('x')\n",
                      encoding="utf-8")
    baseline = tmp_path / GATE.BASELINE_RELATIVE_PATH
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"version": GATE.BASELINE_VERSION, "entries": entries}), encoding="utf-8")
    return tmp_path


def test_w021_is_enforced_by_default_and_advisory_mode_stays_tested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert GATE.ADVISORY_RULES == frozenset()
    entry = {"count": 1, "reason": "debt-0.5.5.4: kernel seam", "removal": "0.5.5.4"}
    assert GATE.scan(_tree(tmp_path / "unbaselined", {}))["status"] == "FAIL"
    assert GATE.scan(_tree(tmp_path / "baselined", {PROBE: {"W021": entry}}))["status"] == "PASS"

    monkeypatch.setattr(GATE, "ADVISORY_RULES", frozenset({"W021"}))
    root = _tree(tmp_path / "advisory", {})
    report = GATE.scan(root)
    assert report["status"] == "PASS"
    assert [row["rule"] for row in report["advisoryFindings"]] == ["W021"]
    assert GATE.main(["--root", str(root)]) == 0
    assert f"ADVISORY W021 {PROBE}:4" in capsys.readouterr().out
    stale = GATE.scan(_tree(tmp_path / "stale", {PROBE: {"W021": entry}}))
    assert stale["status"] == "FAIL"
    assert [row["rule"] for row in stale["staleBaseline"]] == ["W021"]


def test_the_gate_no_longer_recommends_psutil() -> None:
    assert "psutil" not in (GATE.__doc__ or "")
    assert not [rule for rule, (_name, remedy) in GATE.RULES.items() if "psutil" in remedy]
    assert "psutil" not in (ROOT / GATE.BASELINE_RELATIVE_PATH).read_text(encoding="utf-8")
