"""Public CLI composition boundary for command families.

The process entry owns argv, machine-compatibility rewrites and process
exits.  Command families must not import that entry: they register against a
:class:`CliRuntime`, which supplies the two cross-cutting ports every command
needs:

* :meth:`CliRuntime.request` -- the one daemon request client.  The resolver
  is read on every call, so a composition root (or a test) can replace the
  request callable without rebuilding the tree.
* :meth:`CliRuntime.execute` -- the one result boundary.  Operations return a
  :class:`~hyprial.shell.impl.cli.output.CliResult` or ``CliStream``; this
  method delegates to the existing ``cli/output.run_command`` renderer.  A
  family never writes stdout/stderr itself.

Minimal B/C usage (registration happens in A's shared
``runtime/registration.py``; the family module only exports the registrar)::

    import typer
    from hyprial.shell.impl.cli.output import CliResult
    from hyprial.shell.impl.cli.runtime.composition import CliRuntime

    def register_links(app: typer.Typer, runtime: CliRuntime) -> None:
        @app.command("list")
        def link_list(json_output: bool = typer.Option(False, "--json")) -> None:
            operation = lambda: CliResult(runtime.request("link.list", {}))
            runtime.execute(operation, json_output=json_output)

    # A-owned shared wiring:
    runtime.add_registrar(register_links, parent=link_app)

No placeholder command is registered for an unimplemented family: the
registrar is only called when the real handler exists.  The runtime does not
import the process entry, a command family, Typer's app globals or output
internals, so it can be constructed by a focused test or by a future CLI
composition root.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import typer

JsonObject = dict[str, Any]


class RequestPort(Protocol):
    """The generic daemon request callable injected by the composition root."""

    def __call__(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout: float | None = None,
        restore_wait: float | None = None,
    ) -> Any: ...


class ExecutePort(Protocol):
    """The single CliResult/CliStream output boundary."""

    def __call__(
        self,
        operation: Callable[[], Any],
        *,
        json_output: bool,
        json_indent: int | None = None,
        allow_missing_home: bool = False,
    ) -> None: ...


class CommandRegistrar(Protocol):
    """A B/C-style family registrar: register commands, receive live ports."""

    def __call__(self, app: typer.Typer, runtime: "CliRuntime") -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class CliRuntime:
    """Live ports and registration surface for one CLI composition root.

    ``request_resolver`` and ``execute_resolver`` are *resolvers*, not bound
    functions: every call resolves the current port.  This preserves the
    existing per-invocation injection contract without making command
    families read a service locator or the entry module.
    """

    app: typer.Typer
    request_resolver: Callable[[], RequestPort]
    execute_resolver: Callable[[], ExecutePort]
    worker_session_resolver: Callable[[], Any] | None = None

    def request(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout: float | None = None,
        restore_wait: float | None = None,
    ) -> Any:
        """Call the injected daemon request client."""

        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if restore_wait is not None:
            kwargs["restore_wait"] = restore_wait
        # Keep the historical injection shape: a request without params is
        # called with only the method, so existing fake request ports keep
        # their narrower signatures.
        if params is None:
            return self.request_resolver()(method, **kwargs)
        return self.request_resolver()(method, params, **kwargs)

    def worker_session_binding(self) -> Any:
        """Resolve the carrier binding lazily for this operation."""

        if self.worker_session_resolver is None:
            raise RuntimeError("worker session resolver is not configured")
        return self.worker_session_resolver()

    def execute(
        self,
        operation: Callable[[], Any],
        *,
        json_output: bool,
        json_indent: int | None = None,
        allow_missing_home: bool = False,
    ) -> None:
        """Run one operation through the only result/error output path."""

        kwargs: dict[str, Any] = {"json_output": json_output}
        if json_indent is not None:
            kwargs["json_indent"] = json_indent
        if allow_missing_home:
            kwargs["allow_missing_home"] = True
        self.execute_resolver()(operation, **kwargs)

    def add_family(
        self,
        family: typer.Typer,
        *,
        name: str,
        parent: typer.Typer | None = None,
        hidden: bool = False,
    ) -> typer.Typer:
        """Attach one command family to the root (or a parent group)."""

        (parent or self.app).add_typer(family, name=name, hidden=hidden)
        return family

    def add_registrar(
        self,
        register: CommandRegistrar,
        *,
        parent: typer.Typer | None = None,
    ) -> Mapping[str, Any]:
        """Run a family registrar with the live runtime.

        The returned mapping is the registrar's callback namespace.  It is
        intentionally not merged into the runtime or the entry: callers that
        need named callbacks (the legacy transfer surface) own that mapping.
        """

        return register(parent or self.app, self) or {}
