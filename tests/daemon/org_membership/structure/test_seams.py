"""P0 seam: a migrated method name never falls back to the legacy router.

The catalog migrates one method at a time, so the guard cannot be the family
name: ``org``/``agent`` stay on the legacy chain until every one of their
methods is declared (C17).  A name the migration record owns but the catalog
does not bind must therefore be refused before any legacy branch sees it.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.ipc import router as router_module
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.kernel import DaemonRequestError, ipc_errors


class _RestoredDispatchHost:
    """The only state ``handle`` reads before it consults the catalog."""

    def __init__(self) -> None:
        self._restore_done = threading.Event()
        self._restore_done.set()
        self._inbox = object()
        self._harnesses = object()
        self._presence = object()
        self.calls = []

    def _ipc_management_adapter_remove(self, params):
        self.calls.append(params)
        return {"ok": True}

    def _ipc_outbox_prune(self, params):
        self.calls.append(params)
        return {"ok": True}


def test_a_migrated_name_without_a_binding_is_refused_before_the_legacy_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _RestoredDispatchHost()
    monkeypatch.setattr(router_module, "METHOD_REGISTRY", frozenset())
    monkeypatch.setattr(router_module, "MIGRATED_METHODS", frozenset({"management.adapter.remove"}))
    with pytest.raises(DaemonRequestError) as caught:
        _IpcDispatchMixin.handle(host, "management.adapter.remove", {"target": "group-x"})
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND
    assert host.calls == []


def test_a_method_outside_the_migration_tables_still_uses_its_legacy_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _RestoredDispatchHost()
    monkeypatch.setattr(router_module, "MIGRATED_METHODS", frozenset())
    assert "outbox.prune" not in router_module.METHOD_REGISTRY
    assert "outbox" not in router_module.MIGRATED_FAMILIES
    result = _IpcDispatchMixin.handle(host, "outbox.prune", {"target": "group-x"})
    assert result == {"ok": True}
    assert host.calls == [{"target": "group-x"}]


def test_the_daemon_facade_table_and_allowlist_still_agree() -> None:
    """P0 re-laid out ``__all__`` only; the public names are unchanged."""

    from hyprial.daemon import interface

    assert set(interface._FACADE_EXPORTS) == set(interface.__all__)
    assert len(interface.__all__) == len(set(interface.__all__))
    assert interface.__getattr__("OrgContextStore") is not None
    assert interface.__getattr__("wait_for_state_ownership") is not None


ORG_COMMANDS_ROOT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "hyprial"
    / "shell"
    / "impl"
    / "cli"
    / "commands"
    / "org"
)


def test_the_org_subpackages_own_the_bodies_the_group_only_registers() -> None:
    from hyprial.shell.impl.cli.commands.org import commands as org_commands
    from hyprial.shell.impl.cli.commands.org.context import commands as context_commands
    from hyprial.shell.impl.cli.commands.org.member import commands as member_commands

    registered = {
        command.name: command.callback
        for command in org_commands.org_app.registered_commands
    }
    for name in ("invite", "join", "leave", "remove", "pending"):
        assert registered[name] is getattr(member_commands, f"org_{name}")
    context_registered = {
        command.name: command.callback
        for command in org_commands.context_app.registered_commands
    }
    context_callbacks = {
        "validate": context_commands.org_context_validate,
        "show": context_commands.org_show,
        "status": context_commands.org_status,
        "fetch": context_commands.org_fetch,
        "import": context_commands.org_import,
    }
    for name, callback in context_callbacks.items():
        assert context_registered[name] is callback

    # `org group invite` stays an alias of the same callable, not a copy.
    group_commands = {
        command.name: command.callback
        for command in org_commands.group_app.registered_commands
    }
    assert group_commands["invite"] is member_commands.org_invite


def test_the_extracted_org_command_modules_import_no_backend_module() -> None:
    """The CLI->backend import ledger is per file, so the move adds no edge."""

    backend = {"hyprial.kernel", "hyprial.identity", "hyprial.daemon", "hyprial.biz"}
    for relative in (
        "context/commands.py",
        "member/commands.py",
        "member/render.py",
    ):
        tree = ast.parse((ORG_COMMANDS_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                head = ".".join(node.module.split(".")[:2])
                assert head not in backend, (relative, node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    head = ".".join(alias.name.split(".")[:2])
                    assert head not in backend, (relative, alias.name)
