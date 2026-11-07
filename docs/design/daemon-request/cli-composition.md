# A-02 CLI composition and request/output boundary

- Date: 2026-10-28
- Work package: A-02 (`2188b4ed-b5c5-4fe7-a9ba-d1fe9577a393`)
- Branch: `refactor/a02-cli-composition`
- Scope: public registration/DI and the single result output path; no move
  business, no per-family migration, no new compatibility alias or test-only
  re-export.

## 1. Modules and lifecycle

| Module | Responsibility |
| --- | --- |
| `shell/impl/cli/runtime/composition.py` | Public `CliRuntime`: live request port, live `CliResult`/`CliStream` execution boundary, family/registrar registration. Imports neither the process entry nor a command family. |
| `shell/impl/cli/runtime/registration.py` | A-owned shared wiring: imports the command modules in their historical order, attaches real families to the process root, and rejects repeated or wrong-root assembly. |
| `shell/impl/cli/runtime/services.py` | Imports owning helpers and builds the one explicit legacy `CliServices` instance. Request/execute stay on `CliRuntime`; no per-call `globals()`/`getattr` sweep. |
| `shell/impl/cli/runtime/machine_argv.py` | Previous-release machine argv rewrite table and resolver. |
| `shell/impl/cli/runtime/entry.py` | Process entry only: ~100 lines, `main`, Typer/error exits and the owned `TyperClickException`/`TyperAbort` classes. `__all__ = ["main"]`; it does not import command families or re-export their helpers. |

`build_app(runtime)` is process composition: it composes the module-level root
app that command decorators already populate, exactly once. It rejects:

- a second assembly (`RuntimeError`), rather than duplicating Typer state;
- a runtime whose `app` is not the process root (`ValueError`).

An independent family test constructs `CliRuntime(app=typer.Typer(), ...)` and
uses `add_registrar`/`add_family`; it does not call `build_app`.

## 2. Minimal B/C usage

A family exports a registrar; A's shared `registration.py` calls it with the
live runtime and parent group. The family never imports `entry` and never uses
`get_services()` for the generic ports.

```python
import typer
from hyprial.shell.impl.cli.output import CliResult
from hyprial.shell.impl.cli.runtime import CliRuntime


def register_links(app: typer.Typer, runtime: CliRuntime) -> None:
    @app.command("list")
    def link_list(json_output: bool = typer.Option(False, "--json")) -> None:
        def operation() -> CliResult:
            return CliResult(runtime.request("link.list", {}))

        runtime.execute(operation, json_output=json_output)


# A-owned shared wiring:
runtime.add_registrar(register_links, parent=link_app)
```

Rules:

- `runtime.request(method, params, *, timeout, restore_wait)` is the generic
  daemon request port. It resolves `support._daemon_request` on every call, so
  replacing the owning support callable is the explicit test/operator seam. Do
  not add a second client or a blind retry for writes.
- `runtime.execute(operation, *, json_output, ...)` is the only result/error
  output path. `operation` returns `CliResult`, `CliStream` or a mapping as
  accepted by the existing output layer; a command must not write stdout.
- Register only commands that have a real handler. A move command is wired in
  the same B PR that supplies its handler; A-02 adds no placeholder.
- B keeps move business, MethodSpec/handler ownership and the thin move CLI;
  C migrates the remaining command groups. Both consume the same `CliRuntime`.

## 3. Legacy service snapshot and tests

`CliServices` remains the flat snapshot for unmigrated families, but it is now
an explicitly assembled mutable instance:

- `registration.build_app` completes first, then `runtime.services.install_services`
  imports owning helpers and installs one instance;
- `get_services()` returns that instance; there is no resolver reading the
  process entry and no `globals()` snapshot;
- request/execute are not duplicate fields; commands use
  `services.runtime.request/execute`;
- the process entry no longer re-exports command helpers or callback aliases;
  tests import the owning module or patch the explicit service instance. The
  legacy snapshot has 54 fields and is frozen by
  `tests/shell/cli/runtime/test_cli_composition.py`; C/DR-30 retires it by
  group.

`transfer/runtime` and `transfer/move` keep their own registrar/dependency
ownership. `parse_depends` is a module-level function in `transfer/move.py`;
there is no second transfer callback namespace or test-only getter.

## 4. Verification hooks

- `tests/shell/cli/runtime/test_cli_composition.py`: B/C-style registrar,
  injected request, live output boundary, composition lifecycle rejection,
  frozen legacy snapshot, no entry/command import in composition.
- `tests/shell/cli/output/test_output_discipline.py`: command modules still do
  not write output themselves.
- `tests/shell/cli/presentation/test_cli_help_inventory.py`,
  `test_ci_cli_help_inventory.py`: registered tree and `hyprial help` stay in
  agreement; the exact recursive help tree was compared against
  `intg/cli-command-surface@78d77e360`.
- `tests/shell/entrypoints/test_shell_cli_entry.py`: installed entry delegates
  to the native Shell `main` and `__all__ == ["main"]`.
- `tests/integration/transfer/commands/test_transfer_cli_structure.py`:
  transfer commands use the registered tree and the owning `parse_depends`.
