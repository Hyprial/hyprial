"""Z-6b: the GUI child can no longer host the bootstrap or promote.

A tripwire, not a gate: it only catches the members that armed or ran a
promotion coming back on ``GuiRuntimeHost`` and a direct import of the bootstrap
runtime, the bootstrap dispatcher or the daemon application factory by name in
the child's two modules.  It does not see attribute access through a module
(``from .bootstrap import dispatcher``) or reflection; the structural and
boundary gates and review are the real guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.gui_host import GuiRuntimeHost

SOURCE = Path(__file__).resolve().parents[4] / "src" / "hyprial"
CHILD_MODULES = (
    SOURCE / "daemon" / "impl" / "application" / "onboarding" / "gui_host.py",
    SOURCE / "shell" / "impl" / "request_http" / "bootstrap" / "gui.py",
)
FORBIDDEN_NAMES = ("BootstrapRuntime", "BootstrapDispatcher", "build_daemon_application")


@pytest.mark.parametrize(
    "member",
    ["enable_promotion", "run_promoted", "take_post_reply", "reopen_required", "stop_promoted"],
)
def test_the_gui_host_has_no_promotion_member(member):
    assert not hasattr(GuiRuntimeHost, member)


@pytest.mark.parametrize("module", CHILD_MODULES, ids=lambda path: path.name)
def test_the_child_modules_do_not_import_the_hosted_machinery(module):
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        alias.name.rsplit(".", 1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not imported.intersection(FORBIDDEN_NAMES)
