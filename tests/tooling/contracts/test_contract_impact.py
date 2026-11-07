"""contract-impact gate: drift must red; sync, declaration, or no-change must green.

The fixtures mirror the measured shapes, not invented ones: the base source is
the #519 dict (`"meshHolders": list(mesh.holders)` beside a `code="..."`
error-code call) and the base contract is the backticked reference form that
`contract/e2e/01-two-machine-roundtrip.md` actually uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CHECK = ROOT / "scripts" / "verify" / "check_contract_impact.py"

BASE_APP = """\
def diagnostics(mesh):
    return {
        "meshHolders": list(mesh.holders),
        "localRecords": len(mesh.records),
        "records": [record.to_json() for record in merged],
    }


def submit(mesh):
    return SubmissionResult(mesh.message_id, False, code="TARGET_OFFLINE")
"""

BASE_CONTRACT = """\
# 01-two-machine-roundtrip

`meshReplies=1 meshHolders=['node-a'] undecodable=0 noKnownLoss=True` -- the
assertion: `meshHolders` contains both holders (line 141 form).
The word records appears in prose; error TARGET_OFFLINE must not surface.
"""

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Contract Impact Test",
    "GIT_AUTHOR_EMAIL": "cit@example.com",
    "GIT_COMMITTER_NAME": "Contract Impact Test",
    "GIT_COMMITTER_EMAIL": "cit@example.com",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
        env=_GIT_ENV,
    )


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run(
    repo: Path, base: str, head: str, body: str | None = None
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable, str(CHECK),
        "--repo", str(repo), "--base", base, "--head", head,
    ]
    if body is not None:
        body_file = repo / ".pr-body"
        body_file.write_text(body, encoding="utf-8")
        args += ["--pr-body-file", str(body_file)]
    return subprocess.run(args, text=True, capture_output=True, check=False)


@pytest.fixture()
def repo_with_base(tmp_path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    base = _commit(
        tmp_path,
        "base",
        {
            "src/hyprial/daemon/application.py": BASE_APP,
            "contract/e2e/01-two-machine-roundtrip.md": BASE_CONTRACT,
        },
    )
    return tmp_path, base


def test_value_replacement_reds(repo_with_base: tuple[Path, str]) -> None:
    """#519 shape: key stays, the value's first token changes -> RED."""
    repo, base = repo_with_base
    head = _commit(
        repo,
        "widen meshHolders to all responders",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                "list(mesh.holders)", "list(mesh.responded_holders)"
            ),
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "meshHolders" in result.stdout
    assert "contract/e2e/01-two-machine-roundtrip.md" in result.stdout
    assert "candidates=1" in result.stdout


def test_key_deletion_reds(repo_with_base: tuple[Path, str]) -> None:
    repo, base = repo_with_base
    head = _commit(
        repo,
        "drop meshHolders from diagnostics",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                '        "meshHolders": list(mesh.holders),\n', ""
            ),
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "meshHolders" in result.stdout


def test_key_rename_reds(repo_with_base: tuple[Path, str]) -> None:
    repo, base = repo_with_base
    head = _commit(
        repo,
        "rename meshHolders to meshResponders",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                '"meshHolders"', '"meshResponders"'
            ),
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "meshHolders" in result.stdout


