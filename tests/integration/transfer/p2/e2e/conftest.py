"""AT07 I4: two real daemons, one P2 chain, and the one row that has to exist.

``PROVEN_CHAINS`` is a module constant on purpose: nothing on the wire, in the
environment or on a command line may open that fence inside a running daemon.
``chain_tree`` copies the frozen tree unchanged and has one short-lived process
read the *landed* table out of it, so a row that vanishes takes this run down
before a daemon starts.  The fence's negative arm rewrites a scratch copy whose
**only** difference is that the row is gone.  Either way the daemons import
that tree through ``PYTHONPATH`` and the test process never does.

Everything else is real: two ``hyprial daemon run`` subprocesses with their own
homes, states, sockets and node ids, no mesh, and ``pi`` workers launched
against the repository's own protocol double (real daemon, real lifecycle, fake
model protocol -- the model-credential half of the card is TC012 and stays
BLOCKED).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import (
    IsolatedHarness,
)
from tests.support.runtime._tmp import hyprial_test_tmp_root

REPO = Path(__file__).resolve().parents[5]
OWNER = "at07-owner"
SOURCE_NODE = "at07-source"
TARGET_NODE = "at07-target"
WORKER = "at07-worker"

#: The two harness protocol doubles the P2 e2e drives.  ``pi`` is the row I4
#: landed; ``claude`` is the row this slice proves.  Both are the repository's
#: own doubles, named in the reading's disclosure.
FAKE_PI = REPO / "tests/fixtures/harnesses/fake_pi_rpc.py"
FAKE_CLAUDE = REPO / "tests/fixtures/harnesses/fake_claude_sdk.py"

#: The reading the landed row cites.  ``chain_tree`` reads the row back from the
#: frozen tree and asserts it still names this file.
PROOF = "docs/notes/at07-i4-run.txt"
#: The codex first-proof reading; the same frozen-tree check binds its row.
CODEX_PROOF = "docs/notes/c3-codex-run.txt"
#: The claude row's reading, landed in the same commit as the row.
PROOF_CLAUDE = "docs/notes/c3-claude-run.txt"
#: The neighbour arm's harness.  Its own row is deleted from the scratch tree
#: the neighbour daemon loads, so proof for the other rows cannot leak to it.
#: (Codex was this neighbour before its own C3-S1-C row landed; once every
#: transferable harness had a row, the absence is built on purpose instead of
#: borrowed from a harness nobody has run.)
NEIGHBOUR = "claude"

_TABLE = "PROVEN_CHAINS: tuple[ProvenChain, ...] ="


def _table_span(text: str) -> tuple[int, int]:
    """The whole ``PROVEN_CHAINS`` assignment, parens balanced."""

    start = text.index(_TABLE)
    index = text.index("=", start) + 1
    while text[index] in " \n":
        index += 1
    depth = 0
    while True:
        char = text[index]
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
            if depth == 0:
                return start, index + 1
        index += 1


def _rewrite_table(text: str, body: str) -> str:
    start, end = _table_span(text)
    return text[:start] + f"{_TABLE} {body}" + text[end:]


def _scratch_tree(prefix: str, body: str | None) -> Path:
    """A copy of ``src/`` whose only difference is the proven-chain table.

    ``body=None`` leaves the table exactly as the frozen tree has it.
    """

    root = Path(tempfile.mkdtemp(prefix=prefix, dir=hyprial_test_tmp_root()))
    src = root / "src"
    shutil.copytree(REPO / "src", src, symlinks=True)
    if body is not None:
        lift = src / "hyprial/daemon/impl/transfer/p2/orchestration/lift.py"
        lift.write_text(_rewrite_table(lift.read_text(), body))
    return src


def _table_body_without(harness: str) -> str:
    """The frozen table's body text with one harness's entry deleted.

    The neighbour arm needs a table that proves the *other* harnesses and no
    row for its own; building that absence on purpose is the only reading left
    once every transferable harness has a landed row.
    """

    text = (
        REPO / "src/hyprial/daemon/impl/transfer/p2/orchestration/lift.py"
    ).read_text()
    start, end = _table_span(text)
    body = text[start:end]
    rhs = body[body.index("=") + 1 :].lstrip()
    entry_at = rhs.index(f'    ProvenChain(\n        "{harness}",')
    following_at = rhs.find("\n    ProvenChain(", entry_at + 1)
    if following_at == -1:
        return rhs[:entry_at].rstrip("\n") + "\n)"
    return rhs[:entry_at] + rhs[following_at + 1 :]


def _landed_table(src: Path) -> dict:
    """The fence's table as a daemon reads it: import the frozen tree, not the test.

    One short-lived process gets ``PYTHONPATH`` pointed at the copied tree and
    prints the rows back; the test process itself never imports that tree.
    """

    probe = (
        "import json\n"
        "from hyprial.daemon.impl.transfer.p2.orchestration.lift import (\n"
        "    P2_CHAIN_MODE, PROVEN_CHAINS,\n"
        ")\n"
        "print(json.dumps({\n"
        '    "mode": P2_CHAIN_MODE,\n'
        '    "rows": [[row.harness, row.mode, row.evidence] for row in PROVEN_CHAINS],\n'
        "}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip())


@pytest.fixture(scope="module")
def chain_tree() -> Path:
    """The tree this run proves: the frozen tree, unchanged.

    ③-4 landed the row the run justified, so the run reads that row where the
    daemons read it -- the frozen table itself.  The check parses rather than
    matches text, and it lives here so that a row deleted from the tree fails
    this run instead of being covered by a fixture that writes the row back.
    """

    src = _scratch_tree("at07-tree-", None)
    try:
        table = _landed_table(src)
        rows = [(harness, mode) for harness, mode, _ in table["rows"]]
        # Every landed row reads back from the frozen table: pi (I4), codex
        # (C3-S1-C) and claude (C3-S2-C).  Each row's evidence must name this
        # run's reading, so rows and readings cannot drift apart; a missing
        # row takes that arm down before a daemon starts.
        for harness, proof in (
            ("pi", PROOF),
            ("codex", CODEX_PROOF),
            ("claude", PROOF_CLAUDE),
        ):
            assert (harness, table["mode"]) in rows, rows
            evidence = next(
                evidence
                for row_harness, mode, evidence in table["rows"]
                if (row_harness, mode) == (harness, table["mode"])
            )
            assert proof in evidence, evidence
        yield src
    finally:
        shutil.rmtree(src.parent, ignore_errors=True)


@pytest.fixture(scope="module")
def closed_tree() -> Path:
    """The same tree with the row removed -- the fence's own negative arm.

    A missing row is not "untested": it is the exact condition the fence exists
    to refuse, so the run builds it on purpose instead of borrowing the frozen
    tree's state.
    """

    src = _scratch_tree("at07-closed-", "()")
    try:
        rewrite = (
            src / "hyprial/daemon/impl/transfer/p2/orchestration/lift.py"
        ).read_text()
        assert 'ProvenChain("pi"' not in rewrite
        assert 'ProvenChain("codex"' not in rewrite
        assert 'ProvenChain("claude"' not in rewrite
        yield src
    finally:
        shutil.rmtree(src.parent, ignore_errors=True)


def _environment(root: Path, *, node_id: str, python_path: Path) -> dict[str, str]:
    """A daemon that cannot reach a real home, mesh, credential or socket."""

    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(python_path),
        "HOME": str(root),
        "HYPRIAL_HOME": str(root / "home"),
        "HARNESS_STATE_DIR": str(root / "state"),
        "HYPRIAL_NODE_ID": node_id,
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_LISTEN": "",
        "HYPRIAL_ZENOH_CONNECT": "",
        "UV_TOOL_DIR": str(root / "uv-tools"),
        "FAKE_PI_MODE": "settled",
        # The claude double is injected through the daemon-side SDK bootstrap
        # seam (``sdk_worker_command`` reads this in the daemon, not the
        # child).  ``FAKE_CLAUDE_MODE`` rides the child whitelist so the
        # worker's behaviour is explicit and the reading can name it.
        "HYPRIAL_BUNDLED_SDK_BOOTSTRAP": str(FAKE_CLAUDE),
        "FAKE_CLAUDE_MODE": "settled",
    }


@pytest.fixture(params=["pi", "claude"])
def harness(request: pytest.FixtureRequest) -> str:
    """The two harnesses the P2 chain is read for: the I4 row and this slice."""

    return str(request.param)


def _node(*, node_id: str, python_path: Path, prefix: str) -> IsolatedHarness:
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=hyprial_test_tmp_root()))
    node = IsolatedHarness(root)
    node.environment = _environment(root, node_id=node_id, python_path=python_path)
    node.write_owner(OWNER)
    return node


@pytest.fixture
def source_node(chain_tree: Path):
    node = _node(node_id=SOURCE_NODE, python_path=chain_tree, prefix="at07-src-")
    try:
        yield node
    finally:
        _teardown(node, WORKER)


@pytest.fixture
def target_node(chain_tree: Path):
    node = _node(node_id=TARGET_NODE, python_path=chain_tree, prefix="at07-tgt-")
    try:
        yield node
    finally:
        _teardown(node, WORKER)


@pytest.fixture
def neighbour_node() -> IsolatedHarness:
    """One daemon on the run tree with the neighbour's own row deleted.

    The row axis and the harness axis are read on tables that differ by one
    entry: the other rows stay, the neighbour's named row is absent, so the
    same landed table cannot admit the harness nobody ran.  The absence is
    built on purpose -- once every transferable harness has a row, there is
    no unproven harness left to borrow.
    """

    tree = _scratch_tree("at07-neighbour-", _table_body_without(NEIGHBOUR))
    node = _node(
        node_id=TARGET_NODE, python_path=tree, prefix="at07-neighbour-"
    )
    try:
        yield node
    finally:
        _teardown(node, WORKER)
        shutil.rmtree(tree.parent, ignore_errors=True)


@pytest.fixture
def unproven_node(closed_tree: Path) -> IsolatedHarness:
    """One daemon on the tree with the row removed: the fence's own arm."""

    node = _node(
        node_id=SOURCE_NODE, python_path=closed_tree, prefix="at07-frozen-"
    )
    try:
        yield node
    finally:
        _teardown(node, WORKER)


def _teardown(node: IsolatedHarness, *names: str) -> None:
    if node.daemon is not None:
        for name in names:
            try:
                # No provider: the teardown runs for whichever harness the
                # arm seeded, so a claude arm must not be downed as ``pi``.
                node.ipc("down", {"name": name})
            except (AssertionError, OSError):
                pass
        node.stop_daemon()
    shutil.rmtree(node.root, ignore_errors=True)
