"""Public facade generation with explicit lazy forwarding.

Rule kind ``facade_export`` adds one approved export to a facade module
(package ``__init__.py`` or ``interface.py``). Two modes:

- ``lazy`` (default): the facade module is tool-managed and forwards the
  symbol through a generated module-level ``__getattr__`` plus a
  ``TYPE_CHECKING`` import, so importing the package never eagerly imports
  the implementation module. Star imports are never generated.
- ``eager``: appends ``from <provider> import <symbol>`` to an existing
  file after verifying the name is not already bound at top level.

Conflicts (same symbol exported from a different provider, symbol already
defined in the facade, non-tool-managed lazy facade) are refused.
"""

from __future__ import annotations

import libcst as cst

from .common import Diagnostic, ERROR, Rule, RuleOutcome
from .parsing import ParsedPython, TransformContext, assign_target_names, import_target_name

KINDS = ("facade_export",)

MANAGED_MARKER = "managed by scripts/refactor (facade)"


class _Refusal(Exception):
    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


def _render_lazy(facade_module: str, exports: list[tuple[str, str]]) -> str:
    exports = sorted(exports)
    type_lines = "\n".join(
        f"    from {provider} import {symbol}" for symbol, provider in exports
    )
    map_lines = ",\n".join(f'    "{symbol}": "{provider}"' for symbol, provider in exports)
    all_line = ", ".join(f'"{symbol}"' for symbol, _ in exports)
    return f'''"""Public facade for {facade_module}. {MANAGED_MARKER}.

Exports are forwarded lazily: attribute access imports the provider module
on first use, so importing this package does not import implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
{type_lines}

_FACADE_EXPORTS = {{
{map_lines},
}}


def __getattr__(name: str):
    try:
        module_path = _FACADE_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {{__name__!r}} has no attribute {{name!r}}") from None
    from importlib import import_module

    export = getattr(import_module(module_path), name)
    globals()[name] = export
    return export


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [{all_line}]
'''


def _top_level_bindings(module: cst.Module) -> dict[str, int]:
    names: dict[str, int] = {}
    for statement in module.body:
        line = None
        if isinstance(statement, cst.SimpleStatementLine):
            line = statement
            smalls = statement.body
        else:
            smalls = ()
        for small in smalls:
            if isinstance(small, (cst.Import, cst.ImportFrom)):
                if isinstance(small, cst.Import):
                    for alias in small.names:
                        name = (
                            alias.asname.name.value
                            if alias.asname is not None
                            else import_target_name(alias).split(".")[0]
                        )
                        names.setdefault(name, 0)
                else:
                    if isinstance(small.names, cst.ImportStar):
                        names.setdefault("*", 0)
                        continue
                    for alias in small.names:
                        name = (
                            alias.asname.name.value
                            if alias.asname is not None
                            else import_target_name(alias)
                        )
                        names.setdefault(name, 0)
            elif isinstance(statement, (cst.FunctionDef, cst.ClassDef)):
                names.setdefault(statement.name.value, 0)
            elif isinstance(statement, cst.SimpleStatementLine):
                pass
        if isinstance(statement, (cst.FunctionDef, cst.ClassDef)):
            names.setdefault(statement.name.value, 0)
        if line is not None:
            for small in line.body:
                if isinstance(small, cst.Assign):
                    for target_name in assign_target_names(small):
                        names.setdefault(target_name, 0)
                elif isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name):
                    names.setdefault(small.target.value, 0)
    return names


def _existing_lazy_exports(parsed: ParsedPython) -> dict[str, str] | None:
    """Return the export map if the file is a tool-managed lazy facade."""
    text = parsed.source
    if MANAGED_MARKER not in text:
        return None
    module = parsed.module
    exports: dict[str, str] = {}
    for statement in module.body:
        if isinstance(statement, cst.SimpleStatementLine):
            for small in statement.body:
                if isinstance(small, cst.Assign):
                    if "_FACADE_EXPORTS" in assign_target_names(small) and isinstance(
                        small.value, cst.Dict
                    ):
                        for element in small.value.elements:
                            if not isinstance(element, cst.DictElement):
                                continue
                            key = element.key
                            value = element.value
                            if isinstance(key, cst.SimpleString) and isinstance(
                                value, cst.SimpleString
                            ):
                                exports[key.raw_value] = value.raw_value
    return exports


