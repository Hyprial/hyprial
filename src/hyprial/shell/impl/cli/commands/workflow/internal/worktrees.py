"""``hyprial worktrees`` git worktree inventory."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from hyprial.kernel import ipc_errors
import os
import shlex
import subprocess
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _overview_age, _overview_table
_WORKTREE_GIT_TIMEOUT_SECONDS = 20


_WORKTREE_STATES = ("missing", "dirty", "unpushed", "merged", "pushed", "error")


def _run_worktree_git(
    arguments: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run one bounded, read-only git query for ``hyprial worktrees``.

    Kept as a small seam so tests can prove that one worktree's git failure
    does not abort the remaining scan.
    """

    return subprocess.run(
        ["git", "--no-optional-locks", "-C", os.fspath(cwd), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=_WORKTREE_GIT_TIMEOUT_SECONDS,
    )


def _worktree_git_output(arguments: Sequence[str], *, cwd: Path) -> str:
    services = get_services()
    try:
        completed = services._run_worktree_git(arguments, cwd=cwd)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"git {' '.join(arguments)} timed out after "
            f"{_WORKTREE_GIT_TIMEOUT_SECONDS}s"
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"could not run git {' '.join(arguments)}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"git {' '.join(arguments)} exited {completed.returncode}{suffix}"
        )
    return completed.stdout


def _resolve_worktree_repository(value: Path) -> tuple[Path, Path]:
    services = get_services()
    requested = value.expanduser()
    probe = requested.parent if requested.is_file() else requested
    try:
        raw_common = _worktree_git_output(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=probe,
        ).strip()
    except (RuntimeError, NotADirectoryError) as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"{value} is not a git repository; pass a repository path",
        ) from error
    common_dir = Path(raw_common)
    if not common_dir.is_absolute():
        common_dir = probe / common_dir
    common_dir = common_dir.resolve()
    repository = common_dir.parent if common_dir.name == ".git" else common_dir
    return repository, common_dir


def _parse_worktree_porcelain(output: str) -> list[JsonObject]:
    records: list[JsonObject] = []
    current: JsonObject = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"detached", "bare"}:
            current[key] = True
        else:
            current[key] = value
    return records


def _worktree_ref_exists(repository: Path, ref: str) -> bool:
    services = get_services()
    completed = services._run_worktree_git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repository
    )
    if completed.returncode in {1, 128}:
        return False
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise services.CliError(
            "GIT_ERROR",
            f"could not check base {ref!r} in {repository}: "
            f"{detail or f'git exited {completed.returncode}'}",
        )
    return True


def _worktree_bases(repository: Path, requested: str | None) -> list[str]:
    """The refs a worktree counts as merged into.

    Without --base, every default that exists: origin/HEAD usually names
    main while work lands on dev, so checking only the first match would call
    nearly every merged branch unmerged.  Local ``main`` only stands in when
    the repository has none of the remote ones.
    """
    services = get_services()

    if requested is not None:
        if not _worktree_ref_exists(repository, requested):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"base ref {requested!r} does not exist in repository {repository}",
            )
        return [requested]
    remote = [
        ref
        for ref in ("refs/remotes/origin/HEAD", "origin/dev", "origin/main")
        if _worktree_ref_exists(repository, ref)
    ]
    if remote:
        return remote
    return ["main"] if _worktree_ref_exists(repository, "main") else []


def _dirty_paths(status: str) -> list[str]:
    """Working-tree paths from ``git status --porcelain=v1 -z``.

    ``-z`` prints paths raw, so there is no C-quoting (octal UTF-8 bytes) to
    decode.  A rename or copy entry is followed by its original path as an
    extra field, which is skipped.
    """

    fields = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            index += 1
    return paths


def _dirty_activity_at_ms(worktree: Path, dirty_paths: list[str]) -> int | None:
    newest: int | None = None
    for relative in dirty_paths[:200]:
        try:
            modified = (worktree / relative).lstat().st_mtime_ns // 1_000_000
        except OSError:
            continue
        newest = modified if newest is None else max(newest, modified)
    return newest


def _git_count(arguments: Sequence[str], *, cwd: Path) -> int:
    raw = _worktree_git_output(arguments, cwd=cwd).strip()
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(
            f"git {' '.join(arguments)} returned a non-integer count: {raw!r}"
        ) from error


