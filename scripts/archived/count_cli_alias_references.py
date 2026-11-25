#!/usr/bin/env python3
"""Count Phase 1 legacy CLI spellings in tracked skills, prompts, and docs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Alias:
    disposition: str
    command: str


# Target-state candidates. Only paths still registered in the current Typer
# tree are reported; already-absent paths are listed separately and contribute
# no counts.
CANDIDATES = (
    Alias("retire", "channel join"),
    Alias("retire", "channel part"),
    Alias("retire", "channel list"),
    Alias("move-down", "lark-auth check"),
    Alias("move-down", "lark-auth complete"),
    Alias("move-down", "trajectory"),
    Alias("move-down", "outbox list"),
    Alias("move-down", "outbox prune"),
    Alias("move-down", "autoupdate install"),
    Alias("move-down", "autoupdate uninstall"),
    Alias("move-down", "autoupdate status"),
    Alias("hide/internalize", "mcp claude-channel"),
    Alias("hide/internalize", "mcp agent-channel"),
    Alias("hide/internalize", "transfer-precheck"),
    Alias("hide/internalize", "transfer-receive"),
    Alias("hide/internalize", "transfer-session-path"),
)

_CODE_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_HYPRIAL_FRAGMENT = re.compile(
    r"\bhyprial[ \t]+[a-z0-9-]+(?:[ \t]+[a-z0-9|/<>.-]+){0,5}", re.IGNORECASE
)
_HYPRIAL_VARIABLE_FRAGMENT = re.compile(
    r"\b[A-Z0-9_]*HYPRIAL[\"']?[ \t]+[a-z0-9-]+"
    r"(?:[ \t]+[a-z0-9|/<>.-]+){0,5}"
)
_WORD = re.compile(r"[a-z][a-z0-9-]*", re.IGNORECASE)


def _registered_command_paths() -> set[str]:
    from typer._click import Context
    from typer.main import get_command

    from hyprial.shell.impl.cli.runtime.entry import app

    root = get_command(app)
    paths: set[str] = set()

    def walk(command: object, context: Context, prefix: tuple[str, ...]) -> None:
        list_commands = getattr(command, "list_commands", None)
        if not callable(list_commands):
            return
        for name in list_commands(context):
            child = command.get_command(context, name)  # type: ignore[attr-defined]
            if child is None:
                continue
            path = (*prefix, name)
            paths.add(" ".join(path))
            walk(child, Context(child, info_name=name, parent=context), path)

    walk(root, Context(root, info_name="hyprial"), ())
    return paths


def _category(path: Path) -> str | None:
    lowered_parts = tuple(part.lower() for part in path.parts)
    if path.name.lower() == "skill.md" or "skills" in lowered_parts:
        return "skill"
    if (
        any("prompt" in part for part in lowered_parts)
        or path.name.lower() in {"agents.md", "claude.md"}
    ):
        return "prompt"
    if path.suffix.lower() in {".md", ".markdown", ".rst", ".txt", ".adoc"}:
        return "docs"
    return None


def _tracked_documents(root: Path) -> tuple[tuple[Path, str], ...]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    documents: list[tuple[Path, str]] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        category = _category(relative)
        if category is not None:
            documents.append((relative, category))
    return tuple(documents)


def _aliases_in_fragment(fragment: str, aliases: tuple[Alias, ...]) -> set[str]:
    candidate = fragment.strip().lower()
    explicit_hyprial = False
    literal_prefix = re.match(r"^hyprial\s+", candidate)
    variable_prefix = re.match(r"^[a-z0-9_]*hyprial[\"']?\s+", candidate)
    prefix = literal_prefix or variable_prefix
    if prefix is not None:
        explicit_hyprial = True
        candidate = candidate[prefix.end() :]

    words = [word.lower() for word in _WORD.findall(candidate)]
    if not words:
        return set()

    found: set[str] = set()
    for alias in aliases:
        command = alias.command.split()
        if words[0] != command[0]:
            continue
        if len(command) == 1:
            # Bare top-level words such as ``trajectory.schema.json`` are not
            # CLI references. Without a hyprial prefix, accept only the exact
            # spelling or argument/option notation used in command docs.
            if explicit_hyprial or re.match(
                rf"^{re.escape(command[0])}(?:$|\s+(?:<|\[|--|\*))", candidate
            ):
                found.add(alias.command)
            continue
        # A bare code span must use whitespace between the command root and
        # its subcommand. This excludes protocol/member names like
        # ``outbox.list`` while still expanding ``check|complete`` shorthand.
        if not re.match(rf"^{re.escape(command[0])}\s+", candidate):
            continue
        if all(token in words[1:] for token in command[1:]):
            found.add(alias.command)
    return found


def _reference_mentions(text: str, aliases: tuple[Alias, ...]) -> list[set[str]]:
    mentions: list[set[str]] = []
    masked = list(text)
    for match in _CODE_SPAN.finditer(text):
        found = _aliases_in_fragment(match.group(1), aliases)
        if found:
            mentions.append(found)
        for index in range(match.start(), match.end()):
            masked[index] = " "

    remaining = "".join(masked)
    for pattern in (_HYPRIAL_FRAGMENT, _HYPRIAL_VARIABLE_FRAGMENT):
        for match in pattern.finditer(remaining):
            found = _aliases_in_fragment(match.group(0), aliases)
            if found:
                mentions.append(found)
    return mentions


def _count(root: Path, aliases: tuple[Alias, ...]) -> list[dict[str, object]]:
    counts = {
        alias.command: {"skill": 0, "prompt": 0, "docs": 0}
        for alias in aliases
    }
    files = {
        alias.command: {"skill": set(), "prompt": set(), "docs": set()}
        for alias in aliases
    }
    for relative, category in _tracked_documents(root):
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for mention in _reference_mentions(text, aliases):
            for command in mention:
                counts[command][category] += 1
                files[command][category].add(relative.as_posix())

    rows: list[dict[str, object]] = []
    for alias in aliases:
        per_category = counts[alias.command]
        rows.append(
            {
                "disposition": alias.disposition,
                "command": alias.command,
                "counts": {
                    **per_category,
                    "total": sum(per_category.values()),
                },
                "files": {
                    category: sorted(paths)
                    for category, paths in files[alias.command].items()
                },
            }
        )
    return rows


def _render_table(rows: list[dict[str, object]], not_registered: list[str]) -> str:
    lines = [
        "CLI alias references (tracked skill/prompt/docs files; occurrence counts)",
        "",
        "| Disposition | Old command | Skill | Prompt | Docs | Total |",
        "|---|---|---:|---:|---:|---:|",
    ]
    totals = {"skill": 0, "prompt": 0, "docs": 0, "total": 0}
    for row in rows:
        counts = row["counts"]
        assert isinstance(counts, dict)
        for category in totals:
            totals[category] += int(counts[category])
        lines.append(
            f"| {row['disposition']} | `hyprial {row['command']}` | "
            f"{counts['skill']} | {counts['prompt']} | {counts['docs']} | "
            f"{counts['total']} |"
        )
    lines.append(
        f"| **TOTAL** |  | **{totals['skill']}** | **{totals['prompt']}** | "
        f"**{totals['docs']}** | **{totals['total']}** |"
    )
    lines.extend(
        [
            "",
            "Not registered on this tree (excluded): "
            + (", ".join(f"`hyprial {path}`" for path in not_registered) or "none"),
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Git checkout to scan.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable counts.")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    # App mounts are import-time registrations. Use an empty home so the
    # source-tree alias set cannot be changed by locally installed apps.
    with tempfile.TemporaryDirectory(prefix="hyprial-alias-count-") as raw:
        home = Path(raw) / "home"
        state = Path(raw) / "state"
        home.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        os.environ["HYPRIAL_HOME"] = str(home)
        os.environ["HARNESS_STATE_DIR"] = str(state)
        registered = _registered_command_paths()

    aliases = tuple(alias for alias in CANDIDATES if alias.command in registered)
    not_registered = [
        alias.command for alias in CANDIDATES if alias.command not in registered
    ]
    rows = _count(root, aliases)
    if args.json:
        print(
            json.dumps(
                {"aliases": rows, "notRegistered": not_registered},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        print(_render_table(rows, not_registered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
