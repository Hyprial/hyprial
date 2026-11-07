"""Static supervision-boundary checks: who still builds their own start logic.

Six capabilities belong to ``hyprial.actor_runtime`` and nowhere else (see
``docs/notes/supervision-consolidation-standard-2026-08-29.md`` §1): non-blocking
start, ready reporting, start timeout, retry/backoff, quarantine, and child
lifetime binding.  The same shape has now been written three times, each after
its own incident, and each fix touched only its own site.  The trigger has to
be mechanical because the person who needs it is whoever edits some *other*
module next, and they will not have read the standard.

Like ``actor_boundary_gate``, this parses Python rather than grepping it: a
docstring saying "bounded by ``startup_timeout``" is prose, not an
implementation.

⚠️ The hard part here is not finding matches, it is **not** finding everything.
``cli.py`` alone carries a socket-connect timeout, an RPC request timeout and a
``--timeout`` flag, none of which are supervision.  A word list of
``timeout|backoff|delay`` matches all of them and buries the five real sites, so
a name qualifies only when it is *compound*:

    a start noun (start / ready / boot / launch / spawn ...)
    AND a limit noun (timeout / deadline / seconds / threshold ...)
    OR a token that means nothing else here (quarantine / backoff)

That is what separates ``startup_timeout`` from ``timeout``, and
``_BOOTSTRAP_REPORT_SECONDS`` from ``AGENT_HEARTBEAT_TTL_SECONDS``.

The compound rule cannot see a site that never names the value --
``.wait(timeout=60.0)`` with the literal inline -- so ``scan_blocking_waits``
covers that shape separately.  It is reported, never manifested: the two rules
answer different questions and merging their outputs would let a site satisfy
one while failing the other.

⚠️ **The failure mode this file is most likely to die of.**  The manifest is
generated from the scanner, so it cannot notice the scanner going blind.  If a
rule stops matching a site that is still there, every entry for that site turns
into "stale exemption -- the migration finished", and the remedy the error
message asks for is to *delete the entry*.  Do that and the site is unmanifested,
unguarded, and the gate is green.  A check that stopped running looks exactly
like a check that passed, and here the check even hands you the instructions for
finishing it off.

Two things hold that shut, and both must survive any refactor of this file:

* ``NAMED_BY_THE_STANDARD`` in the test module is transcribed from the
  standard's hand-compiled table -- a source independent of this scanner -- and
  asserts recall directly.  It is the only thing here that can fail *because*
  the scanner went blind.
* The stale-entry message says to confirm the site is really gone by reading it.
  An exemption may be deleted on evidence from the source file, never on the
  say-so of this gate.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.support.gates.authority_migration import SUPERVISION_CROSSWALK, lexical_definitions

#: ``actor_runtime`` is the definition site for every managed capability, so it
#: is the one place allowed to name them.  Exempting it is not a concession --
#: a hit inside core is core doing its job.
#: (PY-BATCH-2026-10-01 foundation: the package moved to kernel/impl/actor_runtime;
#: the core check follows the package, authority semantics unchanged.)
CORE_ROOT = Path("kernel/impl/actor_runtime")

# ── the compound name rule ────────────────────────────────────────────────

#: Words that mean "bringing something up".  Without one of these, a limit is
#: some other subsystem's limit and none of our business.
_START_NOUNS = frozenset(
    {
        "start",
        "starts",
        "starting",
        "startup",
        "restart",
        "restarts",
        "ready",
        "readiness",
        "readable",
        "boot",
        "bootstrap",
        "launch",
        "spawn",
        "settle",
        "settled",
    }
)

#: Words that mean "a bound on time or count".  Without one of these, a start
#: noun is just naming the thing being started (``ready_fd``, ``_start_lock``).
_LIMIT_NOUNS = frozenset(
    {
        "timeout",
        "timer",
        "deadline",
        "delay",
        "budget",
        "seconds",
        "cooldown",
        "interval",
        "elapsed",
        "threshold",
        "max",
        "wait",
        "attempts",
        "retries",
        "flight",  # ``_starts_in_flight``: bookkeeping that bounds concurrency
        # ``backoff`` and ``inflight`` are limits, not standalone evidence.
        # Measured: as standalone tokens they pulled in delivery retry
        # (``inbox``), API rate limiting (``usage``), poll backoff (``mcp``) and
        # workflow effect retry -- none of which are *start* retry.  Requiring a
        # start noun alongside keeps ``start_backoff_max_seconds`` and drops
        # those.  Standalone retry/backoff is still reported, by
        # :func:`scan_retry_backoff`, because whether §1.4 scopes to start is
        # hq-adjutant's call and not something this gate should decide silently.
        "backoff",
        "inflight",
    }
)

#: Tokens that carry the supervision meaning by themselves.
#:
#: ⚠️ ``quarantine`` is *not* perfectly unambiguous -- ``adapters/lark/scopes.py``
#: quarantines a corrupt file by renaming it.  It stays standalone anyway: one
#: known false positive costs a manifest line that says so, whereas requiring a
#: second word would drop the sites that really do own the capability (the
#: harness one was deleted with the 2026-09-02 retrying/failed split; the
#: actor-runtime quarantine for child actors remains).
_STANDALONE = frozenset({"quarantine", "quarantined"})

#: A limit measured in these is not a *time or attempt* budget, whatever else
#: the name says.  ``MAX_START_BYTES`` is a payload cap, not a start timeout.
_NOT_A_BUDGET = frozenset({"bytes", "byte", "kb", "mb", "kib", "mib", "size", "chars", "bits"})

_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")


def tokenize(identifier: str) -> frozenset[str]:
    """Split ``snake_case`` and ``CamelCase`` into lowercase word tokens."""

    return frozenset(match.group(0).lower() for match in _TOKEN.finditer(identifier))


def names_a_managed_concept(identifier: str) -> bool:
    """True when this identifier names one of the six managed capabilities."""

    tokens = tokenize(identifier)
    if tokens & _NOT_A_BUDGET:
        return False
    if tokens & _STANDALONE:
        return True
    return bool(tokens & _START_NOUNS) and bool(tokens & _LIMIT_NOUNS)


# ── findings ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, order=True)
class Finding:
    """One place outside core that names a managed capability itself."""

    path: str
    symbol: str
    kind: str
    line: int
    owner: str = ''

    @property
    def key(self) -> tuple[str, str]:
        """The identity the manifest is keyed on -- deliberately not the line.

        A manifest keyed on line numbers would go red on every unrelated edit
        above it, and the noise would get the gate switched off.
        """

        return (self.path, self.symbol)

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} {self.symbol!r}"


def _label(path: Path, src_root: Path) -> str:
    return str(Path("src/hyprial") / path.relative_to(src_root))


def _is_core(path: Path, src_root: Path) -> bool:
    relative = path.relative_to(src_root)
    return relative.parts[: len(CORE_ROOT.parts)] == CORE_ROOT.parts


def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> Iterator[str]:
    targets: Iterable[ast.expr]
    targets = (node.target,) if isinstance(node, ast.AnnAssign) else node.targets
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id


def _is_string_valued(node: ast.Assign | ast.AnnAssign) -> bool:
    value = node.value
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.arg]:
    yield from node.args.posonlyargs
    yield from node.args.args
    yield from node.args.kwonlyargs


def _scan_module_names(tree: ast.Module, label: str) -> Iterator[Finding]:
    """Every identifier position the rules look at, before any filtering.

    Kept separate from the rule so a second rule can reuse the positions
    without re-deriving them, and so "where we look" stays reviewable apart
    from "what we accept".
    """

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def scope(node: ast.AST, *, include_self: bool = False) -> str:
        owners = []
        current = node if include_self else parents.get(node)
        while current is not None:
            if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                owners.append(current.name)
            current = parents.get(current)
        return '.'.join(reversed(owners)) or '<module>'

    # Module-level constants.
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            # A string-valued constant is a name, not a budget:
            # ``DAEMON_START_TIMEOUT = "DAEMON_START_TIMEOUT"`` is the wire code
            # for the error, and the site that *reports* a start timeout is not
            # the site that implements one.  Checking the value is only possible
            # because we parse rather than grep.
            if _is_string_valued(node):
                continue
            for name in _assignment_targets(node):
                yield Finding(label, name, "module constant", node.lineno, '<module>')

    for node in ast.walk(tree):
        # Definitions whose own name is the capability: ``_start_timeout_seconds``
        # reads an env var, ``HarnessStartTimerElapsedCommand`` is a start timer.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            yield Finding(label, node.name, kind, node.lineno, scope(node, include_self=True))

        # Class-level annotations: dataclass fields and their kin.
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.AnnAssign):
                    for name in _assignment_targets(member):
                        yield Finding(label, name, "field", member.lineno, scope(member) + '.' + name)

        # Parameters.  Not only defaulted ones: ``startup_timeout: float`` with
        # no default is still this site owning its own budget, and the compound
        # rule already keeps bare ``timeout`` out.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in _parameters(node):
                yield Finding(label, argument.arg, "parameter", argument.lineno, scope(argument))


def _scan_module(tree: ast.Module, label: str) -> Iterator[Finding]:
    for finding in _scan_module_names(tree, label):
        if names_a_managed_concept(finding.symbol):
            yield finding


def scan_supervision_sites(src_root: Path) -> list[Finding]:
    """Every non-core place that names a managed supervision capability."""

    findings: set[Finding] = set()
    for path in sorted(src_root.rglob("*.py")):
        if _is_core(path, src_root):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        findings.update(_scan_module(tree, _label(path, src_root)))
    return sorted(findings)


def owned_symbols(src_root: Path) -> set[tuple[str, str]]:
    """The manifest-keyed view of :func:`scan_supervision_sites`."""

    return {SUPERVISION_CROSSWALK.get((finding.path, finding.symbol, finding.kind, finding.owner), finding.key)
            for finding in scan_supervision_sites(src_root)}


# ── retry/backoff that is not start retry ─────────────────────────────────

_RETRY_TOKENS = frozenset({"backoff", "retry", "retries"})


def scan_retry_backoff(src_root: Path) -> list[Finding]:
    """Retry/backoff outside core that the compound rule deliberately drops.

    §1.4 names "retry and backoff" as a managed capability, but every incident
    in §0 is about *starting* something, and the repository's other backoffs --
    message delivery, API rate limiting, poll intervals, workflow effect retry
    -- are unrelated mechanisms that merely share a word.

    Reported separately rather than folded in either direction, because the
    question "does §1.4 mean start retry, or all retry?" is the standard's to
    answer.  Folding them in would have inflated the migration by four
    subsystems; dropping them silently would have hidden the question.
    """

    findings: set[Finding] = set()
    for path in sorted(src_root.rglob("*.py")):
        if _is_core(path, src_root):
            continue
        label = _label(path, src_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for finding in _scan_module_names(tree, label):
            if names_a_managed_concept(finding.symbol):
                continue  # already counted by the compound rule
            if tokenize(finding.symbol) & _RETRY_TOKENS:
                findings.add(finding)
    return sorted(findings)


# ── the shape the name rule cannot see ────────────────────────────────────

#: Methods that park the caller.  ``get`` is here for ``Queue.get(timeout=…)``,
#: but ``dict.get(key)`` shares the name and is everywhere -- measured, it was
#: 54 of 69 hits, all noise.  So ``get`` and ``result`` are only ever counted
#: with an explicit ``timeout=`` keyword, which no dict access has.
_BLOCKING_WAITS = frozenset({"wait", "result", "join", "acquire", "select", "get"})
_KEYWORD_ONLY_WAITS = frozenset({"get", "result", "select"})


def _receiver_tokens(call: ast.Call) -> frozenset[str]:
    tokens: set[str] = set()
    current: ast.AST = call.func
    while isinstance(current, ast.Attribute):
        tokens |= tokenize(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        tokens |= tokenize(current.id)
    return frozenset(tokens)


def _has_timeout_argument(call: ast.Call, attribute: str) -> bool:
    """True when this call really carries a wait budget.

    ⚠️ An earlier version accepted "any positional argument", which made every
    ``payload.get("key")`` in the repository look like a blocking wait -- 54 of
    69 reported hits were dict lookups.  A rule that noisy reads as coverage
    while proving nothing, so the budget now has to be visible: a ``timeout=``
    keyword always counts, and a bare positional only for the methods where
    ``Event.wait(60.0)`` is the idiom.
    """

    if any(keyword.arg in {"timeout", "block"} for keyword in call.keywords):
        return True
    if attribute in _KEYWORD_ONLY_WAITS:
        return False
    return bool(call.args)


def scan_blocking_waits(src_root: Path) -> list[Finding]:
    """Blocking waits on something start-related, whose budget may be inlined.

    Deliberately separate from the name rule and never manifested.  A site can
    pass the name rule (it owns no constant) while still blocking the caller on
    a literal, which is capability #1 -- so folding these together would let
    one rule's silence stand in for the other's verdict.
    """

    findings: set[Finding] = set()
    for path in sorted(src_root.rglob("*.py")):
        if _is_core(path, src_root):
            continue
        label = _label(path, src_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for parent in ast.walk(tree):
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            enclosing = tokenize(parent.name)
            for node in ast.walk(parent):
                if not isinstance(node, ast.Call):
                    continue
                attribute = getattr(node.func, "attr", None)
                if attribute not in _BLOCKING_WAITS:
                    continue
                if not _has_timeout_argument(node, attribute):
                    continue
                # Scope: the thing being waited on, or the function doing the
                # waiting, must be about readiness.  Without this the rule
                # matches every queue read in the repository.
                context = _receiver_tokens(node) | enclosing
                if context & _START_NOUNS:
                    findings.add(
                        Finding(label, f"{parent.name}/{attribute}", "blocking wait", node.lineno)
                    )
    return sorted(findings)


# ── manifest: exact, bidirectional, shrinking-only ────────────────────────


def validate_supervision_manifest(src_root: Path, manifest_path: Path) -> list[str]:
    """Compare the manifest against reality in both directions.

    Both directions are errors and they mean opposite things:

    * a hit with no entry -- somebody wrote the shape a fourth time
    * an entry with no hit -- a site was migrated and its exemption is now
      dead weight, which is the "no dead code" half of the requirement

    ⚠️ The second direction has a trap worth naming, because its obvious remedy
    is wrong in one case.  If the *scanner* loses sight of a site that is still
    there, the failure looks identical to a completed migration, and deleting
    the entry -- what the message asks for -- leaves that site unguarded and
    the gate green.  So an entry may only be deleted once the site is confirmed
    gone by reading it, not because this said so.
    """

    raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return ["manifest schema_version must be exactly 1"]
    if raw.get("policy") != "shrinking-only":
        errors.append("manifest policy must be exactly 'shrinking-only'")

    entries = raw.get("exemptions")
    if not isinstance(entries, list):
        return errors + ["manifest exemptions must be a list"]

    manifested: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"exemptions[{index}] must be an object")
            continue
        required = (
            entry.get("path"),
            entry.get("symbol"),
            entry.get("reason"),
            entry.get("owner"),
            entry.get("card"),
        )
        if not all(isinstance(value, str) and value.strip() for value in required):
            errors.append(
                f"exemptions[{index}] needs non-empty path, symbol, reason, owner, card"
            )
            continue
        key = (entry["path"], entry["symbol"])
        if key in manifested:
            errors.append(f"duplicate manifest exemption: {key[0]}:{key[1]}")
        manifested.add(key)

    findings = scan_supervision_sites(src_root)
    actual = {SUPERVISION_CROSSWALK.get((finding.path, finding.symbol, finding.kind, finding.owner), finding.key)
              for finding in findings}
    guarded = {(path, symbol) for path, symbol, _kind, _owner in SUPERVISION_CROSSWALK}
    trees: dict[str, ast.Module] = {}
    native_keys = {(finding.path, finding.symbol, finding.kind, finding.owner) for finding in findings}
    for native, canonical in SUPERVISION_CROSSWALK.items():
        if canonical in manifested and native not in native_keys:
            errors.append(f'absent exact supervision migration site: {native}')
    for finding in findings:
        if finding.key not in guarded:
            continue
        native = (finding.path, finding.symbol, finding.kind, finding.owner)
        if native not in SUPERVISION_CROSSWALK:
            errors.append(f'unmapped supervision migration identity: {native}')
        else:
            tree = trees.setdefault(finding.path, ast.parse(
                (src_root.parents[1] / finding.path).read_text(encoding='utf-8')))
            if lexical_definitions(tree)[finding.owner] != 1:
                errors.append(f'non-unique supervision migration definition: {native}')
    for path, symbol in sorted(actual - manifested):
        errors.append(
            f"unmanifested self-built supervision: {path}:{symbol} -- compose the "
            "actor_runtime capability instead, or manifest it with an owner and card"
        )
    for path, symbol in sorted(manifested - actual):
        errors.append(
            f"stale supervision exemption must shrink now: {path}:{symbol} -- "
            "confirm the site is really gone before deleting this entry"
        )
    return errors
