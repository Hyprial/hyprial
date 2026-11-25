"""Which hyprial a round runs, and how that choice is proven.

WHY THIS IS A SEPARATE MODULE (2026-10-16): ``_daemon_runner.py`` reached 1004
lines against the 1000-line structural limit, and the last +8 lines were the
gossip opt-in (#1510).  Splitting is the honest repair -- trimming comments or
blanks to fit would turn the count green while leaving the file one small edit
away from red again, which is the state that produced this one.

The unit stands on its own: ``_daemon_runner`` is the process machinery (start,
stop, request, isolate), while this is the provenance question that the runner
asks on the way in -- WHICH executable was chosen, which source tree it was
installed from, and whether that tree is the SHA the round declared.  Keeping
the answer next to the questions it answers is the point of the boundary; the
three functions are also the part most directly pinned by tests
(tests/integration/e2e/harness/execution/test_e2e_binary_resolution.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# Defined here rather than imported from _daemon_runner: that module imports THIS one, so the dependency
# must not run the other way.  Same expression against the same layout (contract/e2e/ -> repository root).
ROOT = Path(__file__).resolve().parents[2]


class HYPRIALResolution:
    """Which hyprial was selected, why, and which tree it was built from.

    The path alone cannot answer "was this run actually exercising the tree
    under test".  A resolver that picks between candidates and returns only
    the winner leaves that question unanswerable after the fact, which is
    exactly how one E2E run's provenance became undeterminable.  The choice
    is therefore part of the return value, not a log line.

    Deliberately a plain class rather than a dataclass: under ``from
    __future__ import annotations`` the dataclass machinery resolves
    annotations through ``sys.modules[cls.__module__]``, which is None for
    the spec-loaders some tests use to import this file directly.  Requiring
    callers to register the module first would make this helper harder to
    import than the thing it helps import.
    """

    __slots__ = ("path", "source", "src_dir", "tree_head", "src_reason")

    def __init__(
        self,
        *,
        path: Path,
        source: str,
        src_dir: Path | None,
        tree_head: str | None,
        src_reason: str = "",
    ) -> None:
        self.path = path
        self.source = source
        self.src_dir = src_dir
        self.tree_head = tree_head
        # How ``src_dir`` was decided -- and, when it is None, what was looked
        # for and what was found instead.  Without this the "cannot be checked"
        # failure names a conclusion and no evidence, which leaves a reader
        # guessing at the very moment the guard is asking them to act.
        self.src_reason = src_reason

    def __repr__(self) -> str:
        return (
            f"HYPRIALResolution(path={self.path!r}, source={self.source!r}, "
            f"src_dir={self.src_dir!r}, tree_head={self.tree_head!r})"
        )


def _tree_head(src_dir: Path | None) -> str | None:
    if src_dir is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _installed_source_tree(executable: Path) -> tuple[Path | None, str]:
    """Which tree ``executable``'s hyprial package was installed from, and how known.

    Read from the installer's own record (PEP 610 ``direct_url.json``) rather
    than inferred from where the binary happens to sit.  The distinction is
    not academic: this function replaces ``src_dir = ROOT if source == "tree
    .venv" else None``, which used the *name of the winning candidate* as a
    proxy for *whether a source tree exists on disk*.  Those are independent
    facts that merely correlated in every case tried by hand, and the one
    layout they disagree on -- CI, where ``HYPRIAL_BIN`` points into a venv built
    from the checkout under test -- is the layout the guard exists for.

    ``dir_info.editable`` is required, not merely a ``file://`` url.  A
    non-editable install records the tree it was *built* from, whose HEAD can
    move while the running code stays a frozen copy; the check would then go
    green about a tree it is not executing.
    """

    venv = executable.parent.parent
    site_packages = sorted(venv.glob("lib/python*/site-packages"))
    if not site_packages:
        return None, f"no site-packages under {venv}"
    # The project version is rewritten in-place by ``_bump_disposable_version``
    # for the install-flow scenario, so the directory name is not fixed.
    dist_infos = [
        entry
        for parent in site_packages
        for entry in sorted(parent.glob("hyprial-*.dist-info"))
    ]
    if not dist_infos:
        return None, f"no hyprial-*.dist-info under {venv}"
    if len(dist_infos) > 1:
        names = ", ".join(str(entry.name) for entry in dist_infos)
        return None, f"several hyprial dist-info directories under {venv}: {names}"
    record = dist_infos[0] / "direct_url.json"
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"{record} is unreadable ({type(error).__name__})"
    url = payload.get("url") if isinstance(payload, dict) else None
    dir_info = payload.get("dir_info") if isinstance(payload, dict) else None
    editable = bool(dir_info.get("editable")) if isinstance(dir_info, dict) else False
    if not isinstance(url, str) or not url.startswith("file://"):
        return None, f"{record} does not name a local directory (url={url!r})"
    if not editable:
        return None, (
            f"{record} records a non-editable install from {url}; its tree's "
            "HEAD would not describe the code being executed"
        )
    tree = Path(url[len("file://") :])
    if not tree.is_dir():
        return None, f"{record} names {tree}, which is not a directory"
    return tree, f"editable install recorded by {record}"


def resolve_hyprial() -> HYPRIALResolution:
    """Resolve the hyprial under test, reporting which candidate won.

    ``HYPRIAL_BIN`` wins when set; otherwise only the tree's own ``.venv``
    counts.  There is deliberately no fall back to ``PATH``: that silently
    selects the installed production build, so a scenario would pass while
    testing something other than the checkout.  Requiring an explicit
    ``HYPRIAL_BIN`` keeps "not the tree under test" a decision rather than an
    accident.

    Both candidates get their source tree derived the same way.  Deciding it
    per candidate is what produced the defect this function once had.
    """

    configured = os.environ.get("HYPRIAL_BIN")
    candidates: list[tuple[Path, str]] = []
    if configured:
        candidates.append((Path(configured), "HYPRIAL_BIN"))
    candidates.append((ROOT / ".venv" / "bin" / "hyprial", "tree .venv"))
    for candidate, source in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            src_dir, src_reason = _installed_source_tree(resolved)
            return HYPRIALResolution(
                path=resolved,
                source=source,
                src_dir=src_dir,
                tree_head=_tree_head(src_dir),
                src_reason=src_reason,
            )
    located = shutil.which("hyprial")
    raise AssertionError(
        "hyprial executable not found. Candidates tried, in order: "
        f"$HYPRIAL_BIN ({configured or 'unset'}), "
        f"{ROOT / '.venv' / 'bin' / 'hyprial'}. "
        "PATH is deliberately NOT a candidate: it resolves to the installed "
        f"build ({located or 'none on PATH'}), which is not the tree under "
        "test. Create the tree's .venv, or set HYPRIAL_BIN explicitly."
    )


_RECORDED_RESOLUTION: HYPRIALResolution | None = None


def record_resolution(resolution: HYPRIALResolution) -> None:
    """Emit the provenance line once per process.

    Kept out of :func:`resolve_hyprial` so the resolver stays free of side
    effects; every scenario reaches it anyway through :func:`hyprial_executable`,
    which is what all existing call sites use.
    """

    global _RECORDED_RESOLUTION
    if _RECORDED_RESOLUTION is not None:
        return
    _RECORDED_RESOLUTION = resolution
    line = (
        f"hyprial-under-test\tpath={resolution.path}\tsource={resolution.source}\t"
        f"src={resolution.src_dir or '-'}\thead={resolution.tree_head or '-'}\t"
        f"why={resolution.src_reason or '-'}"
    )
    print(line, flush=True)
    root = os.environ.get("HYPRIAL_E2E_ROOT")
    if not root:
        return
    try:
        diag = Path(root) / "diag"
        diag.mkdir(parents=True, exist_ok=True)
        with (diag / "hyprial-under-test.tsv").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # Provenance is evidence, not a gate: never fail a scenario because
        # the diag directory happened to be unwritable.
        pass


def hyprial_executable() -> Path:
    """Path-only view of :func:`resolve_hyprial`, kept for existing callers.

    Records the resolution on the way through, so all twenty existing
    scenarios gain provenance without twenty edits, and enforces the declared
    SHA when this run declared one.
    """

    resolution = resolve_hyprial()
    record_resolution(resolution)
    assert_tree_matches_declared_sha(resolution)
    return resolution.path


def assert_tree_matches_declared_sha(resolution: HYPRIALResolution) -> None:
    """Fail when the tree under test is not the SHA this run declared.

    Declaring nothing is allowed -- local runs do it constantly -- so this is
    a no-op without ``HYPRIAL_E2E_EXPECT_SHA`` (or CI's ``GITHUB_SHA``).  When a
    SHA *is* declared the check must be able to fail, which is why the tree's
    own HEAD is asserted to exist first: a bare "HEAD != declared" test also
    passes when the tree moved away or git stopped answering, and a check
    that goes green because its subject vanished is not guarding anything.
    """

    declared = os.environ.get("HYPRIAL_E2E_EXPECT_SHA") or os.environ.get(
        "GITHUB_SHA"
    )
    if not declared:
        return
    # Positive control: the thing being asserted about must be observable.
    if resolution.src_dir is None:
        raise AssertionError(
            f"declared SHA {declared} cannot be checked: no source tree found "
            f"for {resolution.path} ({resolution.source}). "
            f"Looked for the installer's record and found: "
            f"{resolution.src_reason or 'no reason recorded'}. "
            "Install hyprial editable into that venv, or unset the declared SHA."
        )
    if resolution.tree_head is None:
        raise AssertionError(
            f"declared SHA {declared} cannot be checked: git could not "
            f"rev-parse HEAD in {resolution.src_dir}. The check's subject is "
            "missing, so its silence would mean nothing."
        )
    if not resolution.tree_head.startswith(declared) and not declared.startswith(
        resolution.tree_head
    ):
        raise AssertionError(
            f"tree under test is {resolution.tree_head}, but this run declared "
            f"{declared}. The evidence would describe a different tree than "
            "the one named."
        )



