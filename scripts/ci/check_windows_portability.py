#!/usr/bin/env python3
"""Windows-portability anti-pattern gate (only-out ratchet), pure stdlib AST.

Owner principle (0.5.5.3.4): Windows compatibility is not patched call site by
call site.  Code uses libraries and stdlib APIs that are cross-platform by
construction (``filelock``, ``multiprocessing.connection``, ``platformdirs``,
``keyring``, the kernel seams such as ``probe_process`` and ``OwnedProcessGroup``), and CI stops the anti-patterns
in BOTH directions: POSIX-only idioms (W001-W014) and Windows-specific code
(W015 platform branches, W019 Windows-only APIs, W020 raw local IPC, W021 a
raise that turns Windows away) outside the platform seams.

Rule ids: W001-W015 and W019-W021 are AST rules in this file; W016 is the
import smoke (``tests/tooling/architecture/platform/test_windows_import_smoke.py``,
its baseline entries are validated here and compared there); W017/W018 are
review-checklist rules (``.github/PULL_REQUEST_TEMPLATE.md``), not AST rules.

The baseline ``scripts/ci/windows-portability-baseline.json`` is a count map
keyed by ``(path, rule)``; every key carries a reviewed ``reason`` and a
``removal`` target.  Its polarity is fixed -- keys may only LEAVE:

* a ``(path, rule)`` with more findings than its ``count`` (or with no key) is
  red: write it the portable way instead;
* a key whose ``count`` is above the actual number is red (stale) until the
  baseline is lowered (``--prune-baseline`` only ever lowers);
* ``--base-ref REF`` compares the baseline with the one at ``REF`` (the PR
  base): a key that is not in the base, or a count above the base's, is red;
* a malformed baseline (unknown rule, missing reason/removal, duplicate key)
  fails closed.

A POSIX-only finding (W001-W014) is not counted behind a guard that tests the
SAME capability, with the right polarity:

* platform: ``os.name`` / ``sys.platform`` / ``platform.system()`` comparisons
  (also through ``.lower()``, ``startswith``, ``in``, ``getattr(os, "name")`` or a
  local variable holding one of those) whose truth implies "not Windows" --
  ``if os.name == "posix": return`` does NOT guard the POSIX call after it;
* capability: ``hasattr(os, "fork")``, ``getattr(signal, "SIGKILL", None)``,
  ``os.supports_*`` -- the module AND attribute of the finding; a ``hasattr``
  on anything else is not a guard;
* ``try`` whose body holds the finding: ``ImportError``/``ModuleNotFoundError``
  guards an import, ``AttributeError`` a module attribute, ``NotImplementedError``
  an event-loop capability -- and the handler does not simply re-raise;
* an earlier ``if <guard>: return/raise/continue/break`` (or ``assert``) in the
  same block.

``getattr(os, "O_NOFOLLOW", 0)`` is the capability form and never matches; so is a
Windows-only open flag read as ``getattr(os, "O_BINARY", 0)`` (a zero bit elsewhere);
``getattr(os, "fork")`` without a default is the attribute itself.  Files in
``PLATFORM_SEAMS`` are exempt from every rule: that is where platform
differences are allowed to live.  W015 and W019 are counted everywhere outside
the seams, guarded or not -- they ARE the Windows code business code must not
write.

W020 (raw local IPC) is counted the same way -- guarded or not, because a
guarded ``AF_UNIX`` still breaks on Windows -- and it keeps NO baseline entry:
a baseline key with it is a format error, so its count can only be zero.  It
matches ``socket.AF_UNIX`` in every spelling (attribute, from-import,
``hasattr``/``getattr`` probing), the asyncio and event-loop unix endpoints,
``socketserver.Unix*Server``, and the Windows named-pipe API (``connect_named_pipe``
/ ``listen_named_pipe`` imported or called anywhere, the ``windows_pipe`` module,
``win32pipe``).  Local IPC goes through ``hyprial.kernel.listen_local_ipc`` /
``connect_local_ipc``; W006 keeps its own guard-aware semantics unchanged.

W011 interface example data, W012 read-only toggles and W021 platform
rejections are recognised by meaning, not by line: see
``windows_portability_semantics.py`` beside this file.  W021 is counted guarded
or not (the guard IS the rejection).  ``ADVISORY_RULES`` are reported
(``advisoryFindings``, ``ADVISORY`` lines) but do not fail the gate yet: a new
rule stays advisory until the owner approves its baseline entries, then leaves
the set in the same change that adds them.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import windows_portability_semantics as semantics
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import windows_portability_semantics as semantics

SCAN_ROOTS = ("src/hyprial",)
# Vendored GUI payload, not first-party Python.
EXCLUDED_PARTS = frozenset({"_gui", "__pycache__"})
BASELINE_RELATIVE_PATH = "scripts/ci/windows-portability-baseline.json"
BASELINE_VERSION = 2
ENTRY_FIELDS = frozenset({"count", "reason", "removal"})
REASON_PREFIXES = ("debt-", "posix-only-module:", "lock-")
# Where platform differences may live (every rule is exempt there).
PLATFORM_SEAMS = (
    "src/hyprial/kernel/impl/manysys/",
)

RULES: dict[str, tuple[str, str]] = {
    "W001": ("posix-module-import", "filelock / getpass / pathlib, or a kernel seam"),
    "W002": ("os-posix-api", "getattr(os, 'O_X', 0); kernel private_access / OwnedProcessGroup"),
    "W003": ("signal-posix", "Popen.terminate()/kill(); getattr(signal, 'SIGHUP', None)"),
    "W004": ("subprocess-pass_fds", "no inherited fds: multiprocessing.connection or the kernel IPC client"),
    "W005": ("subprocess-posix-session", "one kernel spawn_detached() seam / an OS supervisor"),
    "W006": ("af-unix", "kernel IPC client or multiprocessing.connection (AF_PIPE/AF_UNIX)"),
    "W007": ("dir-fsync-open", "kernel fsync_directory(path)"),
    "W008": ("dir-fd", "pathlib + kernel private_access no-follow seam"),
    "W009": ("os-kill-zero-probe", "kernel probe_process"),
    "W010": ("os-kill-signal", "Popen.terminate()/kill() or the kernel owned-process seam (OwnedProcessGroup)"),
    "W011": ("posix-path-literal", "tempfile.gettempdir() / platformdirs / shutil.which / the tool's own report"),
    "W012": ("chmod-private-mode", "kernel harden_private_path / private_metadata_violation; keyring"),
    "W013": ("symlink-create", "no link (config/env names the path) or one kernel link_directory() seam"),
    "W014": ("posix-io-multiplex", "socket.socketpair() / multiprocessing.connection.Pipe / asyncio streams"),
    "W015": ("platform-branch-outside-seam", "capability checks or a kernel seam's platform-neutral API"),
    "W016": ("import-time-posix-access", "defer to call time, capability access"),
    "W019": ("windows-only-api-outside-seam", "a cross-platform library or one kernel seam owning the Windows API"),
    "W020": ("raw-local-ipc", "hyprial.kernel.listen_local_ipc / connect_local_ipc (the one local-IPC seam)"),
    "W021": ("platform-rejection-raise", "a portable library or kernel seam; delete the POSIX-only shell"),
}
AST_RULES = frozenset(rule for rule in RULES if rule != "W016")
ADVISORY_RULES: frozenset[str] = frozenset()
UNGUARDED_RULES = frozenset({"W015", "W019", "W020", "W021"})

POSIX_MODULES = frozenset(
    {"fcntl", "termios", "pwd", "grp", "resource", "pty", "tty", "posix", "crypt", "syslog", "readline"}
)
WINDOWS_MODULES = frozenset(
    {"msvcrt", "winreg", "_winreg", "_winapi", "winsound", "nt", "_overlapped", "pywintypes",
     "pythoncom", "wmi", "ntsecuritycon", "winerror"}
)
OS_POSIX_ATTRS = frozenset(
    {
        "fork", "forkpty", "setsid", "setpgid", "getpgid", "setpgrp", "getpgrp", "killpg", "getsid",
        "getuid", "geteuid", "getgid", "getegid", "getgroups", "setuid", "setgid", "seteuid",
        "setegid", "initgroups", "chown", "lchown", "fchown", "mkfifo", "mknod", "fchmod", "lchmod",
        "sync", "getloadavg", "uname", "statvfs", "fstatvfs", "sysconf", "fpathconf", "pathconf",
        "ttyname", "openpty", "pipe2", "nice", "wait", "wait3", "wait4", "waitid", "posix_spawn",
        "posix_spawnp", "pidfd_open", "register_at_fork", "sendfile", "getpriority", "setpriority",
        "sched_getaffinity", "sched_setaffinity", "major", "minor", "makedev",
        "WNOHANG", "WUNTRACED", "WIFEXITED", "WEXITSTATUS", "WIFSIGNALED", "WTERMSIG",
        "WIFSTOPPED", "WSTOPSIG", "O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY", "O_NONBLOCK",
        "O_NOCTTY", "O_SYNC", "O_DSYNC",
    }
)
SIGNAL_POSIX = frozenset(
    {
        "SIGKILL", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGCHLD", "SIGQUIT", "SIGALRM", "SIGPIPE",
        "SIGSTOP", "SIGCONT", "SIGTSTP", "SIGWINCH", "SIGTTIN", "SIGTTOU", "SIGIO", "SIGPROF",
        "alarm", "setitimer", "getitimer", "pthread_kill", "pthread_sigmask", "siginterrupt",
        "sigwait", "sigtimedwait", "sigpending", "ITIMER_REAL",
    }
)
# POSIX-only members of other modules: module -> {attr: rule}.
POSIX_MEMBERS: dict[str, dict[str, str]] = {
    "os": dict.fromkeys(OS_POSIX_ATTRS, "W002"),
    "signal": {**dict.fromkeys(SIGNAL_POSIX, "W003"), **{"Signals.SIGKILL": "W003"}},
    "socket": {"AF_UNIX": "W006"},
    "asyncio": {"start_unix_server": "W006", "open_unix_connection": "W006"},
    "select": {"poll": "W014", "epoll": "W014", "kqueue": "W014", "kevent": "W014", "devpoll": "W014"},
}
# Event-loop methods the Windows (Proactor) loop does not implement.
LOOP_POSIX = {"add_signal_handler": "W003", "remove_signal_handler": "W003",
              "add_reader": "W014", "add_writer": "W014",
              "create_unix_connection": "W006", "create_unix_server": "W006"}
WINDOWS_MEMBERS: dict[str, frozenset[str]] = {
    "subprocess": frozenset({"DETACHED_PROCESS", "STARTUPINFO", "SW_HIDE", "STD_INPUT_HANDLE",
                             "STD_OUTPUT_HANDLE", "STD_ERROR_HANDLE"}),
    "ctypes": frozenset({"windll", "WinDLL", "oledll", "OleDLL", "WINFUNCTYPE", "WinError",
                         "FormatError", "GetLastError", "wintypes"}),
    "os": frozenset({"startfile", "O_BINARY", "O_NOINHERIT", "O_TEMPORARY", "O_SHORT_LIVED",
                     "O_RANDOM", "O_SEQUENTIAL", "O_TEXT", "get_handle_inheritable",
                     "set_handle_inheritable", "add_dll_directory"}),
    "sys": frozenset({"getwindowsversion", "winver", "dllhandle"}),
    "signal": frozenset({"CTRL_C_EVENT", "CTRL_BREAK_EVENT"}),
    "asyncio": frozenset({"WindowsProactorEventLoopPolicy", "WindowsSelectorEventLoopPolicy",
                          "ProactorEventLoop", "IocpProactor"}),
}
WINDOWS_MEMBER_PREFIXES = {"subprocess": ("CREATE_", "STARTF_", "ABOVE_NORMAL_PRIORITY", "BELOW_NORMAL_PRIORITY",
                                          "HIGH_PRIORITY", "IDLE_PRIORITY", "NORMAL_PRIORITY",
                                          "REALTIME_PRIORITY")}
RAW_LOCAL_IPC_MEMBERS = frozenset(
    {("socket", "AF_UNIX"), ("asyncio", "start_unix_server"), ("asyncio", "open_unix_connection"),
     ("socketserver", "UnixStreamServer"), ("socketserver", "UnixDatagramServer")}
)
RAW_LOCAL_IPC_LOOP = frozenset({"create_unix_server", "create_unix_connection"})
RAW_PIPE_FUNCS = frozenset({"connect_named_pipe", "listen_named_pipe"})
RAW_PIPE_MODULES = frozenset({"windows_pipe", "win32pipe"})
NO_BASELINE_RULES = frozenset({"W020"})
TRACKED_MODULES = frozenset(
    {"os", "signal", "socket", "socketserver", "subprocess", "sys", "platform", "select", "asyncio",
     "ctypes", "multiprocessing", "importlib", "functools"}
)
SPAWN_FUNCS = frozenset(
    {"Popen", "run", "call", "check_call", "check_output",
     "create_subprocess_exec", "create_subprocess_shell"}
)
FD_KEYWORDS = frozenset({"pass_fds", "preexec_fn"})
SESSION_KEYWORDS = frozenset({"start_new_session", "process_group"})
SPAWN_KNOBS = FD_KEYWORDS | SESSION_KEYWORDS
IDENTITY_KEYWORDS = frozenset({"user", "group", "extra_groups", "umask"})
# The capability a subprocess knob needs (a hasattr on it is a genuine guard).
KNOB_CAPABILITY = {"start_new_session": ("os", "setsid"), "process_group": ("os", "setpgid"),
                   "preexec_fn": ("os", "fork"), "user": ("os", "setuid"), "group": ("os", "setgid"),
                   "extra_groups": ("os", "setgroups"), "umask": ("os", "umask")}
POSIX_PATH_PREFIXES = (
    "/tmp", "/dev/", "/proc", "/etc/", "/var/", "/usr/", "/bin/", "/sbin/", "/run/",
    "/private/", "/Library/",
)
PRIVATE_MODES = frozenset({0o600, 0o700, 0o400, 0o500})
WINDOWS_VALUES = frozenset({"nt", "win32", "cygwin", "msys", "windows", "win"})
EXITS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
POSIX = ("platform", "posix")
WINDOWS = ("platform", "windows")
Facts = frozenset  # of fact tuples: POSIX / WINDOWS / ("cap", module, attr)
NO_FACTS: frozenset[tuple[str, ...]] = frozenset()


class GateFormatError(ValueError):
    """The baseline cannot be read or validated: fail closed, never skip."""


def _windows_member(module: str, attr: str) -> bool:
    return attr in WINDOWS_MEMBERS.get(module, ()) or attr.startswith(
        WINDOWS_MEMBER_PREFIXES.get(module, ("\0",))
    )


def _windows_module(name: str) -> bool:
    root = name.split(".")[0]
    return root in WINDOWS_MODULES or root.startswith("win32") or name == "ctypes.wintypes"


def _is_constant(node: ast.AST, *values: object) -> bool:
    return isinstance(node, ast.Constant) and any(node.value is value for value in values)


def in_seam(relative: str) -> bool:
    return any(
        relative.startswith(seam) if seam.endswith("/") else relative == seam
        for seam in PLATFORM_SEAMS
    )


class _Scanner(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.seam = in_seam(relative)
        self.example_module = semantics.example_module(relative)
        self.toggles: dict[int, frozenset[int]] = {}
        self.chain: list[ast.AST] = []
        self.findings: list[dict[str, Any]] = []
        # local name -> canonical dotted name ("os", "os.kill", "subprocess.Popen").
        self.names: dict[str, str] = {module: module for module in TRACKED_MODULES}
        # local variable -> a platform value expression / a platform predicate.
        self.platform_values: set[str] = set()
        self.predicates: dict[str, tuple[frozenset, frozenset]] = {}

    # -- bookkeeping -------------------------------------------------------
    def visit(self, node: ast.AST) -> None:
        self.chain.append(node)
        try:
            for target, symbol in semantics.rejections(self, node, POSIX_MEMBERS, POSIX_MODULES):
                self.add("W021", target, symbol)
            super().visit(node)
        finally:
            self.chain.pop()

    def _scope(self) -> str:
        names = [
            node.name for node in self.chain
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        return ".".join(names) or "<module>"

    def add(self, rule: str, node: ast.AST, symbol: str, *, kind: str = "other",
            capability: tuple[str, str] | None = None) -> None:
        if self.seam:
            return
        if rule not in UNGUARDED_RULES and self._guarded(kind, capability):
            return
        self.findings.append(
            {"path": self.relative, "rule": rule, "scope": self._scope(), "symbol": symbol,
             "line": getattr(node, "lineno", 0)}
        )

    # -- name resolution -----------------------------------------------------
    def canon(self, node: ast.AST) -> str:
        """Canonical dotted name: aliases resolved, ``getattr(m, 'a')`` (no default) as ``m.a``."""

        if isinstance(node, ast.Name):
            return self.names.get(node.id, "")
        if isinstance(node, ast.Attribute):
            base = self.canon(node.value)
            return f"{base}.{node.attr}" if base else ""
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) == 2 and not node.keywords and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            base = self.canon(node.args[0])
            return f"{base}.{node.args[1].value}" if base else ""
        return ""

    def _platform_value(self, node: ast.AST) -> bool:
        """``os.name`` / ``sys.platform`` / ``platform.system()`` (+ ``.lower()``, local copies)."""

        if isinstance(node, ast.Name) and node.id in self.platform_values:
            return True
        if self.canon(node) in {"os.name", "sys.platform"} or semantics.platform_getattr(self, node):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"lower", "casefold", "upper", "strip"}:
                return self._platform_value(func.value)
            return not node.args and (
                self.canon(func) in {"platform.system", "platform.platform"}
                or semantics.platform_func_getattr(self, func)
            )
        return False

    # -- guard facts -----------------------------------------------------------
    def facts(self, test: ast.AST) -> tuple[frozenset, frozenset]:
        """(facts when ``test`` is truthy, facts when it is falsy)."""

        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            true, false = self.facts(test.operand)
            return false, true
        if isinstance(test, ast.BoolOp):
            parts = [self.facts(value) for value in test.values]
            if isinstance(test.op, ast.And):
                return frozenset().union(*(p[0] for p in parts)), frozenset.intersection(*(p[1] for p in parts))
            return frozenset.intersection(*(p[0] for p in parts)), frozenset().union(*(p[1] for p in parts))
        if isinstance(test, ast.Name) and test.id in self.predicates:
            return self.predicates[test.id]
        if isinstance(test, ast.Call):
            func = test.func
            if isinstance(func, ast.Name) and func.id in {"hasattr", "getattr"} and len(test.args) >= 2:
                module = self.canon(test.args[0])
                attr = test.args[1]
                if module in TRACKED_MODULES and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                    return frozenset({("cap", module, attr.value)}), NO_FACTS
            if (
                isinstance(func, ast.Name) and func.id == "all" and len(test.args) == 1
                and isinstance(test.args[0], ast.GeneratorExp) and len(test.args[0].generators) == 1
            ):
                return self._all_capabilities(test.args[0]), NO_FACTS
            if isinstance(func, ast.Attribute) and func.attr in {"startswith", "endswith"} and self._platform_value(func.value):
                return self._polarity([arg for arg in test.args], positive=True)
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            left, op, right = test.left, test.ops[0], test.comparators[0]
            if isinstance(op, (ast.Is, ast.IsNot)) and _is_constant(right, None):
                inner, _ = self.facts(left)
                caps = frozenset(fact for fact in inner if fact[0] == "cap")
                return (caps, NO_FACTS) if isinstance(op, ast.IsNot) else (NO_FACTS, caps)
            if isinstance(op, (ast.In, ast.NotIn)) and self.canon(right).startswith("os.supports_"):
                caps = frozenset({("cap", "os", self.canon(right).rsplit(".", 1)[-1])})
                return (caps, NO_FACTS) if isinstance(op, ast.In) else (NO_FACTS, caps)
            positive = isinstance(op, (ast.Eq, ast.In, ast.Is))
            if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)):
                if self._platform_value(left):
                    return self._polarity([right], positive)
                if self._platform_value(right):
                    return self._polarity([left], positive)
        return NO_FACTS, NO_FACTS

    def _all_capabilities(self, generator: ast.GeneratorExp) -> frozenset:
        """``all(hasattr(os, flag) for flag in ("O_A", "O_B"))`` -> both capabilities."""

        loop = generator.generators[0]
        element = generator.elt
        if not (
            isinstance(loop.target, ast.Name) and isinstance(loop.iter, (ast.Tuple, ast.List, ast.Set))
            and not loop.ifs and isinstance(element, ast.Call) and isinstance(element.func, ast.Name)
            and element.func.id in {"hasattr", "getattr"} and len(element.args) >= 2
            and isinstance(element.args[1], ast.Name) and element.args[1].id == loop.target.id
        ):
            return NO_FACTS
        module = self.canon(element.args[0])
        if module not in TRACKED_MODULES:
            return NO_FACTS
        return frozenset(
            ("cap", module, item.value) for item in loop.iter.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )

    @staticmethod
    def _polarity(nodes: list[ast.AST], positive: bool) -> tuple[frozenset, frozenset]:
        values: list[str] = []
        for node in nodes:
            elements = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else [node]
            for element in elements:
                if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                    return NO_FACTS, NO_FACTS
                values.append(element.value.lower())
        if not values:
            return NO_FACTS, NO_FACTS
        windows = [value in WINDOWS_VALUES for value in values]
        if all(windows):
            matched = frozenset({WINDOWS})
            other = frozenset({POSIX})
        elif not any(windows):
            matched, other = frozenset({POSIX}), frozenset({WINDOWS})
        else:
            return NO_FACTS, NO_FACTS
        # "x == 'nt'" truthy -> Windows; falsy -> not Windows.  For a POSIX value set,
        # falsy only says "not this POSIX value", so it proves nothing.
        if matched == {WINDOWS}:
            return (matched, other) if positive else (other, matched)
        return (matched, NO_FACTS) if positive else (NO_FACTS, matched)

    def _covers(self, facts: frozenset, kind: str, capability: tuple[str, str] | None) -> bool:
        if POSIX in facts:
            return True
        if capability is None:
            return False
        module, attr = capability
        return ("cap", module, attr) in facts or (
            module == "os" and any(fact[0] == "cap" and fact[2].startswith("supports_") for fact in facts)
            and attr.startswith("supports_")
        )

    def _block_facts(self, block: list[ast.stmt], child: ast.AST) -> frozenset:
        found: set[tuple[str, ...]] = set()
        for statement in block:
            if statement is child:
                break
            if isinstance(statement, ast.If):
                true, false = self.facts(statement.test)
                if statement.body and isinstance(statement.body[-1], EXITS):
                    found |= false
                elif statement.orelse and isinstance(statement.orelse[-1], EXITS):
                    found |= true
            elif isinstance(statement, ast.Assert):
                found |= self.facts(statement.test)[0]
        return frozenset(found)

    def _guarded(self, kind: str, capability: tuple[str, str] | None) -> bool:
        chain = self.chain
        for index in range(len(chain) - 2, -1, -1):
            ancestor, child = chain[index], chain[index + 1]
            facts: frozenset = NO_FACTS
            if isinstance(ancestor, (ast.If, ast.IfExp, ast.While)) and child is not ancestor.test:
                true, false = self.facts(ancestor.test)
                in_body = child is ancestor.body or (isinstance(ancestor.body, list) and child in ancestor.body)
                facts = true if in_body else false
            elif isinstance(ancestor, ast.BoolOp):
                position = next(i for i, value in enumerate(ancestor.values) if value is child)
                parts = [self.facts(value) for value in ancestor.values[:position]]
                pick = 0 if isinstance(ancestor.op, ast.And) else 1
                facts = frozenset().union(*(part[pick] for part in parts))
            elif isinstance(ancestor, (ast.Try, getattr(ast, "TryStar", ast.Try))) and any(
                child is item for item in ancestor.body
            ):
                wanted = {"import": {"ImportError", "ModuleNotFoundError"},
                          "member": {"AttributeError"}, "loop": {"NotImplementedError"}}.get(kind, set())
                for handler in ancestor.handlers:
                    if self._handler_names(handler) & wanted and not self._reraises(handler):
                        return True
            if self._covers(facts, kind, capability):
                return True
            for field in ("body", "orelse", "finalbody"):
                block = getattr(ancestor, field, None)
                if isinstance(block, list) and any(child is item for item in block):
                    if self._covers(self._block_facts(block, child), kind, capability):
                        return True
        return False

    @staticmethod
    def _handler_names(handler: ast.ExceptHandler) -> set[str]:
        if handler.type is None:
            return {"BaseException"}
        elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        return {ast.unparse(element).rsplit(".", 1)[-1] for element in elements}

    @staticmethod
    def _reraises(handler: ast.ExceptHandler) -> bool:
        return len(handler.body) == 1 and isinstance(handler.body[0], ast.Raise) and handler.body[0].exc is None

    # -- members ---------------------------------------------------------------
    def _member(self, module: str, attr: str, node: ast.AST, *, default: bool = False,
                zero_flag: bool = False) -> None:
        if module not in TRACKED_MODULES:
            return
        if (module, attr) in RAW_LOCAL_IPC_MEMBERS:
            self.add("W020", node, f"{module}.{attr}")
        if _windows_member(module, attr):
            if not (zero_flag and module == "os" and attr.startswith("O_")):
                self.add("W019", node, f"{module}.{attr}")
            return
        rule = POSIX_MEMBERS.get(module, {}).get(attr)
        if rule and not default:
            self.add(rule, node, f"{module}.{attr}", kind="member", capability=(module, attr))

    def _module_import(self, name: str, node: ast.AST) -> None:
        parts = name.split(".")
        if parts[0] in POSIX_MODULES:
            self.add("W001", node, f"import {name}", kind="import")
        if _windows_module(name):
            self.add("W019", node, f"import {name}")
        if parts[0] in RAW_PIPE_MODULES or "windows_pipe" in parts[1:]:
            self.add("W020", node, f"import {name}")

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._module_import(alias.name, node)
            root = alias.name.split(".")[0]
            if root in TRACKED_MODULES:
                self.names[alias.asname or root] = alias.name if alias.asname else root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            return
        self._module_import(module, node)
        for alias in node.names:
            if alias.name in RAW_PIPE_FUNCS or alias.name in RAW_PIPE_MODULES:
                self.add("W020", node, f"import {alias.name}")
                if alias.name in RAW_PIPE_FUNCS:
                    self.names[alias.asname or alias.name] = alias.name
            if module in TRACKED_MODULES:
                self._member(module, alias.name, node)
                self.names[alias.asname or alias.name] = f"{module}.{alias.name}"
        self.generic_visit(node)

    # -- attribute access ----------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        module = self.canon(node.value)
        if module in TRACKED_MODULES:
            self._member(module, node.attr, node)
        elif module == "signal.Signals":
            self._member("signal", f"Signals.{node.attr}" if node.attr == "SIGKILL" else node.attr, node)
        elif node.attr in LOOP_POSIX and not module:
            if node.attr in RAW_LOCAL_IPC_LOOP:
                self.add("W020", node, f"loop.{node.attr}")
            self.add(LOOP_POSIX[node.attr], node, f"loop.{node.attr}", kind="loop",
                     capability=("loop", node.attr))
        self.generic_visit(node)

    # -- assignments (platform variables, spawn-knob dicts) -------------------
    def _shadow(self, name: str) -> None:
        """A rebound from-import alias (``from os import name`` then ``name = ...``) is no longer it."""

        if name not in TRACKED_MODULES:
            self.names.pop(name, None)

    def visit_arg(self, node: ast.arg) -> None:
        self._shadow(node.arg)
        self.platform_values.discard(node.arg)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._shadow(target.id)
                if self._platform_value(node.value):
                    self.platform_values.add(target.id)
                else:
                    self.platform_values.discard(target.id)
                    facts = self.facts(node.value)
                    if facts != (NO_FACTS, NO_FACTS):
                        self.predicates[target.id] = facts
            if (
                isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                and target.slice.value in SPAWN_KNOBS
            ):
                self._knob(target.slice.value, node.value, node, "kwargs")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in SPAWN_KNOBS:
                self._knob(key.value, value, node, "kwargs")
        self.generic_visit(node)

    # -- calls -------------------------------------------------------------
    def _spawn_name(self, func: ast.AST) -> tuple[str, bool] | None:
        """(name, strict): strict = certainly a process spawn (identity knobs apply)."""

        canonical = self.canon(func)
        if canonical.startswith("subprocess.") and canonical[11:] in SPAWN_FUNCS:
            return canonical[11:], True
        if canonical in {"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"}:
            return canonical[8:], True
        if isinstance(func, ast.Attribute) and func.attr in SPAWN_FUNCS:
            return func.attr, func.attr in {"Popen", "create_subprocess_exec", "create_subprocess_shell"}
        return None

    def _disabled(self, knob: str, value: ast.AST) -> bool:
        if knob == "process_group":
            # process_group=0 is "new group": only None disables it.
            return _is_constant(value, None)
        if _is_constant(value, None, False):
            return True
        if knob in FD_KEYWORDS and isinstance(value, (ast.Tuple, ast.List)) and not value.elts:
            return True
        # value-level guard: true only off Windows, or only when the capability exists
        true, _ = self.facts(value)
        return POSIX in true or any(fact[0] == "cap" and fact[1] == "os" for fact in true)

    def _knob(self, knob: str, value: ast.AST, node: ast.AST, func_name: str) -> None:
        if self._disabled(knob, value):
            return
        rule = "W004" if knob in FD_KEYWORDS else "W005"
        self.add(rule, node, f"{func_name}({knob})", capability=KNOB_CAPABILITY.get(knob))

    def _spawn_keywords(self, call: ast.Call, func_name: str, spawn: bool) -> None:
        for keyword in call.keywords:
            if keyword.arg in SPAWN_KNOBS:
                self._knob(keyword.arg, keyword.value, call, func_name)
            elif keyword.arg in IDENTITY_KEYWORDS and spawn:
                value = keyword.value
                disabled = _is_constant(value, None) or (
                    keyword.arg == "umask" and ast.unparse(value) == "-1"
                )
                if not disabled:
                    self.add("W005", call, f"{func_name}({keyword.arg})",
                             capability=KNOB_CAPABILITY.get(keyword.arg))

    def _w007(self, node: ast.Call) -> None:
        flags = node.args[1]
        function = next(
            (item for item in reversed(self.chain) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))),
            self.chain[0],
        )
        if isinstance(flags, ast.Name):  # resolve a simple local constant
            for item in ast.walk(function):
                if isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == flags.id for t in item.targets
                ):
                    flags = item.value
                    break
        names = {
            item.attr if isinstance(item, ast.Attribute) else item.id
            for item in ast.walk(flags) if isinstance(item, (ast.Attribute, ast.Name))
        } | {item.value for item in ast.walk(flags) if isinstance(item, ast.Constant)}
        if not names & {"O_RDONLY", "O_DIRECTORY"}:
            return
        parent = self.chain[-2] if len(self.chain) > 1 else None
        if isinstance(parent, ast.Call) and self.canon(parent.func) == "os.fsync":
            self.add("W007", node, "os.open(dir)+os.fsync", capability=("os", "O_DIRECTORY"))
            return
        if not (isinstance(parent, ast.Assign) and isinstance(parent.targets[0], ast.Name)):
            return
        fd = parent.targets[0].id
        if any(
            isinstance(item, ast.Call) and self.canon(item.func) == "os.fsync" and item.args
            and isinstance(item.args[0], ast.Name) and item.args[0].id == fd
            for item in ast.walk(function)
        ):
            self.add("W007", node, "os.open(dir)+os.fsync", capability=("os", "O_DIRECTORY"))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        canonical = self.canon(func)
        last = canonical.rsplit(".", 1)[-1] if canonical else (
            func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        )
        # getattr(module, "attr"[, default]) is an attribute access.
        if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            module, attr = self.canon(node.args[0]), node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                self._member(module, attr.value, node, default=len(node.args) > 2,
                             zero_flag=len(node.args) == 3 and _is_constant(node.args[2], 0))
        if (
            isinstance(func, ast.Name) and func.id == "hasattr" and len(node.args) == 2
            and self.canon(node.args[0]) == "socket"
            and isinstance(node.args[1], ast.Constant) and node.args[1].value == "AF_UNIX"
        ):
            self.add("W020", node, "hasattr(socket, 'AF_UNIX')")
        if last in RAW_PIPE_FUNCS:
            self.add("W020", node, f"{last}()")
        # importlib.import_module("fcntl") / __import__("pwd")
        if (canonical == "importlib.import_module" or (isinstance(func, ast.Name) and func.id == "__import__")) and (
            node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        ):
            self._module_import(node.args[0].value, node)
        spawn = self._spawn_name(func)
        partial = canonical == "functools.partial" and node.args and self._spawn_name(node.args[0])
        if spawn is not None:
            self._spawn_keywords(node, *spawn)
        elif partial:
            self._spawn_keywords(node, f"partial({partial[0]})", partial[1])
        elif (isinstance(func, ast.Name) and func.id == "dict") or last in {"update", "setdefault"}:
            self._spawn_keywords(node, "kwargs", False)
        if canonical in {"multiprocessing.get_context", "multiprocessing.set_start_method"} and any(
            isinstance(arg, ast.Constant) and arg.value in {"fork", "forkserver"} for arg in node.args
        ):
            self.add("W002", node, f"{canonical}(fork)", capability=("os", "fork"))
        for keyword in node.keywords:
            if keyword.arg == "dir_fd" and not _is_constant(keyword.value, None):
                self.add("W008", node, f"{last}(dir_fd)", capability=("os", "supports_dir_fd"))
        if canonical == "os.kill" and len(node.args) == 2:
            signal_arg = node.args[1]
            if isinstance(signal_arg, ast.Constant) and type(signal_arg.value) is int and signal_arg.value == 0:
                self.add("W009", node, "os.kill(pid, 0)")
            else:
                self.add("W010", node, f"os.kill(pid, {ast.unparse(signal_arg)[:40]})")
        if last in {"chmod", "fchmod", "lchmod"}:
            os_call = canonical.startswith("os.")
            modes = [*node.args[1:]] if os_call or isinstance(func, ast.Name) else [*node.args[:1]]
            modes += [keyword.value for keyword in node.keywords if keyword.arg == "mode"]
            private = sorted(
                {
                    item.value for mode in modes for item in ast.walk(mode)
                    if isinstance(item, ast.Constant) and type(item.value) is int
                    and item.value in PRIVATE_MODES
                }
            )
            if private and not semantics.write_restore(self.chain, self.toggles):
                self.add("W012", node, f"{last}({oct(private[0])})",
                         capability=("os", last) if os_call else None)
        if canonical == "os.symlink" or (
            isinstance(func, ast.Attribute) and func.attr in {"symlink", "symlink_to"}
        ):
            self.add("W013", node, f"{last}()")
        if canonical == "os.pipe":
            self.add("W014", node, "os.pipe()")
        if canonical == "os.open" and len(node.args) >= 2:
            self._w007(node)
        if (
            isinstance(func, ast.Attribute) and func.attr in {"startswith", "endswith"}
            and self._platform_value(func.value)
        ) or (last == "get" and node.args and self._platform_value(node.args[0])):
            self.add("W015", node, ast.unparse(node)[:60])
        self.generic_visit(node)

    # -- literals, comparisons, platform dispatch -----------------------------
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value.startswith(POSIX_PATH_PREFIXES):
            parent = self.chain[-2] if len(self.chain) > 1 else None
            docstring = isinstance(parent, ast.Expr)
            inner_fragment = isinstance(parent, ast.JoinedStr) and parent.values[0] is not node
            if not docstring and not inner_fragment and not semantics.example_data(
                self.chain, self.example_module
            ):
                self.add("W011", node, repr(node.value[:60]))

    def visit_Compare(self, node: ast.Compare) -> None:
        if any(self._platform_value(item) for item in [node.left, *node.comparators]):
            self.add("W015", node, ast.unparse(node)[:60])
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        if self._platform_value(node.subject):
            self.add("W015", node, f"match {ast.unparse(node.subject)[:50]}")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._platform_value(node.slice):
            self.add("W015", node, ast.unparse(node)[:60])
        self.generic_visit(node)


def scan_source(text: str, relative: str) -> list[dict[str, Any]]:
    scanner = _Scanner(relative)
    tree = ast.parse(text, filename=relative)
    scanner.toggles = semantics.toggle_containers(tree)
    scanner.visit(tree)
    return scanner.findings


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root
        if base.is_dir():
            files.extend(
                path for path in base.rglob("*.py")
                if not EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
            )
    return sorted(files)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise GateFormatError(f"duplicate baseline key(s): {duplicates}")
    return dict(pairs)


def parse_baseline(text: str, where: str = BASELINE_RELATIVE_PATH) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(path, rule): {count, reason, removal}}``; any defect fails closed."""

    try:
        document = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as error:
        raise GateFormatError(f"{where}: unreadable baseline: {error}") from error
    if not isinstance(document, dict) or document.get("version") != BASELINE_VERSION:
        raise GateFormatError(f"{where}: version must be {BASELINE_VERSION}")
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise GateFormatError(f"{where}: entries must be a {{path: {{rule: entry}}}} map")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path, by_rule in entries.items():
        if not isinstance(by_rule, dict) or not by_rule:
            raise GateFormatError(f"{where}: {path}: must map rule ids to entries")
        for rule, entry in by_rule.items():
            key = f"{where}: {path} {rule}"
            if rule not in RULES:
                raise GateFormatError(f"{key}: unknown rule")
            if rule in NO_BASELINE_RULES:
                raise GateFormatError(f"{key}: this rule admits no baseline entry")
            if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
                raise GateFormatError(f"{key}: fields must be exactly {sorted(ENTRY_FIELDS)}")
            for field in ("reason", "removal"):
                if not isinstance(entry[field], str) or not entry[field].strip():
                    raise GateFormatError(f"{key}: {field} must be a non-empty string")
            if not entry["reason"].startswith(REASON_PREFIXES):
                raise GateFormatError(f"{key}: reason must start with one of {REASON_PREFIXES}")
            if type(entry["count"]) is not int or entry["count"] < 1:
                raise GateFormatError(f"{key}: count must be a positive integer")
            if in_seam(path):
                raise GateFormatError(f"{key}: platform seams are exempt and need no entry")
            result[(path, rule)] = entry
    semantics.validate_seeded_rules(document, result, RULES, NO_BASELINE_RULES, where, GateFormatError)
    return result


