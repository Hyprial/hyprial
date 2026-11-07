"""Session-file locators: the harness-private layouts, consumed exactly once.

Each rule is pinned against a real observed layout (see the module docstring
in ``hyprial.transfer.session_files``); a harness that changes its layout must
fail these tests rather than silently cold-start a transferred worker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.session_files import (
    TRANSFERABLE_HARNESSES,
    SessionFileAmbiguous,
    SessionFileError,
    SessionFileNotFound,
    claude_project_dir_name,
    claude_session_file,
    claude_session_target,
    codex_rollout_file,
    codex_rollout_target,
    locate_session_file,
    pi_session_dir_name,
    pi_session_file,
    pi_session_target,
    rewrite_pi_session_cwd,
    session_target_path)

# ---------------------------------------------------------------------------
# Encoding rules (pinned to real observed directory names)
# ---------------------------------------------------------------------------


def test_claude_project_dir_encoding_matches_observed_layout() -> None:
    # Observed live (claude 2.1.235): /private/tmp/hyprial-q1-a -> -private-tmp-hyprial-q1-a
    assert claude_project_dir_name("/private/tmp/hyprial-q1-a") == "-private-tmp-hyprial-q1-a"
    assert claude_project_dir_name("/Users/alice/work space/x.y") == (
        "-Users-alice-work-space-x-y"
    )


def test_pi_session_dir_encoding_matches_observed_layout() -> None:
    # Observed live (pi >=0.76): /private/tmp/hyprial-cmp-a -> --private-tmp-hyprial-cmp-a--
    assert pi_session_dir_name("/private/tmp/hyprial-cmp-a") == "--private-tmp-hyprial-cmp-a--"
    assert pi_session_dir_name("/") == "----"
    assert pi_session_dir_name("/srv/work:alt") == "--srv-work-alt--"


def test_pi_encoding_resolves_symlinked_cwds(tmp_path: Path) -> None:
    # macOS /tmp -> /private/tmp is the motivating case (docker verification):
    # pi encodes the REAL path, so a logical path must resolve before encoding.
    real = tmp_path / "real-proj"
    real.mkdir()
    link = tmp_path / "link-proj"
    link.symlink_to(real)
    assert pi_session_dir_name(str(link)) == pi_session_dir_name(str(real))
    assert claude_project_dir_name(str(link)) == claude_project_dir_name(str(real))


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, header: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    return path


def test_claude_locate_in_encoded_cwd_dir(tmp_path: Path) -> None:
    expected = _write_jsonl(
        tmp_path / "projects" / "-work-proj" / "abc-123.jsonl", {"type": "session"}
    )
    assert claude_session_file(tmp_path, "/work/proj", "abc-123") == expected


def test_claude_locate_falls_back_to_global_search(tmp_path: Path) -> None:
    # Q1 behaviour: the session moved cwd, so the file sits under the OLD
    # project's encoded dir; resume (and therefore transfer) still finds it.
    expected = _write_jsonl(
        tmp_path / "projects" / "-old-dir" / "abc-123.jsonl", {"type": "session"}
    )
    assert claude_session_file(tmp_path, "/new/dir", "abc-123") == expected


def test_claude_locate_missing_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SessionFileNotFound):
        claude_session_file(tmp_path, "/work/proj", "abc-123")


def test_claude_locate_ambiguous_fails_loudly(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "projects" / "-a" / "abc-123.jsonl", {"type": "session"})
    _write_jsonl(tmp_path / "projects" / "-b" / "abc-123.jsonl", {"type": "session"})
    with pytest.raises(SessionFileAmbiguous):
        claude_session_file(tmp_path, "/c", "abc-123")


def test_claude_target_uses_the_new_cwd_encoding(tmp_path: Path) -> None:
    assert claude_session_target(tmp_path, "/srv/work", "abc-123") == (
        tmp_path / "projects" / "-srv-work" / "abc-123.jsonl"
    )


# ---------------------------------------------------------------------------
# pi
# ---------------------------------------------------------------------------


def _write_pi_session(
    agent_dir: Path, cwd: str, pi_id: str, *, header_id: str | None = None
) -> Path:
    path = (
        agent_dir
        / "sessions"
        / pi_session_dir_name(cwd)
        / f"2026-08-20T04-50-37-658Z_{pi_id}.jsonl"
    )
    return _write_jsonl(
        path, {"type": "session", "id": header_id if header_id is not None else pi_id}
    )


def test_pi_locate_by_sanitized_id_with_header_verification(tmp_path: Path) -> None:
    expected = _write_pi_session(tmp_path, "/work/proj", "session-1")
    assert pi_session_file(tmp_path, "/work/proj", "session-1") == expected


def test_pi_locate_sanitizes_raw_refs_at_the_boundary(tmp_path: Path) -> None:
    # Raw refs with pi-illegal characters (colons) map to the on-disk id.
    expected = _write_pi_session(tmp_path, "/work/proj", "squire.alw.usrb")
    assert pi_session_file(tmp_path, "/work/proj", "squire:alw:usrb") == expected


def test_pi_locate_missing_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SessionFileNotFound):
        pi_session_file(tmp_path, "/work/proj", "session-1")


def test_pi_locate_header_mismatch_fails_loudly(tmp_path: Path) -> None:
    _write_pi_session(tmp_path, "/work/proj", "session-1", header_id="other")
    with pytest.raises(SessionFileError, match="header id"):
        pi_session_file(tmp_path, "/work/proj", "session-1")


def test_pi_locate_ambiguous_fails_loudly(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / pi_session_dir_name("/work/proj")
    _write_jsonl(
        session_dir / "2026-08-20T04-50-37-658Z_session-1.jsonl",
        {"type": "session", "id": "session-1"},
    )
    _write_jsonl(
        session_dir / "2026-08-21T09-00-00-000Z_session-1.jsonl",
        {"type": "session", "id": "session-1"},
    )
    with pytest.raises(SessionFileAmbiguous):
        pi_session_file(tmp_path, "/work/proj", "session-1")


def test_pi_target_keeps_filename_under_the_new_cwd_dir(tmp_path: Path) -> None:
    assert pi_session_target(
        tmp_path, "/srv/work", "2026-08-20T04-50-37-658Z_s-1.jsonl"
    ) == (
        tmp_path
        / "sessions"
        / "--srv-work--"
        / "2026-08-20T04-50-37-658Z_s-1.jsonl"
    )


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def test_codex_locate_rollout_by_thread_id(tmp_path: Path) -> None:
    expected = _write_jsonl(
        tmp_path
        / "sessions"
        / "2026"
        / "08"
        / "20"
        / "rollout-2026-08-20T15-28-08-019db93d-thread-9.jsonl",
        {"type": "session"},
    )
    assert codex_rollout_file(tmp_path, "thread-9") == expected


def test_codex_locate_missing_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SessionFileNotFound):
        codex_rollout_file(tmp_path, "thread-9")


def test_codex_target_preserves_the_date_hierarchy(tmp_path: Path) -> None:
    source = (
        tmp_path
        / "sessions"
        / "2026"
        / "08"
        / "20"
        / "rollout-2026-08-20T15-28-08-019db93d-thread-9.jsonl"
    )
    assert codex_rollout_target(tmp_path, source) == source


def test_codex_target_rejects_files_outside_sessions(tmp_path: Path) -> None:
    with pytest.raises(SessionFileError, match="not under"):
        codex_rollout_target(tmp_path, tmp_path / "elsewhere" / "x.jsonl")


# ---------------------------------------------------------------------------
# The orchestrator seams
# ---------------------------------------------------------------------------


def test_locate_session_file_dispatches_with_default_homes(tmp_path: Path) -> None:
    expected = _write_jsonl(
        tmp_path / ".claude" / "projects" / "-work" / "s-1.jsonl", {"type": "session"}
    )
    assert locate_session_file("claude", "/work", "s-1", home=tmp_path) == expected
    with pytest.raises(SessionFileError, match="no transferable"):
        locate_session_file("dsh", "/work", "s-1", home=tmp_path)


def test_session_target_path_dispatches_with_default_homes(tmp_path: Path) -> None:
    assert session_target_path("claude", "/srv", Path("/x/s-1.jsonl"), "s-1", home=tmp_path) == (
        tmp_path / ".claude" / "projects" / "-srv" / "s-1.jsonl"
    )
    with pytest.raises(SessionFileError, match="no transferable"):
        session_target_path("lark", "/srv", Path("/x/s-1.jsonl"), "s-1", home=tmp_path)


def test_transferable_harnesses_are_the_p0_set() -> None:
    assert TRANSFERABLE_HARNESSES == frozenset({"pi", "codex", "claude"})


# ---------------------------------------------------------------------------
# pi header cwd rewrite (pi's stored-cwd existence check)
# ---------------------------------------------------------------------------


def test_rewrite_pi_session_cwd_repoints_only_the_header(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / pi_session_dir_name("/old/proj")
    session_dir.mkdir(parents=True)
    source = session_dir / "2026-08-20T04-50-37-658Z_session-1.jsonl"
    source.write_text(
        json.dumps({"type": "session", "id": "session-1", "cwd": "/old/proj"})
        + "\n",
        encoding="utf-8",
    )
    with source.open("ab") as stream:
        stream.write(b'{"type":"message","cwd":"/old/proj"}\n')
    dest = tmp_path / "out.jsonl"

    rewrite_pi_session_cwd(source, "/new/proj", dest)

    lines = dest.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["cwd"] == "/new/proj"
    assert header["id"] == "session-1"
    # Entry lines are byte-verbatim (only the header is pi's cwd authority).
    assert lines[1] == '{"type":"message","cwd":"/old/proj"}'
    # The source file is untouched (rollback resumes it as-is).
    assert json.loads(source.read_text(encoding="utf-8").splitlines()[0])[
        "cwd"
    ] == "/old/proj"


def test_rewrite_pi_session_cwd_without_header_cwd_is_a_verbatim_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "s.jsonl"
    source.write_text(
        json.dumps({"type": "session", "id": "s-1"}) + '\n{"payload":1}\n',
        encoding="utf-8",
    )
    dest = tmp_path / "out.jsonl"
    rewrite_pi_session_cwd(source, "/new/proj", dest)
    assert json.loads(dest.read_text(encoding="utf-8").splitlines()[0]) == {
        "type": "session",
        "id": "s-1",
    }


def test_rewrite_pi_session_cwd_rejects_non_session_files(tmp_path: Path) -> None:
    source = tmp_path / "s.jsonl"
    source.write_text('{"type":"message"}\n', encoding="utf-8")
    with pytest.raises(SessionFileError):
        rewrite_pi_session_cwd(source, "/new/proj", tmp_path / "out.jsonl")
