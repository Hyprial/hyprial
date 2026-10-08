"""Mapping-time sandbox smoke for a transferred worker (design §4).

``docs/design-agent-standard-form.md`` §4 splits sandboxing in two: the outer
transfer layer packs, ships and maps; the harness itself keeps its own
sandbox.  What the outer layer owes, between "the payload is mapped on the
target" and "the session is strict-resumed", is a few-second smoke that uses
the harness's own sandbox entry point -- no model, no credentials -- and
**refuses the landing when the smoke fails**.  That step was missing from the
receive path: only the smolvm carrier smoked anything (at worker start), and
that carrier is not a supported landing for a transferred worker.

What this module can and cannot claim:

* It runs the **host** entry point of the harness.  For codex that is
  ``codex sandbox -c 'sandbox_mode="read-only"'`` over a sentinel file the
  sandbox must refuse to overwrite, with ``CODEX_HOME`` pointed at a scratch
  directory and model-vendor keys stripped from the environment.
* A harness with no provable native sandbox entry point in this repo (pi,
  and claude whose confinement this repo does not certify) is reported as
  ``NOT_RUN``.  That is a warning, not a veto: the outer layer does not
  manufacture a sandbox to fill the gap, and admission for those harnesses is
  a published-matrix question, not this function's.
* A container landing hands the harness a sandbox from the image.  The host
  entry point is not evidence for the guest, so that case is ``NOT_RUN`` here
  and the honest gap is stated instead of being papered over.

The smoke does not prove that the *worker* execution path honours the same
sandbox: it proves the entry point denies an outside write on this machine.
That residual risk is the one §4 already names.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

#: Statuses a smoke verdict can carry.  ``NOT_RUN`` is never a silent pass.
SMOKE_PASS = "PASS"
SMOKE_NOT_RUN = "NOT_RUN"
SMOKE_FAIL = "FAIL"

#: POSIX sh + ``cksum`` (not sha256sum/stat, which differ between macOS and
#: Linux).  ``$1`` is the sentinel path *outside* the sandbox's writable
#: workspace: reading must work, overwriting must be denied.  Exit 91 is the
#: load-bearing case -- the write succeeded, so the sandbox did not enforce.
_SENTINEL_SCRIPT = r"""
set -eu
sentinel="$1"
test -r "$sentinel"
before=$(cksum < "$sentinel")
if printf changed > "$sentinel" 2>/dev/null; then
  exit 91
fi
after=$(cksum < "$sentinel")
test "$before" = "$after"
printf 'native-read-only-sentinel: PASS\n'
"""

#: Environment names that would let a smoke "pass" by talking to a model.
#: The smoke is supposed to be credential-free, so they are removed outright.
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@dataclass(frozen=True)
class SmokeResult:
    """One sandbox smoke verdict, with the command that produced it."""

    harness: str
    status: str
    detail: str
    command: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "harness": self.harness,
            "status": self.status,
            "detail": self.detail,
            "command": list(self.command),
        }


def _codex_smoke_argv(sentinel: str) -> tuple[str, ...]:
    return (
        "codex",
        "sandbox",
        "-c",
        'sandbox_mode="read-only"',
        "--",
        "/bin/sh",
        "-c",
        _SENTINEL_SCRIPT,
        "hyprial-sandbox-smoke",
        sentinel,
    )


def _scratch_env(root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if base is None else base)
    for name in list(source):
        if name.upper().endswith(_CREDENTIAL_SUFFIXES):
            del source[name]
    # codex refuses to start when CODEX_HOME does not exist ("CODEX_HOME
    # points to ..., but that path does not exist"), so the scratch home has
    # to be created here -- a smoke that cannot start would fail every codex
    # landing on a machine whose sandbox is perfectly fine.
    (root / "codex-home").mkdir(parents=True, exist_ok=True)
    source["CODEX_HOME"] = str(root / "codex-home")
    source["TMPDIR"] = str(root / "tmp")
    return source


def _tail(value: bytes | str | None, limit: int = 400) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    text = text.strip()
    return text[-limit:]


def run_sandbox_smoke(
    harness: str,
    *,
    containerized: bool = False,
    runner: Runner | None = None,
    timeout: float = 20.0,
    base_env: Mapping[str, str] | None = None,
) -> SmokeResult:
    """Smoke ``harness``'s own sandbox entry point on this machine.

    ``runner`` is the subprocess seam (same signature as ``subprocess.run``);
    tests inject it, production uses the real one.
    """

    if containerized:
        return SmokeResult(
            harness,
            SMOKE_NOT_RUN,
            "container landing: the harness runs inside the image, so the host "
            "entry point is not evidence for the guest sandbox; this receive "
            "path runs no guest smoke",
        )
    if harness != "codex":
        return SmokeResult(
            harness,
            SMOKE_NOT_RUN,
            f"{harness} has no provable native sandbox entry point in this "
            "repo; the outer layer does not add one (design §4)",
        )

    run: Runner = subprocess.run if runner is None else runner
    with tempfile.TemporaryDirectory(prefix="hyprial-sandbox-smoke-") as tmp:
        root = Path(tmp)
        (root / "tmp").mkdir()
        sentinel = root / "sentinel"
        sentinel.write_text("before\n", encoding="utf-8")
        argv = _codex_smoke_argv(str(sentinel))
        try:
            completed = run(
                list(argv),
                capture_output=True,
                timeout=timeout,
                check=False,
                env=_scratch_env(root, base_env),
            )
        except subprocess.TimeoutExpired:
            return SmokeResult(
                harness,
                SMOKE_FAIL,
                f"codex sandbox smoke did not finish within {timeout:g}s; "
                "a sandbox that hangs cannot be trusted to refuse writes",
                argv,
            )
        except OSError as error:
            return SmokeResult(
                harness,
                SMOKE_FAIL,
                f"codex sandbox smoke could not run: {error}",
                argv,
            )
        returncode = int(getattr(completed, "returncode", -1))
        if returncode == 91:
            return SmokeResult(
                harness,
                SMOKE_FAIL,
                "the sandboxed write to the outside sentinel SUCCEEDED: the "
                "sandbox is not enforcing on this machine",
                argv,
            )
        if returncode != 0:
            detail = _tail(getattr(completed, "stderr", None)) or _tail(
                getattr(completed, "stdout", None)
            )
            return SmokeResult(
                harness,
                SMOKE_FAIL,
                f"codex sandbox smoke exited {returncode}: {detail or 'no output'}",
                argv,
            )
        return SmokeResult(
            harness,
            SMOKE_PASS,
            "codex read-only sandbox denied the outside write and left the "
            "sentinel unchanged; this covers the entry point only, not every "
            "worker tool path",
            argv,
        )


def summarise(results: Sequence[SmokeResult]) -> str:
    """One line for a receipt: never let a NOT_RUN read as a pass."""

    parts = [f"{result.harness}:{result.status}" for result in results]
    return ", ".join(parts)
