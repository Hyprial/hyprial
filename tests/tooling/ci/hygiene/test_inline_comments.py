"""Repros for scripts/ci/check_inline_comments.py (the non-blocking reminder).

Every case builds a fresh repository under pytest's ``tmp_path``: nothing reads
the developer's repository, HOME configuration, or the network.  The cases pin
the rules that decide what counts as a NEW inline comment:

* hash comments (Python/Shell/YAML/TOML) and slash comments (JS/TS/Rust/Go);
* the file-header decision block, shebang and encoding lines are exempt;
* Python docstrings and every language's strings are not comments;
* tool directives (noqa/type/pragma/eslint/ts-ignore/rustfmt and friends) are
  exempt;
* only lines added by the diff are judged (existing comments are untouched);
* foreign languages and out-of-scope paths are never guessed at.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_inline_comments.py"


def _env(repo: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": str(repo / ".gitconfig-none"),
        "GIT_CONFIG_SYSTEM": str(repo / ".gitconfig-none"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    )
    assert completed.returncode == 0, f"git {' '.join(args)}: {completed.stderr}"
    return completed


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit(repo: Path) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _run(
    repo: Path, base: str, head: str, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", head, *extra],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    )


def _locations(stdout: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for line in stdout.splitlines():
        if not line.strip() or line.startswith(("::warning", "inline-comment:")):
            continue
        path, _, rest = line.partition(":")
        number, _, _ = rest.partition(":")
        if path and number.isdigit():
            found.setdefault(path, []).append(int(number))
    return found


def test_new_inline_comments_are_reported_for_every_covered_language(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    base = _base(repo)
    _write(repo, "src/pkg/mod.py", "x = 1\ny = 2  # decision lives here\n")
    _write(repo, "src/gui/app.mjs", 'const a = 1;\nconst b = 2; // js note\n')
    _write(repo, "desktop/src-tauri/lib.rs", "fn a() {}\nfn b() {} // rust note\n")
    _write(repo, "sidecar/main.go", "package main\n// go note\n")
    _write(
        repo,
        ".github/workflows/sample.yml",
        "jobs:\n  build:\n    runs-on: x # yaml note\n",
    )
    _write(repo, "scripts/ci/sample.sh", "#!/bin/sh\necho hi # sh note\n")
    _write(repo, "scripts/ci/sample.toml", "name = 'x' # toml note\n")
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {
        "src/pkg/mod.py": [2],
        "src/gui/app.mjs": [2],
        "desktop/src-tauri/lib.rs": [2],
        "sidecar/main.go": [2],
        ".github/workflows/sample.yml": [3],
        "scripts/ci/sample.sh": [2],
        "scripts/ci/sample.toml": [1],
    }
    assert "把决策移到文件头决策块" in result.stdout
    assert "docstring" in result.stdout


def test_the_file_header_block_shebang_and_encoding_are_exempt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(
        repo,
        "src/pkg/header.py",
        "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nx = 1\n",
    )
    _write(repo, "src/gui/header.mjs", "export const a = 1;\n")
    _write(repo, "scripts/ci/header.sh", "#!/bin/sh\necho ok\n")
    base = _commit(repo)
    _write(
        repo,
        "src/pkg/header.py",
        "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
        "# decision: the header owns the why\nx = 1\n",
    )
    _write(
        repo,
        "src/gui/header.mjs",
        "/* decision: header block */\nexport const a = 1;\n",
    )
    _write(
        repo,
        "scripts/ci/header.sh",
        "#!/bin/sh\n# decision: header block\necho ok\n",
    )
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {}


def test_python_docstrings_and_strings_are_not_comments(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/pkg/doc.py", "x = 1\n")
    base = _base(repo)
    _write(
        repo,
        "src/pkg/doc.py",
        'x = 1\ns = "# not a comment"\n\n\ndef f():\n    """Docstring.\n'
        '    # not a comment either\n    """\n    return "# still a string"\n',
    )
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {}


def test_tool_directives_are_exempt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/pkg/directives.py", "x = 1\n")
    _write(repo, "src/gui/directives.mjs", "export const a = 1;\n")
    base = _commit(repo)
    _write(
        repo,
        "src/pkg/directives.py",
        "x = 1  # noqa: E501\ny = 2  # type: ignore[attr-defined]\n"
        "z = 3  # pragma: no cover\nw = 4  # fmt: off\n",
    )
    _write(
        repo,
        "src/gui/directives.mjs",
        "export const a = 1;\n// eslint-disable-next-line no-console\n"
        "// @ts-ignore\nexport const b = 2; // prettier-ignore\n",
    )
    _write(repo, "sidecar/directives.go", "package main\n//go:build linux\n")
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {}


def test_only_added_lines_are_judged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/pkg/old.py", "x = 1  # existing comment stays\n")
    base = _commit(repo)
    _write(
        repo,
        "src/pkg/old.py",
        "x = 1  # existing comment stays\ny = 2  # new comment\n",
    )
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {"src/pkg/old.py": [2]}


def test_javascript_block_comment_and_header_block(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "src/gui/block.mjs", "export const a = 1;\n")
    base = _commit(repo)
    _write(
        repo,
        "src/gui/block.mjs",
        "export const a = 1;\n/* inline decision */\nexport const b = 2;\n",
    )
    _write(
        repo,
        "src/gui/new-header.mjs",
        "/* decision: this file explains itself at the top */\n"
        "export const c = 3;\n// the first code comment is still new\n",
    )
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {
        "src/gui/block.mjs": [2],
        "src/gui/new-header.mjs": [3],
    }


def test_out_of_scope_and_non_code_paths_are_not_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _base(repo)
    _write(repo, "docs/guide.md", "# a markdown heading\n")
    _write(repo, "README.md", "# a root heading\n")
    _write(repo, "src/hyprial/notes.txt", "# not a covered language\n")
    head = _commit(repo)

    result = _run(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert _locations(result.stdout) == {}


def test_the_reminder_is_non_blocking_and_strict_is_the_single_upgrade(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    base = _base(repo)
    _write(repo, "src/pkg/mod.py", "x = 1  # reminder\n")
    head = _commit(repo)

    reminder = _run(repo, base, head)
    strict = _run(repo, base, head, "--strict")

    assert reminder.returncode == 0, reminder.stderr
    assert strict.returncode == 1, strict.stdout
    assert _locations(reminder.stdout) == {"src/pkg/mod.py": [1]}


def test_json_output_and_ci_annotations(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _base(repo)
    _write(repo, "src/pkg/mod.py", "x = 1  # reminder\n")
    head = _commit(repo)

    result = _run(repo, base, head, "--json", "--annotations")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["strict"] is False
    assert payload["findings"] == [
        {
            "path": "src/pkg/mod.py",
            "line": 1,
            "comment": "reminder",
            "suggestion": "把决策移到文件头决策块（docstring 保留）",
        }
    ]
    assert "::warning file=src/pkg/mod.py,line=1::" in result.stderr