def _scan_worktree(
    record: JsonObject, *, repository: Path, bases: list[str]
) -> JsonObject:
    services = get_services()
    path = Path(str(record.get("worktree", "")))
    head = str(record.get("HEAD", ""))
    raw_branch = record.get("branch")
    branch = (
        str(raw_branch).removeprefix("refs/heads/")
        if isinstance(raw_branch, str) and not record.get("detached")
        else None
    )
    # git marks a worktree prunable when its directory or its gitdir link is
    # gone; a directory can survive its link, and git can't read it then.
    missing = "prunable" in record or not path.is_dir()
    row: JsonObject = {
        "path": os.fspath(path),
        "branch": branch,
        "head": head,
        "missing": missing,
        "dirtyFiles": 0 if missing else None,
        "upstream": None,
        "upstreamGone": False,
        "aheadOfUpstream": None,
        "unpushedCommits": None,
        "mergedIntoBase": None,
        "lastCommitAtMs": None,
        "lastActivityAtMs": None,
    }
    try:
        if not missing:
            dirty_paths = _dirty_paths(
                _worktree_git_output(
                    ["status", "--porcelain=v1", "-z", "--untracked-files=normal"],
                    cwd=path,
                )
            )
            row["dirtyFiles"] = len(dirty_paths)
        else:
            dirty_paths = []

        if isinstance(raw_branch, str) and branch is not None:
            upstream, _, track = (
                _worktree_git_output(
                    [
                        "for-each-ref",
                        "--format=%(upstream:short)%00%(upstream:track)",
                        raw_branch,
                    ],
                    cwd=repository,
                )
                .strip()
                .partition("\0")
            )
            row["upstream"] = upstream or None
            # A merged branch's remote is often deleted; that is not an error,
            # and unpushedCommits below still says whether work would be lost.
            row["upstreamGone"] = track == "[gone]"
            if upstream and not row["upstreamGone"]:
                row["aheadOfUpstream"] = _git_count(
                    ["rev-list", "--count", f"{upstream}..{head}"], cwd=repository
                )

        row["unpushedCommits"] = _git_count(
            ["rev-list", "--count", head, "--not", "--remotes"], cwd=repository
        )
        for base in bases:
            completed = services._run_worktree_git(
                ["merge-base", "--is-ancestor", head, base], cwd=repository
            )
            if completed.returncode not in {0, 1}:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(
                    "git merge-base --is-ancestor "
                    f"{head} {base} exited {completed.returncode}"
                    f"{f': {detail}' if detail else ''}"
                )
            row["mergedIntoBase"] = completed.returncode == 0
            if row["mergedIntoBase"]:
                break

        committed_seconds = _git_count(
            ["show", "-s", "--format=%ct", head], cwd=repository
        )
        committed_ms = committed_seconds * 1000
        dirty_ms = _dirty_activity_at_ms(path, dirty_paths) if not missing else None
        row["lastCommitAtMs"] = committed_ms
        row["lastActivityAtMs"] = max(
            committed_ms, dirty_ms if dirty_ms is not None else committed_ms
        )

        dirty_files = int(row["dirtyFiles"] or 0)
        unpushed = int(row["unpushedCommits"] or 0)
        leftover = missing or dirty_files > 0 or unpushed > 0
        state = (
            "missing"
            if missing
            else "dirty"
            if dirty_files > 0
            else "unpushed"
            if unpushed > 0
            else "merged"
            if row["mergedIntoBase"] is True
            else "pushed"
        )
        row.update({"state": state, "leftover": leftover})
    except Exception as error:  # noqa: BLE001 - one bad worktree must not abort peers
        dirty_files = row.get("dirtyFiles")
        unpushed = row.get("unpushedCommits")
        row.update(
            {
                "state": "error",
                "leftover": (
                    missing
                    or (type(dirty_files) is int and dirty_files > 0)
                    or (type(unpushed) is int and unpushed > 0)
                ),
                "error": str(error),
            }
        )
    return row


def _scan_worktree_repository(
    repository: Path, *, requested_base: str | None
) -> JsonObject:
    services = get_services()
    bases = _worktree_bases(repository, requested_base)
    try:
        records = _parse_worktree_porcelain(
            _worktree_git_output(["worktree", "list", "--porcelain"], cwd=repository)
        )
    except RuntimeError as error:
        raise services.CliError(
            "GIT_ERROR", f"could not list worktrees for {repository}: {error}"
        ) from error

    def scan(record: JsonObject) -> JsonObject:
        return _scan_worktree(record, repository=repository, bases=bases)

    # Hundreds of independent worktrees are common on development hosts. Keep
    # process pressure bounded while avoiding a serial status/rev-list chain.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(records)))) as executor:
        rows = list(executor.map(scan, records))
    summary = {
        state: sum(row["state"] == state for row in rows) for state in _WORKTREE_STATES
    }
    summary.update(
        {
            "leftover": sum(row["leftover"] is True for row in rows),
            "total": len(rows),
        }
    )
    return {
        "repo": os.fspath(repository),
        "base": bases[0] if bases else None,
        "bases": bases,
        "worktrees": rows,
        "summary": summary,
    }


def _display_worktree_path(value: object) -> str:
    path = Path(str(value))
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return os.fspath(path)
    return "~" if not relative.parts else os.fspath(Path("~") / relative)


