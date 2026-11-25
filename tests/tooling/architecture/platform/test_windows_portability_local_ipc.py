"""Executable contract for rule W020: no handwritten local IPC outside the seams.

Local IPC (unix domain sockets, Windows named pipes) belongs to one kernel
seam: ``hyprial.kernel.listen_local_ipc`` / ``connect_local_ipc``.  W020 in
``scripts/ci/check_windows_portability.py`` counts every handwritten form
outside ``PLATFORM_SEAMS`` guarded or not -- a guarded ``AF_UNIX`` still broke
``hyprial init`` on Windows (bootstrap ``wire.py`` raised behind ``hasattr``)
-- and the rule keeps NO baseline entry: a key with it is a baseline format
error, so the count must reach zero and stay there.  W006 keeps its own
(guard-aware) semantics unchanged.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_windows_portability.py"
SPEC = importlib.util.spec_from_file_location("check_windows_portability", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

PROBE = "src/hyprial/daemon/impl/probe.py"
SEAM = "src/hyprial/kernel/impl/manysys/ipc/__init__.py"

AF_UNIX_LISTEN = "import socket\ns = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"


def rules(source: str, path: str = PROBE) -> list[str]:
    return sorted(row["rule"] for row in GATE.scan_source(source, path))


@pytest.mark.parametrize(
    "source",
    [
        # socket.AF_UNIX: attribute access, from-import, capability probing
        AF_UNIX_LISTEN,
        "from socket import AF_UNIX\nfamily = AF_UNIX\n",
        "import socket\nfamily = getattr(socket, 'AF_UNIX')\n",
        "import socket\nfamily = getattr(socket, 'AF_UNIX', socket.AF_UNSPEC)\n",
        "import socket\nok = hasattr(socket, 'AF_UNIX')\n",
        # asyncio module-level unix endpoints
        "import asyncio\nasyncio.start_unix_server(serve, path='p')\n",
        "import asyncio\nasyncio.open_unix_connection('p')\n",
        "from asyncio import start_unix_server\nstart_unix_server(serve)\n",
        "from asyncio import open_unix_connection\nopen_unix_connection('p')\n",
        # event-loop unix endpoints (the name loop, as W006 sees them)
        "server = loop.create_unix_server(serve, path='p')\n",
        "transport = loop.create_unix_connection(protocol, path='p')\n",
        # socketserver unix servers
        "import socketserver\nclass S(socketserver.UnixStreamServer):\n    pass\n",
        "import socketserver\nclass S(socketserver.UnixDatagramServer):\n    pass\n",
        "from socketserver import UnixStreamServer\nclass S(UnixStreamServer):\n    pass\n",
        "from socketserver import UnixDatagramServer\nUnixDatagramServer\n",
        # the Windows named-pipe API: import or call, under any binding
        "from hyprial.kernel import connect_named_pipe\n",
        "from hyprial.kernel import connect_named_pipe as connect\nconnect(path, 1.0)\n",
        "from hyprial.kernel import listen_named_pipe\nlisten_named_pipe(path)\n",
        "from hyprial.kernel.impl.manysys.process.ports import listen_named_pipe\n",
        "from hyprial.kernel.impl.manysys.process import ports\nports.connect_named_pipe(path, 1.0)\n",
        "from hyprial.kernel.impl.manysys.ipc.windows_pipe import connect\n",
        "import hyprial.kernel.impl.manysys.ipc.windows_pipe\n",
        "from hyprial.kernel.impl.manysys.ipc import windows_pipe\n",
        "import win32pipe\n",
        "from win32pipe import CreateNamedPipe\n",
    ],
)
def test_every_raw_local_ipc_form_is_counted(source: str) -> None:
    assert "W020" in rules(source), (source, GATE.scan_source(source, PROBE))


@pytest.mark.parametrize(
    "source",
    [
        # capability guard (the wire.py shape): still counted
        "import socket\nif hasattr(socket, 'AF_UNIX'):\n"
        "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n",
        # platform guard with the right polarity: still counted
        "import os, socket\nif os.name == 'posix':\n"
        "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n",
        # try/except NotImplementedError around a loop endpoint: still counted
        "try:\n    server = loop.create_unix_server(serve, path='p')\n"
        "except NotImplementedError:\n    server = None\n",
        # the pipe API behind a platform guard: still counted
        "import os\nfrom hyprial.kernel import listen_named_pipe\n"
        "if os.name == 'nt':\n    listener = listen_named_pipe(path)\n",
        # getattr-with-default probe used as a guard: the probe itself counts
        "import socket\ndef f():\n    if getattr(socket, 'AF_UNIX', None) is None:\n"
        "        return None\n    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n",
    ],
)
def test_a_guard_does_not_excuse_raw_local_ipc(source: str) -> None:
    assert "W020" in rules(source), (source, GATE.scan_source(source, PROBE))


def test_the_bootstrap_wire_shape_is_the_prime_case() -> None:
    source = (
        "import socket\n"
        "def _bind(path):\n"
        "    if not hasattr(socket, 'AF_UNIX'):\n"
        '        raise OSError("this platform has no unix domain sockets")\n'
        "    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    )
    # The probe and the use are both W020; W006 stays guard-aware and silent.
    assert rules(source) == ["W020", "W020", "W021"], GATE.scan_source(source, PROBE)


def test_a_pipe_alias_import_and_its_call_each_count() -> None:
    source = (
        "from hyprial.kernel import listen_named_pipe as PipeListener\n"
        "server = PipeListener(path)\n"
    )
    assert rules(source) == ["W020", "W020"], GATE.scan_source(source, PROBE)


def test_w006_keeps_its_guard_aware_semantics_next_to_w020() -> None:
    # Unguarded AF_UNIX is still W006 (baseline keys unchanged) ...
    assert rules(AF_UNIX_LISTEN) == ["W006", "W020"]
    # ... and a genuine capability guard still suppresses only W006; the W020
    # pair is the probe itself plus the use.
    guarded = (
        "import socket\nif hasattr(socket, 'AF_UNIX'):\n"
        "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    )
    assert rules(guarded) == ["W020", "W020"], GATE.scan_source(guarded, PROBE)


@pytest.mark.parametrize(
    "source",
    [
        # the seam every caller is pointed at
        "from hyprial.kernel import connect_local_ipc\nconnect_local_ipc(path, timeout=1.0)\n",
        "from hyprial.kernel import listen_local_ipc\nlistener = listen_local_ipc(path)\n",
        "import hyprial.kernel\nhyprial.kernel.connect_local_ipc(path)\n",
        # cross-platform sockets and servers are untouched
        "import socket\ns = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)\n",
        "import socketserver\nclass S(socketserver.ThreadingTCPServer):\n    pass\n",
        "from socketserver import TCPServer\nclass S(TCPServer):\n    pass\n",
        # the kernel's cross-platform IPC client surface is not the raw API
        "from hyprial.kernel import daemon_request_once\ndaemon_request_once()\n",
    ],
)
def test_portable_forms_are_not_counted(source: str) -> None:
    assert rules(source) == [], GATE.scan_source(source, PROBE)


def test_the_platform_seams_may_own_every_form() -> None:
    source = (
        "import socket\nimport win32pipe\n"
        "from hyprial.kernel.impl.manysys.ipc.windows_pipe import connect\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "server = loop.create_unix_server(serve, path='p')\n"
        "from hyprial.kernel import listen_named_pipe\nlisten_named_pipe(path)\n"
    )
    assert rules(source, SEAM) == [], GATE.scan_source(source, SEAM)


def _tree(tmp_path: Path, body: str) -> Path:
    source = tmp_path / PROBE
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")
    return tmp_path


def _entry() -> dict[str, object]:
    return {"count": 1, "reason": "debt-0.5.5.4: kernel owned-process seam", "removal": "0.5.5.4"}


def _document(entries: dict[str, dict[str, object]]) -> str:
    return json.dumps({"version": GATE.BASELINE_VERSION, "entries": entries})


def _baseline(root: Path, entries: dict[str, dict[str, object]]) -> None:
    path = root / GATE.BASELINE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_document(entries), encoding="utf-8")


def test_a_w020_baseline_key_is_a_format_error() -> None:
    with pytest.raises(GATE.GateFormatError, match="W020"):
        GATE.parse_baseline(_document({PROBE: {"W020": _entry()}}))


def test_w020_findings_cannot_be_grandfathered_by_the_scan(tmp_path: Path) -> None:
    root = _tree(tmp_path, AF_UNIX_LISTEN)
    _baseline(root, {})
    report = GATE.scan(root)
    assert report["status"] == "FAIL"
    row = next(item for item in report["newFindings"] if item["rule"] == "W020")
    assert (row["count"], row["baseline"]) == (1, 0)
