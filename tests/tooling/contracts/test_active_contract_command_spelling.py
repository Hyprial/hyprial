"""Active contract surfaces may only name commands this CLI registers.

``contract/`` is not all the same age.  Most of it instructs today's operator
and today's E2E runners, and those strings must name registered commands --
E2E-026's assertion text said ``hyprial read`` and the send-links runner's
failure message repeated it long after R12 moved the reader under ``message``.
A smaller, declared set is old-executable or negative material, where the old
spelling is the subject rather than an instruction: E2E-010 drives the
*installed old tag's* own ``hyprial upgrade`` and ``hyprial mcp ...``, and
``cli-blackbox`` asserts the retired ``hyprial ps`` is gone.

So this scans the whole tree and excludes that history by name with a reason,
instead of skipping ``contract/`` wholesale.  Every exclusion is asserted to
still be necessary, so a cleaned-up fixture cannot leave a silent blind spot
behind, and the detector is pinned both ways: it rejects ``hyprial read`` and
accepts the registered grouped spelling.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
import json
from pathlib import Path
import re

from typer.main import get_command

# The populated root: importing the CLI entry module registers every command
# group on the shared Typer app. Importing only commands.common.root gave a bare
# app whose commands existed only if an earlier test in the same process had
# imported the entry (it passed in full runs, failed alone and under layered
# selection; first seen on #2552 shard 3).
from hyprial.shell.impl.cli.runtime.entry import app

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "contract"
SCENARIOS = CONTRACT / "e2e" / "scenarios.json"

#: Top-level spellings the grouped command tree retired (R5, R7, R12 and the
#: folds named in docs/expected-commands.md).  Each must stay absent from the
#: registered root -- asserted below, so this list cannot quietly go stale.
RETIRED_TOP_LEVEL = (
    "ack",
    "autoupdate",
    "dispatch",
    "down",
    "grants-check",
    "grants-visibility",
    "hosts",
    "install",
    "keep",
    "mcp",
    "network",
    "pac",
    "ps",
    "query",
    "read",
    "reply",
    "restart",
    "send",
    "spaces",
    "start",
    "targets",
    "top",
    "transfer",
    "transfer-runtime",
    "upgrade",
    "work",
    "worktrees",
)

#: ``hyprial <retired>`` as a whole command word.  A group prefix (``hyprial
#: message read``) does not match, which is the point.
RETIRED = re.compile(
    r"\bhyprial\s+(?:" + "|".join(re.escape(name) for name in RETIRED_TOP_LEVEL) + r")\b"
)

#: Reviewed history: old-executable material and negative assertions.  Every
#: entry is asserted below to still contain a retired spelling, so the
#: exclusion cannot outlive the fixture it was written for.
EXCLUDED_FILES = {
    "contract/cli-blackbox/run.py": (
        "negative assertions: the retired `hyprial ps` must be absent, so the "
        "spelling is the subject, not guidance"
    ),
    "contract/e2e/10-upgrade-continuity.md": (
        "E2E-010 exercises the installed old tag, whose own CLI is `hyprial upgrade`"
    ),
    "contract/e2e/run-upgrade-continuity.py": (
        "E2E-010 drives the installed old tag's own `hyprial upgrade`"
    ),
    "contract/e2e/run-upgrade-cross-minor-limitation.py": (
        "E2E-010 family: asserts on the old executable's own failure text"
    ),
    "contract/e2e/run-inprocess-autoupdate.py": (
        '`git config user.name "hyprial autoupdate e2e"` is a git identity string'
    ),
}

EXCLUDED_DIRECTORIES = {
    "contract/e2e/_upgrade_continuity": (
        "E2E-010 fixtures: the installed old tag answers `hyprial upgrade` and "
        "`hyprial mcp claude-channel` through its own interpreter"
    ),
}

#: Structured entries excluded by scenario id (their assertions quote the old
#: executable on purpose).
EXCLUDED_SCENARIOS = {
    "E2E-010": "old-executable upgrade continuity: the assertion is the old tag's CLI",
}

SCANNED_SUFFIXES = frozenset({".md", ".json", ".py", ".sh", ".yaml", ".yml"})


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _in_excluded_directory(relative: str) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in EXCLUDED_DIRECTORIES
    )


def _scanned_files() -> Iterator[Path]:
    for path in sorted(CONTRACT.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        if path == SCENARIOS:
            # Structured pass below: per-scenario assertions, so an excluded
            # scenario id is skipped by entry rather than by file.
            continue
        relative = _relative(path)
        if relative in EXCLUDED_FILES or _in_excluded_directory(relative):
            continue
        yield path


def _python_strings(tree: ast.AST) -> Iterator[tuple[int, str]]:
    joined_values = {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for value in node.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield (
                node.lineno,
                "".join(
                    value.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else "<value>"
                    for value in node.values
                ),
            )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in joined_values
        ):
            yield node.lineno, node.value


def _json_strings(value: object, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _json_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_strings(child, f"{path}[{index}]")


def _strings(path: Path) -> Iterator[tuple[str, str]]:
    relative = _relative(path)
    if path.suffix == ".py":
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, text in _python_strings(tree):
            yield f"{relative}:{line}", text
    elif path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        for location, text in _json_strings(payload):
            yield f"{relative}{location[1:]}", text
    else:
        for number, text in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            yield f"{relative}:{number}", text


def _scenario_entries() -> list[dict[str, object]]:
    payload = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return payload["scenarios"]


def _assertions(scenario: dict[str, object]) -> list[str]:
    assertions = scenario.get("assertions", [])
    assert isinstance(assertions, list)
    return [text for text in assertions if isinstance(text, str)]


def test_the_declared_surface_is_contract_not_a_convenient_subset() -> None:
    scanned = {_relative(path) for path in _scanned_files()}

    assert "contract/e2e/_send_links/phases.py" in scanned
    assert "contract/wire/send-links.md" in scanned
    # scenarios.json is scanned structurally, one scenario entry at a time.
    scenario_ids = {scenario.get("id") for scenario in _scenario_entries()}
    assert "E2E-026" in scenario_ids - set(EXCLUDED_SCENARIOS)
    assert "E2E-010" in scenario_ids & set(EXCLUDED_SCENARIOS)
    for excluded in (*EXCLUDED_FILES, *EXCLUDED_DIRECTORIES):
        assert (ROOT / excluded).exists(), f"stale exclusion: {excluded}"


def test_retired_top_level_names_are_absent_from_the_registered_root() -> None:
    """The list above describes the real CLI, and the grouped spelling exists."""

    root = get_command(app)
    assert not set(RETIRED_TOP_LEVEL) & set(root.commands), (
        "a declared-retired name is registered at the top level again"
    )
    message = root.commands["message"]
    assert hasattr(message, "commands")
    assert "read" in message.commands


def test_active_contract_surfaces_name_only_registered_commands() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        for location, text in _strings(path):
            match = RETIRED.search(text)
            if match is not None:
                offenders.append(f"{location}: {match.group(0)}")

    for scenario in _scenario_entries():
        if scenario.get("id") in EXCLUDED_SCENARIOS:
            continue
        for index, text in enumerate(_assertions(scenario)):
            match = RETIRED.search(text)
            if match is not None:
                offenders.append(
                    f"scenarios.json {scenario.get('id')} assertions[{index}]: "
                    f"{match.group(0)}"
                )

    assert offenders == [], (
        "active contract surfaces name retired top-level commands; use the "
        "registered grouped spelling (for example `hyprial message read`):\n"
        + "\n".join(offenders)
    )


def test_e2e_026_names_the_registered_body_reader() -> None:
    """The corrected surface is really checked, not dropped from the scan."""

    entry = next(item for item in _scenario_entries() if item.get("id") == "E2E-026")
    joined = "\n".join(_assertions(entry))
    assert "hyprial message read" in joined
    assert "hyprial read" not in joined

    runner = (CONTRACT / "e2e" / "_send_links" / "phases.py").read_text("utf-8")
    assert "hyprial message read did not return the body" in runner


def test_declared_exclusions_are_still_necessary() -> None:
    """A blind spot must not outlive the old-executable material it covers."""

    def mentions_retired(path: Path) -> bool:
        return any(RETIRED.search(text) for _, text in _strings(path))

    for relative in EXCLUDED_FILES:
        assert mentions_retired(ROOT / relative), (
            f"{relative} no longer names a retired command; remove its exclusion"
        )

    for prefix, _reason in EXCLUDED_DIRECTORIES.items():
        files = [
            path
            for path in (ROOT / prefix).rglob("*")
            if path.is_file()
            and path.suffix in SCANNED_SUFFIXES
            and "__pycache__" not in path.parts
        ]
        assert files, f"{prefix} has no scannable file; remove its exclusion"
        assert any(mentions_retired(path) for path in files), (
            f"{prefix} no longer names a retired command; remove its exclusion"
        )

    by_id = {scenario.get("id"): scenario for scenario in _scenario_entries()}
    for scenario_id in EXCLUDED_SCENARIOS:
        entry = by_id.get(scenario_id)
        assert entry is not None, f"stale scenario exclusion: {scenario_id}"
        assert any(RETIRED.search(text) for text in _assertions(entry)), (
            f"{scenario_id} no longer names a retired command; remove its exclusion"
        )


def test_the_check_rejects_retired_spellings() -> None:
    for text in (
        "pending rows expose no body; hyprial read fetches it",
        "`hyprial ps --json` retains suppressed connectors",
        "run `hyprial upgrade status` for the schedule",
        "hyprial start claude --headless",
    ):
        assert RETIRED.search(text) is not None, text


def test_the_check_accepts_the_registered_grouped_spelling() -> None:
    for text in (
        "`hyprial message read <id> --from <actor> --json` returns the body",
        "`hyprial agent list --json` retains suppressed connectors",
        "`hyprial daemon upgrade status` shows the schedule",
        "`hyprial agent start claude --headless`",
    ):
        assert RETIRED.search(text) is None, text