def _worktree_remove_command(path: str, *, width: int = 118) -> str:
    """Return one copyable shell command, continued when its path is long."""

    prefix = "git worktree remove "
    command = prefix + shlex.quote(path)
    if len(command) <= width:
        return command

    lines: list[str] = []
    remaining = path
    available = width - len(prefix) - 1
    first = True
    while remaining:
        size = min(len(remaining), available)
        while size > 1 and len(shlex.quote(remaining[:size])) > available:
            size -= 1
        quoted = shlex.quote(remaining[:size])
        remaining = remaining[size:]
        line = (prefix if first else "") + quoted
        lines.append(line + ("\\" if remaining else ""))
        first = False
        available = width - 1
    return "\n".join(lines)


def _render_worktree_repository(
    result: JsonObject, *, show_all: bool, now_ms: int | None = None
) -> str:
    services = get_services()
    summary = result["summary"]
    assert isinstance(summary, dict)
    total = int(summary["total"])
    leftovers = int(summary["leftover"])
    bases = result.get("bases")
    base_text = (
        ", ".join(str(ref).removeprefix("refs/remotes/") for ref in bases)
        if isinstance(bases, list) and bases
        else "none found"
    )
    title = (
        f"hyprial worktrees   {result['repo']}   {leftovers} leftovers of "
        f"{total} worktrees (base {base_text})"
    )
    if leftovers == 0 and not show_all:
        return title

    rows = result["worktrees"]
    assert isinstance(rows, list)
    visible = rows if show_all else [row for row in rows if row.get("leftover") is True]
    visible.sort(
        key=lambda row: (
            row.get("lastActivityAtMs")
            if type(row.get("lastActivityAtMs")) is int
            else -1,
            str(row.get("path", "")),
        )
    )
    observed_at = services.time.time_ns() // 1_000_000 if now_ms is None else now_ms
    cells = [
        [
            str(row.get("state", "error")),
            _display_worktree_path(row.get("path", "")),
            str(row.get("branch") or "-"),
            str(row["dirtyFiles"]) if type(row.get("dirtyFiles")) is int else "-",
            str(row["unpushedCommits"])
            if type(row.get("unpushedCommits")) is int
            else "-",
            _overview_age(row.get("lastActivityAtMs"), now_ms=observed_at),
            "yes"
            if row.get("mergedIntoBase") is True
            else "no"
            if row.get("mergedIntoBase") is False
            else "-",
        ]
        for row in visible
    ]
    lines = [
        _overview_table(
            title,
            ["STATE", "PATH", "BRANCH", "DIRTY", "UNPUSHED", "ACTIVITY", "MERGED"],
            cells,
            truncate_column=1,
        )
    ]
    counts = ", ".join(f"{state} {summary.get(state, 0)}" for state in _WORKTREE_STATES)
    lines.append(f"Summary: {counts}")

    next_steps: list[str] = []
    if any(row.get("state") == "missing" for row in rows):
        next_steps.append("git worktree prune")
    removable = [
        row
        for row in rows
        if row.get("state") == "merged"
        and row.get("dirtyFiles") == 0
        and row.get("unpushedCommits") == 0
        and Path(str(row.get("path"))).resolve() != Path(str(result["repo"])).resolve()
    ]
    next_steps.extend(
        _worktree_remove_command(str(row["path"])) for row in removable[:10]
    )
    if len(removable) > 10:
        next_steps.append(f"… and {len(removable) - 10} more (use --json)")
    if any(row.get("state") == "dirty" for row in rows):
        next_steps.append("Inspect dirty worktrees; commit changes before removal.")
    if any(row.get("state") == "unpushed" for row in rows):
        next_steps.append("Inspect unpushed worktrees; push commits before removal.")
    if any(row.get("state") == "error" for row in rows):
        next_steps.append("Inspect error rows before making any worktree changes.")
    if next_steps:
        lines.extend(["Next steps:", *(f"  {step}" for step in next_steps)])
    return "\n".join(lines)


@app.command("worktrees")
def worktrees_command(
    repos: list[Path] | None = typer.Argument(
        None, metavar="[REPO]...", help="Repository paths (default: current directory)."
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Merged means reachable from this ref (default: any of origin/HEAD, origin/dev, origin/main that exist).",
    ),
    show_all: bool = typer.Option(
        False, "--all", help="Show every worktree in human output."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Inspect local git worktrees for read-only leftover-work signals."""
    services = get_services()

    def operation() -> Any:
        repositories: list[Path] = []
        seen: set[Path] = set()
        for value in repos or [Path.cwd()]:
            repository, common_dir = _resolve_worktree_repository(value)
            if common_dir in seen:
                continue
            seen.add(common_dir)
            repositories.append(repository)
        results = [
            _scan_worktree_repository(repository, requested_base=base)
            for repository in repositories
        ]
        return CliResult(
            {"ok": True, "repos": results},
            render=lambda data: "\n\n".join(
                _render_worktree_repository(result, show_all=show_all) for result in data["repos"]
            ),
        )

    services._execute(operation, json_output=json_output, allow_missing_home=True)
