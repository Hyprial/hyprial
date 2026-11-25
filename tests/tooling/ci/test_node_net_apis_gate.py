"""Executable contract for the Node net-API gate.

The gate is ``scripts/ci/check_node_net_apis.py``: outside the one shared
local-IPC module, no ``.ts`` / ``.mjs`` / ``.js`` / ``.cjs`` file under
``src/hyprial`` may call ``net.createConnection`` / ``net.connect`` /
``net.createServer`` through any binding of ``node:net``.  There is no
baseline -- a hit is always red -- so the live tree must scan clean.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ci" / "check_node_net_apis.py"
SPEC = importlib.util.spec_from_file_location("check_node_net_apis", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

OTHER_FILE = "src/hyprial/daemon/impl/harnesses/pi/resources/pi_harness_bridge.ts"
SHARED_MODULE = "src/hyprial/daemon/impl/harnesses/pi/resources/pi_local_ipc.ts"


def findings(source: str, path: str = OTHER_FILE) -> list[str]:
    return [row["symbol"] for row in GATE.scan_source(source, path)]


@pytest.mark.parametrize(
    "source",
    [
        'import * as net from "node:net";\nnet.createConnection(path);\n',
        'import * as net from "node:net";\nnet.connect(path);\n',
        'import * as net from "node:net";\nnet.createServer(handler);\n',
        'import net from "node:net";\nnet.createConnection(path);\n',
        'import * as pipe from "node:net";\npipe.createConnection(name);\n',
        'import { createConnection } from "node:net";\ncreateConnection(name);\n',
        'import { connect as dial } from "node:net";\ndial(name);\n',
        'import { createServer } from "node:net";\n',
        'const net = require("node:net");\nnet.createConnection(name);\n',
        'const net = require("node:net");\nnet . createServer(h);\n',
        'const net = await import("node:net");\nnet.connect(name);\n',
        'const socket = require("node:net").createConnection(name);\n',
    ],
    ids=[
        "namespace-createConnection",
        "namespace-connect",
        "namespace-createServer",
        "default-import",
        "aliased-namespace",
        "from-import-bare-call",
        "from-import-renamed-call",
        "from-import-unused-still-red",
        "require-binding",
        "whitespace-member-access",
        "dynamic-import",
        "inline-require-chain",
    ],
)
def test_every_binding_and_member_is_red_outside_the_shared_module(source: str) -> None:
    assert findings(source)


def test_net_usage_inside_the_shared_module_is_not_reported() -> None:
    source = (
        'import * as net from "node:net";\n'
        "net.createConnection(path);\n"
        "net.connect(path);\n"
        "net.createServer(handler);\n"
    )
    assert GATE.scan_source(source, SHARED_MODULE) == []


def test_unrelated_createConnection_calls_are_not_reported() -> None:
    assert findings("const pool = makePool();\npool.createConnection(url);\n") == []


def test_files_without_node_net_are_never_reported() -> None:
    assert findings("const socket = connectLocalIpc(endpoint);\n") == []


def test_the_live_tree_scans_clean() -> None:
    report = GATE.scan(ROOT)
    assert report["errors"] == []
    assert report["findings"] == []
    assert report["status"] == "PASS"


def test_the_gate_script_exit_code_matches_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert GATE.main(["--root", str(ROOT)]) == 0
    assert "node-net-apis gate: PASS" in capsys.readouterr().out
