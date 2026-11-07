"""W016: every ``hyprial`` module imports with the POSIX-only stdlib surface removed.

The AST gate (``scripts/ci/check_windows_portability.py``) sees direct uses; it
cannot see a module that touches ``fcntl``/``signal.SIGHUP``/``socket.AF_UNIX``
at import time through a helper.  This smoke runs on Linux CI: in an isolated
(``-I``) interpreter it blocks the POSIX-only modules, deletes the POSIX-only
``os``/``signal``/``socket`` attributes, presents ``os.name == "nt"`` with the
win32 extension modules stubbed (so correctly dispatched seams take their
Windows branch), and imports every module under ``src/hyprial``.

A failure is attributed to the innermost ``hyprial`` frame (``path`` + message).
The same import run on the unmodified interpreter is subtracted, so a module
that cannot import for an unrelated reason (an optional build dependency, a
worker that exits by design) is not a Windows finding.  Every remaining root
cause must be a ``(path, "W016")`` key in
``scripts/ci/windows-portability-baseline.json`` with ``count`` = the number of
modules it breaks -- a new root cause or a new importer of a broken module is
red, a fixed one is stale (only-out ratchet, like the AST gate).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BASELINE = ROOT / "scripts" / "ci" / "windows-portability-baseline.json"

_SMOKE = r'''
import importlib, json, os, pathlib, sys, traceback, types
root = pathlib.Path(sys.argv[1]); mode = sys.argv[2]
sys.path.insert(0, str(root / "src"))
# Stdlib/third-party modules that legitimately use POSIX APIs internally load first.
import subprocess, asyncio, tempfile, shutil, multiprocessing, selectors, select, getpass  # noqa
import uuid, ssl, logging, sqlite3, http.client, urllib.request, concurrent.futures  # noqa
import platform, ctypes, signal, socket  # noqa
for name in ("typer", "click", "pydantic", "httpx", "filelock", "mcp", "zenoh"):
    try:
        importlib.import_module(name)
    except Exception:
        pass
if mode == "windows":
    for module in ("fcntl", "termios", "pwd", "grp", "resource", "pty", "tty", "posix",
                   "crypt", "syslog", "readline"):
        sys.modules[module] = None
    for attr in ("fork", "forkpty", "setsid", "setpgid", "getpgid", "setpgrp", "killpg",
                 "getuid", "geteuid", "getgid", "getegid", "getgroups", "chown", "lchown",
                 "fchown", "mkfifo", "mknod", "fchmod", "sync", "getloadavg", "uname",
                 "statvfs", "sysconf", "openpty", "WNOHANG", "O_NOFOLLOW", "O_CLOEXEC",
                 "O_DIRECTORY", "O_NONBLOCK", "O_NOCTTY"):
        if hasattr(os, attr):
            delattr(os, attr)
    for attr in ("SIGKILL", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGCHLD", "SIGQUIT", "SIGALRM",
                 "SIGPIPE", "SIGSTOP", "SIGCONT", "SIGTSTP", "SIGWINCH"):
        if hasattr(signal, attr):
            delattr(signal, attr)
    if hasattr(socket, "AF_UNIX"):
        delattr(socket, "AF_UNIX")

    class _Any:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return _Any()
        def __getattr__(self, name): return _Any()
        def __or__(self, other): return 0
        __ror__ = __or__
        def __int__(self): return 0
        __index__ = __int__

    class _Stub(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return _Any()

    for module in ("msvcrt", "winreg", "_winapi", "win32api", "win32job", "win32pipe",
                   "win32file", "win32con", "win32event", "win32security", "win32process",
                   "pywintypes", "win32com", "win32com.client", "win32com.shell",
                   "ntsecuritycon", "winerror", "win32service", "win32serviceutil", "win32ts",
                   "win32profile"):
        sys.modules[module] = _Stub(module)
    if not hasattr(ctypes, "WinDLL"):
        ctypes.WinDLL = lambda *a, **k: _Any()
        ctypes.windll = _Any()
        ctypes.WinError = lambda *a: OSError()
    # Path() must stay constructible on the POSIX host while os.name says "nt".
    pathlib.Path.__new__ = lambda cls, *a, **k: object.__new__(
        pathlib.PosixPath if cls is pathlib.Path else cls)
    os.name = "nt"
    sys.platform = "win32"
package = root / "src" / "hyprial"
failures = {}
for path in sorted(package.rglob("*.py")):
    parts = list(path.relative_to(package.parent).with_suffix("").parts)
    if "_gui" in parts or parts[-1] == "__main__":
        continue
    if parts[-1] == "__init__":
        parts = parts[:-1]
    name = ".".join(parts)
    try:
        importlib.import_module(name)
    except BaseException as error:  # noqa: BLE001 -- SystemExit included
        frames = [f for f in traceback.extract_tb(error.__traceback__)
                  if "/src/hyprial/" in f.filename.replace("\\", "/")]
        origin = frames[-1].filename.replace("\\", "/").split("/src/", 1)[-1] if frames else "?"
        failures[name] = {"path": "src/" + origin,
                          "symbol": f"{type(error).__name__}: {str(error)[:120]}"}
print(json.dumps(failures))
'''


def _import_failures(mode: str) -> dict[str, dict[str, str]]:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _SMOKE, str(ROOT), mode],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def windows_import_root_causes() -> Counter[str]:
    plain = _import_failures("plain")
    simulated = _import_failures("windows")
    return Counter(
        failure["path"] for module, failure in simulated.items() if module not in plain
    )


def _baseline() -> dict[str, int]:
    """``(path, "W016") -> count`` keys of the gate's count map."""

    document = json.loads(BASELINE.read_text(encoding="utf-8"))
    return {
        path: by_rule["W016"]["count"]
        for path, by_rule in document["entries"].items()
        if "W016" in by_rule
    }


def test_every_module_imports_without_posix_only_surface_beyond_the_baseline() -> None:
    actual = windows_import_root_causes()
    allowed = _baseline()
    new = {key: count for key, count in actual.items() if count > allowed.get(key, 0)}
    stale = {key: count for key, count in allowed.items() if count > actual.get(key, 0)}
    assert not new, (
        "module(s) no longer import on Windows (POSIX-only access at import time); "
        f"defer it to call time / use a capability check: {new}"
    )
    assert not stale, f"lower or delete these W016 baseline entries: {stale}"


def test_the_simulation_does_break_a_module_level_posix_access(tmp_path: Path) -> None:
    """The smoke is not vacuous: blocked attributes really fail at import."""

    probe = tmp_path / "probe.py"
    probe.write_text("import signal\nHANGUP = signal.SIGHUP\n", encoding="utf-8")
    script = _SMOKE.split("package = root")[0] + (
        "sys.path.insert(0, sys.argv[3])\n"
        "try:\n    import probe\nexcept AttributeError as e:\n    print('blocked', e)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT), "windows", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert "blocked" in completed.stdout, completed.stderr[-2000:]