def apply_rule(rule: Rule, ctx: TransformContext) -> RuleOutcome:
    ws = ctx.workspace
    facade_path = str(rule.get("facade_path"))
    facade_module = str(rule.get("facade_module"))
    provider_module = str(rule.get("provider_module"))
    symbol = str(rule.get("symbol"))
    mode = str(rule.get("mode") or "lazy")
    outcome = RuleOutcome(
        rule_id=rule.rule_id,
        kind=rule.kind,
        stage=rule.stage,
        status="refused",
        expected_matches=rule.expected_matches,
    )

    try:
        ctx.writable_strict(facade_path)
    except Exception as exc:  # ToolError -> scope refusal
        outcome.diagnostics.append(
            Diagnostic("SCOPE-READ-ONLY", str(exc), facade_path, None, ERROR)
        )
        return outcome
    if not ws.exists(facade_path):
        if mode != "lazy":
            outcome.diagnostics.append(
                Diagnostic(
                    "FACADE-CREATE-EAGER",
                    "eager mode requires an existing facade file; lazy mode can create one",
                    facade_path,
                    None,
                    ERROR,
                )
            )
            return outcome
        content = _render_lazy(facade_module, [(symbol, provider_module)])
        ws.write(facade_path, content.encode("utf-8"))
        outcome.status = "applied"
        outcome.matches = 1
        outcome.changed_paths = [facade_path]
        return outcome

    data = ws.read(facade_path)
    encoding = "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        outcome.diagnostics.append(
            Diagnostic("FACADE-ENCODING", f"facade file is not UTF-8: {exc}", facade_path)
        )
        return outcome
    try:
        parsed = ParsedPython(facade_path, text, encoding)
    except Exception as exc:  # noqa: BLE001 - surfaced as refusal
        outcome.diagnostics.append(
            Diagnostic("FACADE-PARSE", f"facade file does not parse: {exc}", facade_path)
        )
        return outcome

    bindings = _top_level_bindings(parsed.module)
    if symbol in bindings:
        outcome.diagnostics.append(
            Diagnostic(
                "EXPORT-CONFLICT",
                f"symbol {symbol!r} is already bound at top level of {facade_path}",
                facade_path,
                None,
                ERROR,
            )
        )
        return outcome
    convert_pin = rule.get("convert_pin") or {}
    if mode == "lazy":
        existing = _existing_lazy_exports(parsed)
        if existing is None:
            # a non-managed file may be converted only when the manifest pins
            # the exact old content being replaced (an explicit replace rule)
            import hashlib

            pin_digest = str(convert_pin.get("content_sha256") or "")
            actual = hashlib.sha256(data).hexdigest()
            if not pin_digest:
                outcome.diagnostics.append(
                    Diagnostic(
                        "FACADE-NOT-MANAGED",
                        f"{facade_path} exists without the tool marker; converting it into "
                        "a lazy facade requires an explicit convert_pin {content_sha256} "
                        "nailing down the replaced content",
                        facade_path,
                        None,
                        ERROR,
                    )
                )
                return outcome
            if actual != pin_digest:
                outcome.diagnostics.append(
                    Diagnostic(
                        "CONVERT-PIN-MISMATCH",
                        f"convert_pin does not match the current content of "
                        f"{facade_path} ({actual[:12]}... != {pin_digest[:12]}...); "
                        "re-pin after review or restore the pinned file",
                        facade_path,
                        None,
                        ERROR,
                    )
                )
                return outcome
            existing = {}  # pinned conversion replaces the file wholesale
        if existing.get(symbol) == provider_module:
            outcome.status = "already_done"
            outcome.matches = 0
            return outcome
        if symbol in existing:
            outcome.diagnostics.append(
                Diagnostic(
                    "EXPORT-CONFLICT",
                    f"{symbol!r} already exported from {existing[symbol]!r}, refusing to "
                    f"re-export from {provider_module!r}",
                    facade_path,
                    None,
                    ERROR,
                )
            )
            return outcome
        merged = sorted({**existing, symbol: provider_module}.items())
        content = _render_lazy(facade_module, merged)
        ws.write(facade_path, content.encode("utf-8"))
        outcome.status = "applied"
        outcome.matches = 1
        outcome.changed_paths = [facade_path]
        return outcome

    # eager append into an existing file
    if any(alias_name == "*" for alias_name in bindings):
        outcome.diagnostics.append(
            Diagnostic(
                "EXPORT-CONFLICT",
                f"{facade_path} contains a star import; refusing to append eager exports",
                facade_path,
                None,
                ERROR,
            )
        )
        return outcome
    if f"from {provider_module} import" in text and f" {symbol}\n" in text:
        outcome.status = "already_done"
        outcome.matches = 0
        return outcome
    new_text = text
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += f"from {provider_module} import {symbol}\n"
    ws.write(facade_path, new_text.encode(encoding))
    outcome.status = "applied"
    outcome.matches = 1
    outcome.changed_paths = [facade_path]
    return outcome


__all__ = ["KINDS", "MANAGED_MARKER", "apply_rule"]
