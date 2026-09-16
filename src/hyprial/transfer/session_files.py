"""Harness session-file locators for worker transfer (P0 cold migration).

A persisted ``sessionRef`` (#190) is only a pointer: the object it points at
is a harness-private, machine-local file.  This module is the ONLY consumer
of those private layouts — pi's ``sessions/--<encoded-cwd>--/`` directories,
codex's ``sessions/YYYY/MM/DD/rollout-*.jsonl`` rollouts, claude's
``projects/<encoded-cwd>/<id>.jsonl``.  The layouts are not our contract:
each locator fails loudly (never guesses) so a harness version that changes
its layout surfaces as a transfer error instead of a silent context loss.

Verified facts the rules rest on:

- **claude**: ``--resume <id>`` searches globally and tolerates a cwd change
  (verified live against claude 2.1.235, Q1 experiment 2026-08-21: resume
  from a different cwd found the file under the original project's encoded
  dir and continued it; moving the file to the new cwd's encoded dir also
  works).  Target placement still uses the standard encoding.
- **pi**: ``--session-id <id>`` only searches the CURRENT cwd's encoded
  session dir (``findLocalSessionByExactId`` → ``SessionManager.list(cwd,
  sessionDir)`` in pi's ``dist/main.js``; a miss warns and creates a fresh
  session with that id).  The file MUST land under the target cwd's encoded
  dir or the resume silently cold-starts.
- **codex**: rollouts live under ``$CODEX_HOME/sessions/YYYY/MM/DD/``; the
  date path is preserved verbatim on the target so the app-server's rollout
  scan finds the thread unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Harnesses whose sessions P0 can move.  dsh is excluded by design (#190:
#: its resume is refused in code); lark adapters are not workers.
TRANSFERABLE_HARNESSES = frozenset({"pi", "codex", "claude"})

#: claude encodes a project dir by mapping every non-alphanumeric character
#: to '-' (observed: /private/tmp/hyprial-q1-a → -private-tmp-hyprial-q1-a).
_CLAUDE_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

#: pi encodes a cwd as ``--`` + path with the leading separator stripped and
#: every remaining `/`, `\\`, `:` mapped to '-' + ``--`` (pi's
#: getDefaultSessionDirPath; matches the observed ``--private-tmp-…--``
#: directories).
_PI_UNSAFE = re.compile(r"[/\\:]")

_HEADER_SCAN_BYTES = 1024 * 1024


class SessionFileError(RuntimeError):
    """Base class for session-file location failures (always fail-loud)."""


class SessionFileNotFound(SessionFileError):
    """No session file for the ref — a transfer would cold-start silently."""


class SessionFileAmbiguous(SessionFileError):
    """More than one candidate — never guess which file holds the context."""


def _real_cwd(cwd: str) -> str:
    """Resolve the way the harnesses do before encoding a project dir.

    Both pi (``resolvePath``) and claude encode the REAL path: macOS maps
    ``/tmp`` to ``/private/tmp``, and a logical path encodes to a directory
    neither harness ever looks in (caught by the docker verification: the
    locator searched ``--tmp-…--`` while pi wrote ``--private-tmp-…--``).
    """

    return str(Path(cwd).expanduser().resolve())


def claude_project_dir_name(cwd: str) -> str:
    """The ``projects/`` subdirectory claude uses for one working directory."""

    return _CLAUDE_NON_ALNUM.sub("-", _real_cwd(cwd))


def claude_session_file(config_home: Path, cwd: str, session_id: str) -> Path:
    """Locate a claude session transcript, encoded dir first, then global.

    The global fallback mirrors claude's own resume behaviour (Q1): a session
    whose cwd moved since the last turn sits under the OLD encoded dir, and
    session ids are uuids, so a cross-directory match is unambiguous.
    """

    projects = Path(config_home) / "projects"
    direct = projects / claude_project_dir_name(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    matches = sorted(projects.glob(f"*/{session_id}.jsonl")) if projects.is_dir() else []
    if not matches:
        raise SessionFileNotFound(
            f"no claude session file for {session_id} under {projects}"
        )
    if len(matches) > 1:
        raise SessionFileAmbiguous(
            f"claude session {session_id} found in multiple project dirs: "
            + ", ".join(str(match) for match in matches)
        )
    return matches[0]


def claude_session_target(config_home: Path, cwd: str, session_id: str) -> Path:
    """Where the transcript belongs on the target for one (new) cwd."""

    return (
        Path(config_home)
        / "projects"
        / claude_project_dir_name(cwd)
        / f"{session_id}.jsonl"
    )


def pi_session_dir_name(cwd: str) -> str:
    """pi's per-project session directory name (``--<encoded-cwd>--``)."""

    stripped = _real_cwd(cwd).lstrip("/\\")
    return f"--{_PI_UNSAFE.sub('-', stripped)}--"


def _pi_header_id(path: Path) -> str | None:
    """The session id from a pi transcript's header line (bounded read)."""

    try:
        with Path(path).open("rb") as stream:
            line = stream.readline(_HEADER_SCAN_BYTES)
    except OSError as error:
        raise SessionFileError(f"cannot read pi session file {path}: {error}") from error
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SessionFileError(
            f"pi session file {path} has no readable header: {error}"
        ) from error
    if not isinstance(header, dict) or header.get("type") != "session":
        raise SessionFileError(f"pi session file {path} lacks a session header")
    header_id = header.get("id")
    return header_id if isinstance(header_id, str) and header_id else None