def test_declaration_downgrades_to_notice(
    repo_with_base: tuple[Path, str],
) -> None:
    repo, base = repo_with_base
    head = _commit(
        repo,
        "widen meshHolders to all responders",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                "list(mesh.holders)", "list(mesh.responded_holders)"
            ),
        },
    )
    result = _run(
        repo,
        base,
        head,
        body="Title\n\nContract-Impact: semantics widened to all responders "
        "on purpose; scenario updated in the next PR.\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DECLARED RELEASE" in result.stdout
    assert "declared=yes" in result.stdout
    # The impact is still printed, so the release is named, not silent.
    assert "meshHolders" in result.stdout


def test_declaration_with_empty_reason_is_not_a_release(
    repo_with_base: tuple[Path, str],
) -> None:
    repo, base = repo_with_base
    head = _commit(
        repo,
        "widen meshHolders to all responders",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                "list(mesh.holders)", "list(mesh.responded_holders)"
            ),
        },
    )
    result = _run(repo, base, head, body="Contract-Impact:\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "declared=no" in result.stdout


def test_contract_synced_in_same_increment_greens(
    repo_with_base: tuple[Path, str],
) -> None:
    repo, base = repo_with_base
    head = _commit(
        repo,
        "widen meshHolders and update the scenario",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                "list(mesh.holders)", "list(mesh.responded_holders)"
            ),
            "contract/e2e/01-two-machine-roundtrip.md": BASE_CONTRACT
            + "\nUpdated 2026-09-15: meshHolders now lists every responder.\n",
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "impacts=0" in result.stdout
    assert "candidates=1" in result.stdout


def test_empty_increment_greens_and_prints_zero(
    repo_with_base: tuple[Path, str],
) -> None:
    repo, base = repo_with_base
    result = _run(repo, base, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "candidates=0" in result.stdout
    assert "0 candidate names -- check ran and found none" in result.stdout


def test_pure_move_and_rewrap_do_not_fire(
    repo_with_base: tuple[Path, str],
) -> None:
    """Same key + same first value token elsewhere = move/reformat, not drift.

    The rewrap half is #500's measured shape: same key, same value, the rest
    of the line rewritten by a multi-line conditional.
    """
    repo, base = repo_with_base
    moved = BASE_APP.replace(
        '        "meshHolders": list(mesh.holders),\n', ""
    ).replace(
        "def submit(mesh):",
        "def other(mesh):\n"
        "    return {\n"
        '        "meshHolders": list(mesh.holders),\n'
        "    }\n"
        "\n"
        "\n"
        "def submit(mesh):",
    ).replace(
        '        "localRecords": len(mesh.records),\n',
        '        **({"localRecords": len(mesh.records)} if mesh.records else {}),\n',
    )
    head = _commit(repo, "move + rewrap, no semantics change", {
        "src/hyprial/daemon/application.py": moved,
    })
    result = _run(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "candidates=0" in result.stdout


def test_error_code_replacement_reds(repo_with_base: tuple[Path, str]) -> None:
    repo, base = repo_with_base
    head = _commit(
        repo,
        "rename the offline error code",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                'code="TARGET_OFFLINE"', 'code="TARGET_UNREACHABLE"'
            ),
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "TARGET_OFFLINE" in result.stdout
    assert "(code)" in result.stdout


def test_generic_single_word_key_is_documented_out_of_scope(
    repo_with_base: tuple[Path, str],
) -> None:
    """`records` changed semantics, but a bare word cannot be grepped in prose.

    This pins the measured recall gap ON PURPOSE (20-PR study 2026-09-15):
    single lowercase words produced every prose false positive. If this test
    ever fails, the distinctiveness rule changed -- rerun the study before
    trusting the new rule on CI.
    """
    repo, base = repo_with_base
    head = _commit(
        repo,
        "project records",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                '"records": [record.to_json() for record in merged],',
                '"records": projected,',
            ),
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "candidates=0" in result.stdout


def test_added_side_only_does_not_fire(repo_with_base: tuple[Path, str]) -> None:
    """A purely ADDED key breaks no published contract (2026-09-15 ruling).

    The old producer line is untouched and a second producer is ADDED:
    nothing published was deleted or modified, so the gate must stay green.
    This pins the deleted-side-only rule; if it ever fails, the extractor
    regressed to the touched-lines draft that measured 7/20 red on merged
    PRs (#495/#482/#478 were its false positives).
    """
    repo, base = repo_with_base
    head = _commit(
        repo,
        "add a second meshHolders producer",
        {
            "src/hyprial/daemon/application.py": BASE_APP.replace(
                "def submit(mesh):",
                "def alt(mesh):\n"
                "    return {\n"
                '        "meshHolders": list(mesh.responded_holders),\n'
                "    }\n"
                "\n"
                "\n"
                "def submit(mesh):",
            ),
        },
    )
    result = _run(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "candidates=0" in result.stdout


def test_whole_file_deletion_cites_the_real_source_path(
    repo_with_base: tuple[Path, str],
) -> None:
    """Deleting the whole source file is drift, cited at its real path.

    The raw diff says `+++ /dev/null` for a deleted file; the impact line
    must not inherit that placeholder as the source location.
    """
    repo, base = repo_with_base
    (repo / "src/hyprial/daemon/application.py").unlink()
    head = _commit(repo, "delete the diagnostics source", {})
    result = _run(repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "meshHolders" in result.stdout
    assert "src/hyprial/daemon/application.py" in result.stdout
    assert "/dev/null" not in result.stdout


def test_unresolvable_rev_fails_closed(repo_with_base: tuple[Path, str]) -> None:
    repo, base = repo_with_base
    result = _run(repo, "no-such-ref", base)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "OPERATIONAL" in result.stderr
