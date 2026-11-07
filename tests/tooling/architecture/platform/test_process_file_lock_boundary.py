"""Process-lifetime locks come from one wrapper; no POSIX-only module-level imports.

Windows compatibility is a property of the libraries chosen, not of per-call
patches: ``filelock`` owns the flock/LockFileEx split, and only the kernel
wrapper (``kernel/impl/platform/file_lock.py``) may construct it, because the
library defaults (soft fallback, thread-local, unlink on release) are wrong for
an ownership lock.  The second rule catches the anti-pattern that broke
``request_http`` on Windows: an unconditional module-level import of a
POSIX-only stdlib module makes the whole module unimportable there.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "src" / "hyprial"
WRAPPER = SOURCE / "kernel" / "impl" / "platform" / "file_lock.py"
POSIX_ONLY_MODULES = frozenset({"fcntl", "termios", "pwd", "grp", "resource", "posix"})


def _sources() -> list[Path]:
    return sorted(path for path in SOURCE.rglob("*.py") if "_gui" not in path.parts)


def _imported_roots(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        return {node.module.split(".")[0]}
    return set()


def test_only_the_kernel_wrapper_imports_filelock() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in _sources()
        if path != WRAPPER
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if "filelock" in _imported_roots(node)
    ]
    assert offenders == [], (
        "construct process locks through hyprial.kernel.ProcessFileLock, "
        f"not filelock directly: {offenders}"
    )


def test_the_wrapper_pins_the_lifetime_lock_options() -> None:
    tree = ast.parse(WRAPPER.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FileLock"
    ]
    assert len(calls) == 1
    options = {
        keyword.arg: keyword.value.value
        for keyword in calls[0].keywords
        if isinstance(keyword.value, ast.Constant)
    }
    assert options == {
        "thread_local": False,
        "is_singleton": False,
        "fallback_to_soft": False,
        "preserve_lock_file": True,
    }


def test_no_module_level_posix_only_imports() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in _sources()
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if _imported_roots(node) & POSIX_ONLY_MODULES
    ]
    assert offenders == [], (
        "a module-level import of a POSIX-only module makes the module "
        f"unimportable on Windows; import it where it is used: {offenders}"
    )


def test_request_http_config_imports_without_fcntl(monkeypatch, tmp_path) -> None:
    """The Windows ImportError: the module must load with no ``fcntl`` at all."""

    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "fcntl", None)  # ``import fcntl`` -> ImportError
    monkeypatch.delitem(sys.modules, "hyprial.shell.impl.request_http.config", raising=False)
    config = importlib.import_module("hyprial.shell.impl.request_http.config")
    first = config.RuntimeLock(tmp_path)
    second = config.RuntimeLock(tmp_path)
    first.acquire()
    try:
        try:
            second.acquire()
        except config.ConfigFailure as error:
            assert error.code == "RUNTIME_UNAVAILABLE"
        else:  # pragma: no cover - the assertion below names the failure
            raise AssertionError("a second server acquired the runtime lock")
    finally:
        first.release()
    second.acquire()
    second.release()