def pi_session_file(agent_dir: Path, cwd: str, session_ref: str) -> Path:
    """Locate a pi session transcript under the cwd's encoded session dir.

    The stored ref is OUR key; pi only ever sees the sanitized form
    (:func:`pi_session_id`), and the file name embeds that sanitized id.
    The header id is verified — a file whose name matches but whose header
    disagrees is corrupt, and resuming it would cold-start anyway.
    """

    # Deferred import: hyprial.harnesses re-enters hyprial.daemon at package load,
    # so this module (imported by daemon.application) must not touch the
    # harnesses package at top level.  pi_session itself is pure `re`.
    from hyprial.harnesses.pi_session import pi_session_id

    sanitized = pi_session_id(session_ref)
    session_dir = Path(agent_dir) / "sessions" / pi_session_dir_name(cwd)
    matches = (
        sorted(session_dir.glob(f"*_{sanitized}.jsonl"))
        if session_dir.is_dir()
        else []
    )
    if not matches:
        raise SessionFileNotFound(
            f"no pi session file for {session_ref!r} (pi id {sanitized!r}) "
            f"under {session_dir}"
        )
    if len(matches) > 1:
        raise SessionFileAmbiguous(
            f"pi session {sanitized!r} matches multiple files: "
            + ", ".join(str(match) for match in matches)
        )
    path = matches[0]
    header_id = _pi_header_id(path)
    if header_id != sanitized:
        raise SessionFileError(
            f"pi session file {path} header id {header_id!r} does not match "
            f"the expected id {sanitized!r}"
        )
    return path


def pi_session_target(agent_dir: Path, cwd: str, filename: str) -> Path:
    """Where the transcript belongs on the target for one (new) cwd."""

    return Path(agent_dir) / "sessions" / pi_session_dir_name(cwd) / filename


def codex_rollout_file(codex_home: Path, thread_id: str) -> Path:
    """Locate a codex rollout by thread id under ``sessions/``."""

    sessions = Path(codex_home) / "sessions"
    matches = (
        sorted(sessions.rglob(f"*{thread_id}.jsonl")) if sessions.is_dir() else []
    )
    if not matches:
        raise SessionFileNotFound(
            f"no codex rollout for thread {thread_id} under {sessions}"
        )
    if len(matches) > 1:
        raise SessionFileAmbiguous(
            f"codex thread {thread_id} matches multiple rollouts: "
            + ", ".join(str(match) for match in matches)
        )
    return matches[0]


def codex_rollout_target(codex_home: Path, source: Path) -> Path:
    """The target path preserving the rollout's ``sessions/``-relative path.

    The date hierarchy (``YYYY/MM/DD``) is part of how the app-server scans
    rollouts, so the target keeps the source's relative layout verbatim.
    """

    sessions = Path(codex_home) / "sessions"
    try:
        relative = Path(source).relative_to(sessions)
    except ValueError as error:
        raise SessionFileError(
            f"codex rollout {source} is not under {sessions}"
        ) from error
    return sessions / relative


def rewrite_pi_session_cwd(source: Path, target_cwd: str, dest: Path) -> Path:
    """Copy a pi transcript to ``dest`` with the header cwd repointed.

    pi's ``assertSessionCwdExists`` refuses to resume a session whose HEADER
    cwd does not exist on the machine — on a transfer target the source path
    is by definition absent (caught by the docker pseudo-machine
    verification: pi 0.84.2 exits "Stored session working directory does not
    exist").  The cwd lives only in the header line, so the rewrite is one
    JSON field on line one; every other byte is copied verbatim.  The SOURCE
    file is never touched — a rollback resumes it as-is.
    """

    with Path(source).open("rb") as stream:
        header_line = stream.readline(_HEADER_SCAN_BYTES)
        rest = stream.read()
    try:
        header = json.loads(header_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SessionFileError(
            f"pi session file {source} has no readable header: {error}"
        ) from error
    if not isinstance(header, dict) or header.get("type") != "session":
        raise SessionFileError(f"pi session file {source} lacks a session header")
    if isinstance(header.get("cwd"), str) and header["cwd"]:
        # Written VERBATIM: the path lives on the target, resolving it here
        # would bake the source's symlink mappings (e.g. macOS /tmp) in.
        header["cwd"] = target_cwd
    with Path(dest).open("wb") as stream:
        stream.write(
            json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        stream.write(rest)
    return Path(dest)


def locate_session_file(
    harness: str, cwd: str, session_ref: str, *, home: Path
) -> Path:
    """One seam for the orchestrator: locate by harness with default homes."""

    if harness == "claude":
        return claude_session_file(Path(home) / ".claude", cwd, session_ref)
    if harness == "pi":
        return pi_session_file(Path(home) / ".pi" / "agent", cwd, session_ref)
    if harness == "codex":
        return codex_rollout_file(Path(home) / ".codex", session_ref)
    raise SessionFileError(f"harness {harness!r} has no transferable session files")


def session_target_path(
    harness: str, cwd: str, source: Path, session_ref: str, *, home: Path
) -> Path:
    """One seam for the receive side: target path by harness, default homes."""

    if harness == "claude":
        return claude_session_target(Path(home) / ".claude", cwd, session_ref)
    if harness == "pi":
        return pi_session_target(Path(home) / ".pi" / "agent", cwd, Path(source).name)
    if harness == "codex":
        return codex_rollout_target(Path(home) / ".codex", source)
    raise SessionFileError(f"harness {harness!r} has no transferable session files")
