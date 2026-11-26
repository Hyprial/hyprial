#!/usr/bin/env python3
# Decision record (KP-136 stdin-stop probe, 2026-11-24):
# - Pure standard library (ctypes) so a Windows 11 acceptance machine can run it
#   with any CPython 3.10+ and no checkout dependencies.
# - facts and watch are read-only: they query handles and process state and
#   never write, signal or kill anything. run starts only the command it is
#   given and closes only the stdin pipe or pseudo console it created itself.
# - Output is one JSON object on stdout so results can be pasted into the KP.
"""Collect the Windows facts KP-136 needs about ``hyprial gui --stdin-stop``.

Subcommands::

    facts                      what this process sees on fd 0/1/2
    watch --pid PID            follow PID and its descendants until they exit
    run --via pipe|conpty -- CMD...
                               start CMD behind a pipe or a pseudo console,
                               close it, then follow the process tree

Run ``facts`` inside each terminal host exactly as the GUI would be started
there; run ``watch`` from a second, independent PowerShell window.

Output fields of ``watch`` and ``run`` (also echoed as ``field_notes``)::

    root_opened        the probe holds a handle to the root (watch: OpenProcess
                       succeeded; run --via conpty: CreateProcessW's hProcess)
    root_open_error    GetLastError of the failed OpenProcess, else null; a
                       missing or inaccessible PID is not an exit
    root_exited        an exit code of the root was actually observed
    finished           the root exited, or could not be opened, and the grace
                       period ran out before the timeout
    new_watched_processes_still_alive
                       machine-wide: every process named in WATCH_NAMES that
                       is in the after-snapshot but not the before-snapshot,
                       related to the root or not; descendants are linked by
                       parent PID, so a reused PID can pull in an unrelated one
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Any

FILE_TYPES = {
    0x0000: "UNKNOWN",
    0x0001: "DISK",
    0x0002: "CHAR",
    0x0003: "PIPE",
    0x8000: "REMOTE",
}
STILL_ACTIVE = 259
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
FILE_NAME_INFO_CLASS = 2
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
WATCH_NAMES = ("python.exe", "pythonw.exe", "hyprial.exe", "node.exe", "uv.exe")
FIELD_NOTES = {
    "root_exited": "true only when the root's exit code was observed",
    "root_open_error": "OpenProcess failed for the root PID; not an exit",
    "new_watched_processes_still_alive": (
        "machine-wide by image name (WATCH_NAMES), not limited to the root's tree"
    ),
}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32() -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.GetNamedPipeInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.GetConsoleProcessList.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    kernel.GetConsoleWindow.restype = wintypes.HWND
    return kernel


def _last_error() -> int:
    return int(ctypes.get_last_error())  # type: ignore[attr-defined]


def _handle_name(kernel: Any, handle: int) -> str | None:
    buffer = ctypes.create_string_buffer(4 + 2 * 1024)
    if not kernel.GetFileInformationByHandleEx(
        handle, FILE_NAME_INFO_CLASS, buffer, len(buffer)
    ):
        return None
    length = int.from_bytes(buffer.raw[:4], "little")
    return buffer.raw[4 : 4 + length].decode("utf-16-le", "replace")


def _descriptor_facts(kernel: Any, fd: int) -> dict[str, Any]:
    import msvcrt

    facts: dict[str, Any] = {"fd": fd}
    try:
        handle = int(msvcrt.get_osfhandle(fd))  # type: ignore[attr-defined]
    except OSError as error:
        facts["osfhandle_error"] = str(error)
        return facts
    facts["handle"] = hex(handle)
    ctypes.set_last_error(0)  # type: ignore[attr-defined]
    file_type = int(kernel.GetFileType(handle))
    facts["file_type"] = FILE_TYPES.get(file_type, hex(file_type))
    facts["file_type_error"] = _last_error() if file_type == 0 else 0
    mode = wintypes.DWORD(0)
    is_console = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)))
    facts["is_console"] = is_console
    facts["console_mode"] = hex(mode.value) if is_console else None
    facts["console_mode_error"] = 0 if is_console else _last_error()
    if file_type == 0x0003:
        avail = wintypes.DWORD(0)
        ok = bool(
            kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
        )
        facts["peek_ok"] = ok
        facts["peek_avail"] = int(avail.value) if ok else None
        facts["peek_error"] = 0 if ok else _last_error()
        flags = wintypes.DWORD(0)
        if kernel.GetNamedPipeInfo(handle, ctypes.byref(flags), None, None, None):
            facts["pipe_end"] = "server" if flags.value & 1 else "client"
            facts["pipe_message_mode"] = bool(flags.value & 4)
        else:
            facts["pipe_info_error"] = _last_error()
        facts["object_name"] = _handle_name(kernel, handle)
    return facts


def _snapshot(kernel: Any) -> dict[int, tuple[int, str]]:
    table: dict[int, tuple[int, str]] = {}
    snap = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        return table
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                entry.szExeFile,
            )
            more = kernel.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snap)
    return table


def _ancestry(kernel: Any, pid: int) -> list[dict[str, Any]]:
    table = _snapshot(kernel)
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    while pid in table and pid not in seen and len(chain) < 16:
        seen.add(pid)
        parent, name = table[pid]
        chain.append({"pid": pid, "name": name})
        pid = parent
    return chain


def _descendants(table: dict[int, tuple[int, str]], root: int) -> set[int]:
    found = {root}
    grew = True
    while grew:
        grew = False
        for pid, (parent, _name) in table.items():
            if parent in found and pid not in found and pid != parent:
                found.add(pid)
                grew = True
    return found


def _named(table: dict[int, tuple[int, str]]) -> dict[int, str]:
    return {
        pid: name for pid, (_p, name) in table.items() if name.lower() in WATCH_NAMES
    }


def facts_command(_args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "pid": os.getpid(),
        "has_pthread_kill": hasattr(__import__("signal"), "pthread_kill"),
        "env": {
            key: os.environ.get(key)
            for key in ("TERM", "TERM_PROGRAM", "WT_SESSION", "MSYSTEM", "ConEmuPID")
        },
    }
    if sys.platform != "win32":
        result["supported"] = False
        return result
    kernel = _kernel32()
    result["supported"] = True
    result["descriptors"] = [_descriptor_facts(kernel, fd) for fd in (0, 1, 2)]
    result["console_window"] = bool(kernel.GetConsoleWindow())
    pids = (wintypes.DWORD * 64)()
    result["console_process_count"] = int(kernel.GetConsoleProcessList(pids, 64))
    result["ancestry"] = _ancestry(kernel, os.getpid())
    return result


class Tracker:
    """Follow a root PID and every descendant it spawns, recording exits."""

    def __init__(self, kernel: Any, root: int, root_handle: Any = None) -> None:
        self.kernel = kernel
        self.root = root
        self.started = time.monotonic()
        self.handles: dict[int, Any] = {}
        self.names: dict[int, str] = {}
        self.exits: dict[int, dict[str, Any]] = {}
        self.root_open_error: int | None = None
        if root_handle is None:
            root_handle = kernel.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, root
            )
            if not root_handle:
                self.root_open_error = _last_error()
        if root_handle:
            self.handles[root] = root_handle
            self.names[root] = _snapshot(kernel).get(root, (0, ""))[1]

    def refresh(self) -> None:
        table = _snapshot(self.kernel)
        for pid in _descendants(table, self.root):
            if pid in self.handles or pid not in table:
                continue
            handle = self.kernel.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                self.handles[pid] = handle
                self.names[pid] = table[pid][1]

    def poll(self) -> None:
        for pid, handle in self.handles.items():
            if pid in self.exits:
                continue
            code = wintypes.DWORD(0)
            if (
                self.kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                and code.value != STILL_ACTIVE
            ):
                self.exits[pid] = {
                    "exit_code": int(code.value),
                    "exit_code_hex": hex(code.value),
                    "t": round(time.monotonic() - self.started, 3),
                }

    def root_done(self) -> bool:
        return self.root in self.exits or self.root_open_error is not None

    def root_fields(self) -> dict[str, Any]:
        return {
            "root_opened": self.root_open_error is None,
            "root_open_error": self.root_open_error,
            "root_exited": self.root in self.exits,
        }

    def report(self, mark: float | None) -> list[dict[str, Any]]:
        rows = []
        for pid in sorted(self.handles):
            row: dict[str, Any] = {
                "pid": pid,
                "name": self.names.get(pid),
                "root": pid == self.root,
            }
            exit_row = self.exits.get(pid)
            if exit_row is None:
                row["alive"] = True
            else:
                row.update(exit_row, alive=False)
                if mark is not None:
                    row["since_close_s"] = round(exit_row["t"] - mark, 3)
            rows.append(row)
        return rows

    def close(self) -> None:
        for handle in self.handles.values():
            self.kernel.CloseHandle(handle)


def _follow(tracker: Tracker, timeout: float, grace: float) -> bool:
    deadline = time.monotonic() + timeout
    root_exit: float | None = None
    while time.monotonic() < deadline:
        tracker.refresh()
        tracker.poll()
        if tracker.root_done():
            root_exit = root_exit or time.monotonic()
            if time.monotonic() - root_exit >= grace:
                return True
        time.sleep(0.05)
    return False


def _pid_alive(kernel: Any, pid: int | None) -> bool | None:
    if not pid:
        return None
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD(0)
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok and code.value == STILL_ACTIVE)


def watch_command(args: argparse.Namespace) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"supported": False}
    kernel = _kernel32()
    before = _named(_snapshot(kernel))
    daemon_before = _pid_alive(kernel, args.daemon_pid)
    tracker = Tracker(kernel, args.pid)
    tracker.refresh()
    try:
        finished = _follow(tracker, args.timeout, args.grace)
        exits = [row["t"] for row in tracker.exits.values()]
        mark = min(exits) if exits else None
        after = _named(_snapshot(kernel))
        return {
            "root_pid": args.pid,
            **tracker.root_fields(),
            "finished": finished,
            "first_exit_in_tree_s": mark,
            "processes": tracker.report(mark),
            "new_watched_processes_still_alive": {
                str(pid): name for pid, name in after.items() if pid not in before
            },
            "daemon_alive_before": daemon_before,
            "daemon_alive_after": _pid_alive(kernel, args.daemon_pid),
            "field_notes": FIELD_NOTES,
        }
    finally:
        tracker.close()


def _drain(stream: Any, sink: list[bytes], first_line: threading.Event) -> None:
    while True:
        try:
            chunk = (
                stream.read(4096) if hasattr(stream, "read") else os.read(stream, 4096)
            )
        except OSError:
            break
        if not chunk:
            break
        sink.append(chunk)
        if b"\n" in b"".join(sink):
            first_line.set()
    first_line.set()


def _start_pipe(command: list[str]) -> tuple[int, Any]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return process.pid, process


def _start_conpty(kernel: Any, command: list[str]) -> dict[str, Any]:
    import msvcrt

    kernel.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.CreatePseudoConsole.argtypes = [
        wintypes.DWORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    kernel.CreatePseudoConsole.restype = ctypes.c_long
    kernel.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
    kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    in_read, in_write = wintypes.HANDLE(), wintypes.HANDLE()
    out_read, out_write = wintypes.HANDLE(), wintypes.HANDLE()
    if not kernel.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0):
        raise OSError(_last_error(), "CreatePipe(input)")
    if not kernel.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
        raise OSError(_last_error(), "CreatePipe(output)")
    hpc = ctypes.c_void_p()
    size = 120 | (30 << 16)
    hr = kernel.CreatePseudoConsole(size, in_read, out_write, 0, ctypes.byref(hpc))
    if hr != 0:
        raise OSError(hr, "CreatePseudoConsole")
    kernel.CloseHandle(in_read)
    kernel.CloseHandle(out_write)
    attr_size = ctypes.c_size_t(0)
    kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
    attr_list = ctypes.create_string_buffer(attr_size.value)
    if not kernel.InitializeProcThreadAttributeList(
        attr_list, 1, 0, ctypes.byref(attr_size)
    ):
        raise OSError(_last_error(), "InitializeProcThreadAttributeList")
    if not kernel.UpdateProcThreadAttribute(
        attr_list,
        0,
        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        hpc,
        ctypes.sizeof(ctypes.c_void_p),
        None,
        None,
    ):
        raise OSError(_last_error(), "UpdateProcThreadAttribute")
    startup = STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    startup.lpAttributeList = ctypes.cast(attr_list, ctypes.c_void_p)
    info = PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    if not kernel.CreateProcessW(
        None,
        command_line,
        None,
        None,
        False,
        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
        None,
        None,
        ctypes.byref(startup),
        ctypes.byref(info),
    ):
        raise OSError(_last_error(), "CreateProcessW")
    kernel.CloseHandle(info.hThread)
    output_fd = msvcrt.open_osfhandle(int(out_read.value or 0), os.O_RDONLY)  # type: ignore[attr-defined]
    return {
        "pid": int(info.dwProcessId),
        "process": info.hProcess,
        "hpc": hpc,
        "input": in_write,
        "output_fd": output_fd,
        "attr_list": attr_list,
    }


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"supported": False}
    command = [part for part in args.command if part != "--"]
    if not command:
        raise SystemExit("run needs a command after --")
    kernel = _kernel32()
    before = _named(_snapshot(kernel))
    daemon_before = _pid_alive(kernel, args.daemon_pid)
    output: list[bytes] = []
    first_line = threading.Event()
    conpty: dict[str, Any] | None = None
    process: Any = None
    if args.via == "pipe":
        pid, process = _start_pipe(command)
        reader = threading.Thread(
            target=_drain, args=(process.stdout, output, first_line), daemon=True
        )
    else:
        conpty = _start_conpty(kernel, command)
        pid = conpty["pid"]
        reader = threading.Thread(
            target=_drain, args=(conpty["output_fd"], output, first_line), daemon=True
        )
    reader.start()
    tracker = Tracker(kernel, pid, conpty["process"] if conpty else None)
    tracker.refresh()
    if args.via == "pipe":
        ready = first_line.wait(args.after)
    else:
        time.sleep(args.after)
        ready = first_line.is_set()
    tracker.refresh()
    tracker.poll()
    exited_before_close = tracker.root_done()
    close_mark = round(time.monotonic() - tracker.started, 3)
    if args.via == "pipe":
        process.stdin.close()
    elif args.close == "input":
        kernel.CloseHandle(conpty["input"])
    else:
        threading.Thread(
            target=kernel.ClosePseudoConsole, args=(conpty["hpc"],), daemon=True
        ).start()
    try:
        finished = _follow(tracker, args.timeout, args.grace)
        after = _named(_snapshot(kernel))
        return {
            "via": args.via,
            "close": "stdin" if args.via == "pipe" else args.close,
            "command": command,
            "root_pid": pid,
            "first_output_line_seen": bool(ready),
            "exited_before_close": exited_before_close,
            "closed_at_s": close_mark,
            **tracker.root_fields(),
            "finished": finished,
            "processes": tracker.report(close_mark),
            "new_watched_processes_still_alive": {
                str(p): name for p, name in after.items() if p not in before
            },
            "daemon_alive_before": daemon_before,
            "daemon_alive_after": _pid_alive(kernel, args.daemon_pid),
            "output_head": b"".join(output)[:2000].decode("utf-8", "replace"),
            "field_notes": FIELD_NOTES,
        }
    finally:
        tracker.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("facts", help="print GetFileType/console/pipe facts for fd 0/1/2")
    watch = sub.add_parser("watch", help="follow a PID tree until it exits (read-only)")
    watch.add_argument("--pid", type=int, required=True)
    watch.add_argument("--daemon-pid", type=int, default=None)
    watch.add_argument("--timeout", type=float, default=600.0)
    watch.add_argument("--grace", type=float, default=10.0)
    run = sub.add_parser(
        "run", help="start a command behind a pipe or ConPTY, then close it"
    )
    run.add_argument("--via", choices=("pipe", "conpty"), required=True)
    run.add_argument("--close", choices=("pty", "input"), default="pty")
    run.add_argument("--after", type=float, default=30.0)
    run.add_argument("--daemon-pid", type=int, default=None)
    run.add_argument("--timeout", type=float, default=90.0)
    run.add_argument("--grace", type=float, default=10.0)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    handlers = {"facts": facts_command, "watch": watch_command, "run": run_command}
    result = handlers[args.mode](args)
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
