"""Only cli/output writes results; lower layers never print (design §3, §6)."""

import ast
from pathlib import Path

import hyprial

SRC = Path(hyprial.__file__).resolve().parent
COMMANDS = SRC / "shell" / "impl" / "cli" / "commands"

# Direct output a command module must not perform: results go through
# CliResult/CliStream, everything else through cli/output channels.
_FORBIDDEN_CALLS = {
    "print",
    "typer.echo",
    "typer.secho",
    "typer.confirm",
    "typer.prompt",
    "click.echo",
    "sys.stdout.write",
    "sys.stderr.write",
    "Console",
    "rich.console.Console",
}

# (file relative to cli/commands, call) -> why it may stay.
ALLOWED = {
    ("transfer/move.py", "print"): (
        "transfer facts go to stderr through the injected print hook, so tests "
        "can observe that they precede the first mutation"
    ),
}

_LOWER_LAYERS = ("kernel", "identity", "daemon", "biz")
_PRESENTATION_MODULES = ("typer", "click", "rich")


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _forbidden_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in _FORBIDDEN_CALLS:
                found.append((node.lineno, name))
    return found


def test_command_modules_do_not_write_output_themselves():
    violations = []
    used = set()
    for path in sorted(COMMANDS.rglob("*.py")):
        relative = path.relative_to(COMMANDS).as_posix()
        for line, name in _forbidden_calls(path):
            if (relative, name) in ALLOWED:
                used.add((relative, name))
                continue
            violations.append(f"{relative}:{line} {name}(...)")
    assert violations == [], (
        "return a CliResult/CliStream, or use cli/output channels "
        "(progress/warn/notice/confirm):\n" + "\n".join(violations)
    )
    assert used == set(ALLOWED), f"stale allowlist entries: {set(ALLOWED) - used}"


def test_lower_layers_do_not_import_presentation_libraries():
    violations = []
    for layer in _LOWER_LAYERS:
        for path in sorted((SRC / layer).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    if module.split(".")[0] in _PRESENTATION_MODULES:
                        violations.append(f"{path.relative_to(SRC)}:{node.lineno} {module}")
    assert violations == [], "\n".join(violations)


def test_the_scan_covers_the_link_group_and_the_read_command():
    """A path move must not silently take the new files out of the scan."""

    scanned = {path.relative_to(COMMANDS).as_posix() for path in COMMANDS.rglob("*.py")}

    assert {"link/commands.py", "link/render.py", "message/read.py"} <= scanned


def test_the_scan_sees_a_violation():
    planted = ast.parse("import typer\ndef f():\n    typer.echo('x')\n    print('y')\n")
    names = [_dotted(node.func) for node in ast.walk(planted) if isinstance(node, ast.Call)]
    assert {"typer.echo", "print"} <= set(names) <= _FORBIDDEN_CALLS | {"f"}