def load_baseline(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise GateFormatError(f"{path}: unreadable baseline: {error}") from error
    return parse_baseline(text, str(path))


def compare_with_base(
    current: dict[tuple[str, str], dict[str, Any]],
    base: dict[tuple[str, str], dict[str, Any]],
    base_seeded_rules: frozenset[str] | None = None,
) -> list[str]:
    """Keys may only leave, except a one-time first seed of an unseeded rule id."""

    return semantics.compare_baselines(current, base, base_seeded_rules)


def _read_base(root: Path, ref: str) -> tuple[dict[tuple[str, str], dict[str, Any]], frozenset[str]] | None:
    return semantics.read_base_snapshot(
        root, ref, BASELINE_RELATIVE_PATH, BASELINE_VERSION, parse_baseline, GateFormatError
    )


def base_baseline(root: Path, ref: str) -> dict[tuple[str, str], dict[str, Any]] | None:
    """The baseline at ``ref``; ``None`` when ``ref`` has no v2 baseline yet (first seed)."""

    pair = _read_base(root, ref)
    return None if pair is None else pair[0]


def scan(root: Path, baseline: Path | bool | None = None, base_ref: str | None = None) -> dict[str, Any]:
    """Scan one repository tree and apply the only-out baseline."""

    root = root.resolve()
    files = _python_files(root)
    findings: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            findings.extend(scan_source(path.read_text(encoding="utf-8"), relative))
        except (OSError, UnicodeError, SyntaxError) as error:
            parse_errors.append(f"{relative}: {error}")
    advisory = [row for row in findings if row["rule"] in ADVISORY_RULES]
    findings = [row for row in findings if row["rule"] not in ADVISORY_RULES]
    actual = Counter((row["path"], row["rule"]) for row in findings)

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    baseline_errors: list[str] = []
    if baseline is not False:
        baseline_path = baseline if isinstance(baseline, Path) else root / BASELINE_RELATIVE_PATH
        try:
            entries = load_baseline(baseline_path)
            if base_ref:
                pair = _read_base(root, base_ref)
                if pair is not None:
                    baseline_errors.extend(compare_with_base(entries, pair[0], pair[1]))
        except GateFormatError as error:
            baseline_errors.append(str(error))
    allowed = {key: entry["count"] for key, entry in entries.items() if key[1] in AST_RULES}
    detail: dict[tuple[str, str], list[str]] = {}
    for row in findings:
        detail.setdefault((row["path"], row["rule"]), []).append(
            f"{row['line']} [{row['scope']}] {row['symbol']}"
        )
    new_findings = [
        {"path": k[0], "rule": k[1], "count": count, "baseline": allowed.get(k, 0), "findings": detail[k]}
        for k, count in sorted(actual.items())
        if count > allowed.get(k, 0)
    ]
    stale = [
        {"path": k[0], "rule": k[1], "count": count, "actual": actual.get(k, 0)}
        for k, count in sorted(allowed.items())
        if count > actual.get(k, 0)
    ]
    failing = bool(new_findings or stale or parse_errors or baseline_errors)
    return {
        "status": "FAIL" if failing else "PASS",
        "scanRoots": list(SCAN_ROOTS),
        "filesChecked": len(files),
        "findings": findings,
        "advisoryFindings": advisory,
        "newFindings": new_findings,
        "staleBaseline": stale,
        "parseErrors": parse_errors,
        "baselineErrors": baseline_errors,
        "summary": {
            "findings": len(findings),
            "byRule": dict(sorted(Counter(row["rule"] for row in findings).items())),
            "new": len(new_findings),
            "stale": len(stale),
            "baselineKeys": len(entries),
            "advisory": len(advisory),
        },
    }


def prune_baseline(root: Path) -> int:
    """Lower counts and drop keys whose code is gone; never add or raise one."""

    path = root / BASELINE_RELATIVE_PATH
    document = json.loads(path.read_text(encoding="utf-8"))
    actual = Counter((row["path"], row["rule"]) for row in scan(root, baseline=False)["findings"])
    removed = 0
    for file_path in list(document["entries"]):
        by_rule = document["entries"][file_path]
        for rule in list(by_rule):
            if rule not in AST_RULES:
                continue
            count = min(by_rule[rule]["count"], actual.get((file_path, rule), 0))
            removed += by_rule[rule]["count"] - count
            if count:
                by_rule[rule]["count"] = count
            else:
                del by_rule[rule]
        if not by_rule:
            del document["entries"][file_path]
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--base-ref", help="git ref of the PR base: no new key, no raised count")
    parser.add_argument(
        "--prune-baseline", action="store_true",
        help="lower/remove baseline keys the code no longer needs (never adds)",
    )
    args = parser.parse_args(argv)
    if args.prune_baseline:
        removed = prune_baseline(args.root)
        print(f"windows-portability: pruned {removed} finding(s) from the baseline")
    report = scan(args.root, base_ref=args.base_ref)
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "findings"}, indent=2))
    else:
        print(
            f"windows-portability gate: {report['status']} files={report['filesChecked']} "
            f"findings={report['summary']['findings']} new={report['summary']['new']} "
            f"stale={report['summary']['stale']} advisory={report['summary']['advisory']}"
        )
        for row in report["advisoryFindings"]:
            print(f"  ADVISORY {row['rule']} {row['path']}:{row['line']} [{row['scope']}] {row['symbol']}")
        for row in report["newFindings"]:
            name, remedy = RULES[row["rule"]]
            print(f"  NEW {row['rule']} {name} {row['path']} ({row['count']} > {row['baseline']}); use: {remedy}")
            for item in row["findings"]:
                print(f"      line {item}")
        for row in report["staleBaseline"]:
            print(
                f"  STALE {row['rule']} {row['path']} baseline {row['count']} > actual "
                f"{row['actual']}: run --prune-baseline"
            )
        for error in report["parseErrors"] + report["baselineErrors"]:
            print(f"  ERROR {error}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
