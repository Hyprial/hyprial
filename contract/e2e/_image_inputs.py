"""E2E-001 image build-inputs hash (spec-e2e001 追加 1 + 追加 4).

The image must correspond to the tested source tree.  Reuse is keyed on a
label, ``hyprial.e2e001.inputs=<hash>``, computed from what the Dockerfile
actually consumes — not from the whole-repo tree (a notes-only commit must
reuse the image) and not from a hand-maintained list that can drift from
the Dockerfile (the copy-source list is PARSED out of the Dockerfile, and a
guard test fails if the two ever disagree).

Hash recipe (fixed order, then sha256 over the joined lines):

  1. the first ``FROM`` line's text, verbatim (base image name by reference;
     digest drift of a moving tag is deliberately NOT covered — 追加 4)
  2. for every ``COPY``/``ADD`` source, in file order:
     ``git rev-parse <rev>:<path>`` — a blob for files, a subtree for
     directories (both change when content changes)
  3. the Dockerfile's own blob

Anything the parser cannot model — wildcards, ``COPY . .``, multi-stage
``--from=``, remote URLs, paths git cannot resolve — makes the whole
computation fall back to the single line ``git rev-parse <rev>^{tree}``:
rebuild-too-often is acceptable, build-from-stale is not (追加 4 防漏 b).

Docker builds from the working tree, but this hash describes the committed
tree.  ``dirty_inputs`` answers the one question that gap can turn into a
lie: "is any build input modified relative to <rev>?"  The caller must not
reuse an image when it returns True — the label would describe a commit
whose content was not what got built.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

class InputsHashError(RuntimeError):
    """Raised for anything the parser refuses to model (see module docstring)."""


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    if completed.returncode != 0:
        raise InputsHashError(
            f"git {' '.join(args)} exited {completed.returncode}: "
            f"{completed.stderr[-500:]!r}"
        )
    return completed.stdout.strip()


def join_dockerfile_lines(text: str) -> list[str]:
    """Join backslash continuations, drop comments and blanks."""

    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        buffer += line if not buffer else line.lstrip()
        if buffer.rstrip().endswith("\\"):
            buffer = buffer.rstrip()[:-1] + " "
            continue
        logical.append(buffer.strip())
        buffer = ""
    if buffer:
        logical.append(buffer.strip())
    return logical


def dockerfile_copy_sources(text: str) -> tuple[str, ...]:
    """Every COPY/ADD source path, in file order; raises on what this parser
    deliberately does not model (the caller turns that into the whole-tree
    fallback, per 追加 4 防漏 b)."""

    sources: list[str] = []
    for line in join_dockerfile_lines(text):
        tokens = line.split()
        if not tokens:
            continue
        verb = tokens[0].upper()
        if verb not in ("COPY", "ADD"):
            continue
        rest = tokens[1:]
        flags = [token for token in rest if token.startswith("--")]
        payload = [token for token in rest if not token.startswith("--")]
        for flag in flags:
            # --chmod/--chown do not change source resolution; --from=
            # pulls from another stage, whose inputs this parser does not
            # track.  Refuse rather than under-hash.
            if flag.startswith("--from="):
                raise InputsHashError(f"unmodelled COPY flag: {flag}")
        if len(payload) < 2:
            raise InputsHashError(f"unparseable {verb} instruction: {line}")
        # payload is `src ... dest`; every token but the last is a source.
        for source in payload[:-1]:
            if "://" in source or source.startswith("http:") or source.startswith("https:"):
                raise InputsHashError(f"remote {verb} source: {source}")
            if any(character in source for character in "*?"):
                raise InputsHashError(f"wildcard {verb} source: {source}")
            if source.startswith("/") or source in (".", "..") or "/../" in source or source.endswith("/.."):
                raise InputsHashError(f"context-escaping {verb} source: {source}")
            sources.append(source.rstrip("/"))
    return tuple(sources)


def first_from_line(text: str) -> str:
    for line in join_dockerfile_lines(text):
        if line.split()[:1] and line.split()[0].upper() == "FROM":
            return line
    raise InputsHashError("Dockerfile has no FROM instruction")


def inputs_hash(root: Path, *, rev: str = "HEAD") -> tuple[str, bool]:
    """``(sha256_hex, exact)``.

    ``exact`` is True when the hash was computed from the Dockerfile's own
    inputs; False when the whole-tree fallback was taken.  Callers log
    which one they used — a silent fallback is indistinguishable from an
    exact one in the label alone.
    """

    dockerfile = root / "contract/e2e/docker/Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    try:
        sources = dockerfile_copy_sources(text)
        lines = [first_from_line(text)]
        for source in sources:
            lines.append(_git(root, "rev-parse", f"{rev}:{source}"))
        lines.append(_git(root, "rev-parse", f"{rev}:contract/e2e/docker/Dockerfile"))
    except InputsHashError:
        return _git(root, "rev-parse", f"{rev}^{{tree}}"), False
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest, True


def dirty_inputs(root: Path, *, rev: str = "HEAD") -> bool:
    """True when any Dockerfile input differs between <rev> and the working
    tree (including untracked input paths).  Reuse must be refused then: the
    built image would contain working-tree content while the label names the
    commit.  Paths git cannot resolve count as dirty — never as a reason to
    trust a cached image."""

    dockerfile = root / "contract/e2e/docker/Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    try:
        sources = dockerfile_copy_sources(text)
    except InputsHashError:
        sources = ()  # whole-tree fallback: every path matters
    paths = [
        "contract/e2e/docker/Dockerfile",
        *sources,
    ]
    probe = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", *paths],
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    if probe.returncode != 0:
        return True
    return bool(probe.stdout.strip())


def describe(hash_value: str, *, exact: bool) -> str:
    """Human-facing suffix for RESULT lines."""

    return f"inputs={hash_value[:12]}{' exact' if exact else ' whole-tree-fallback'}"
