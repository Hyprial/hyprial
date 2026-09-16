"""One-time rewrite of the owner segment after identity moved off the host login.

``resolve_node_owner`` used to end in ``getpass.getuser()``, so the owner
segment of every ``agent:<owner>:<machine>:<actor>`` this node minted was the
*host login*.  Owner is a user identity now — one value across every machine a
user owns, with ``<machine>`` distinguishing the nodes — so the stored
addresses have to be rewritten once.

⛔ This is deliberately NOT a substring replace.  On the machine this was
measured on, the literal old owner (``h2oslabs``) appears with **four**
different meanings, and two of them must never be touched:

===========================  =======  ==========================================
meaning                      cells    treatment
===========================  =======  ==========================================
owner segment of an address   25 196  rewrite (delimiter-anchored)
filesystem path               293     leave alone — ``/Users/h2oslabs/…``
this node's FORMER name       ~209    leave alone — ⛔ this node was RENAMED
                                      on 2026-08-17; its own historical rows
                                      spell it ``h2oslabsmac-studio.orkhon-…``,
                                      so the old owner is a PREFIX of a
                                      machine segment
both in one cell              1 022   rewrite the address, keep the path
===========================  =======  ==========================================

A naive ``REPLACE(col, old, new)`` corrupts filesystem paths and falsifies
this node's own record of what it used to be called.  Anchoring on the
delimiters makes both impossible: ``agent:h2oslabs:`` cannot match
``:h2oslabsmac-studio:``.

⛔ **Correction (2026-09-04).**  Earlier revisions of this module said
``h2oslabsmac-studio`` was a *peer* and that rewriting it would sever routes to
another node.  That was wrong — it is this machine's own pre-rename name (the
old and new names never coexist in the log: old rows stop 2026-08-17T03:04Z,
new rows start 03:19Z).  ⭐ The treatment is unchanged and the tests are
untouched; only the reason was wrong.  Keeping a correct assertion attached to
a wrong reason is how the next reader "simplifies" it away.

## Why shape-driven instead of a column whitelist

The six databases hold 244 text columns.  A hand-written ``(table, column)``
list of that size fails in the worst possible direction: a column that is
missing from the list is **silently not rewritten**, the migration reports
success, and stale addresses stay behind with nothing to surface them.  It
also cannot really be reviewed — and column-level intuition is demonstrably
unreliable here (``dlq.owner`` looks like an owner field; it actually stores
a machine name).

So the columns come from the schema, and the *values* are classified:

1. anchored rewrite of the four multi-segment address prefixes, bounded
   rewrite of the two-segment ``user:<owner>`` form, plus an exact bare-value
   match (``agents.owner`` holds a naked ``h2oslabs``), then
2. ⭐ anything still containing the old owner must match one of
   :data:`ALLOWED_RESIDUALS`, or the migration **aborts**.

⇒ There is no third outcome.  A value is rewritten, or it is a known-legitimate
residual, or the migration stops and names it.  "Silently skipped" is not
reachable.

⚠️ :data:`ALLOWED_RESIDUALS` is **data, not branches**, and it was derived from
one machine.  Another deployment may hold a third legitimate residual; that is
exactly the case fail-closed exists for — it halts and reports, and someone
adds a row here rather than the migration guessing.

## Two phases, and why

Classification runs over everything **before any write happens**.  A migration
that rewrote as it scanned and then aborted half-way would leave the state
neither old nor new, which is the one outcome no operator can reason about.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

#: Address forms whose second segment is the owner.  Both delimiters matter:
#: the trailing colon is what stops ``agent:h2oslabs:`` from matching the
#: machine name ``h2oslabsmac-studio``.
ADDRESS_PREFIXES = ("agent:", "adapter:", "channel:", "route:")

#: The complete durable input set.  Preview and apply intentionally import
#: these names instead of maintaining parallel inventories.
MIGRATION_DATABASES = (
    "agents.sqlite3",
    "adapters.sqlite3",
    "inbox.sqlite3",
    "lifecycle-operations.sqlite3",
    "routines.sqlite3",
    "workflows.sqlite3",
)
MIGRATION_TEXT_FILES = ("desired-state.json.v1", "users.json")

#: A ``user:<owner>`` URI has no trailing colon, so its right boundary must be
#: stated directly.  Python ``\w`` supplies Unicode alphanumerics + underscore;
#: dot, at-sign, and hyphen complete the owner spellings accepted in practice
#: (including Casdoor email usernames).  A following character in this set
#: means the match is only a prefix of a different owner and must not move.
USER_IDENTIFIER_EXTRA_CHARACTERS = ".@-"


@dataclass(frozen=True, slots=True)
class ResidualForm:
    """A spelling of the old owner that is legitimately NOT an owner segment.

    ``template`` is formatted with ``old=<old owner>``; a value keeping the old
    owner is accepted when any template's rendering occurs in it.
    """

    name: str
    template: str
    why: str

    def matches(self, value: str, old: str) -> bool:
        return self.template.format(old=old) in value


#: ⚠️ DATA, not branches — see the module docstring.  Add a row when another
#: deployment aborts on a residual that is genuinely not an owner segment.
ALLOWED_RESIDUALS: tuple[ResidualForm, ...] = (
    ResidualForm(
        name="home-directory path",
        template="/Users/{old}",
        why=(
            "the old owner is also the host login, so it names the operator's "
            "home directory; agents.cwd is 100% this form"
        ),
    ),
    ResidualForm(
        name="this node's former machine name",
        template="{old}mac-studio",
        why=(
            "this node was renamed on 2026-08-17 and its own history still "
            "spells it h2oslabsmac-studio.orkhon-bee.ts.net -- the old owner "
            "is a PREFIX of a machine segment, and rewriting it would falsify "
            "the record of what this machine was called at the time"
        ),
    ),
    ResidualForm(
        name="legacy connector / Lark app name under the <owner>-h2b-<purpose> convention",
        template="{old}-h2b",
        why=(
            "the E2E Lark app is named allenwoods-h2b-e2e and its bot display "
            "name is the same string truncated by Lark (allenwoods-h2b-e2); "
            "adapters.sqlite3 identities.adapter / display_name carry them "
            "(7 cells, production rehearsal 2026-09-13).  The owner login "
            "is a naming convention inside an EXTERNAL identifier: the app "
            "name belongs to Lark, and the connector name is the key behind "
            "every adapter:lark:<name> address in routes, the delivery ledger "
            "and desired state -- renaming it would sever those references "
            "and could not rename the Lark app anyway.  Kept, not rewritten."
        ),
    ),
    ResidualForm(
        name="connector / Lark app name under the <owner>-hyprial-<purpose> convention",
        template="{old}-hyprial",
        why=(
            "post-rename external app and connector names may use the hyprial "
            "convention; they have the same external-identity semantics as the "
            "legacy h2b spelling.  Keeping this exact second form avoids making "
            "such state unclassifiable without widening the gate to `{old}-`."
        ),
    ),
)


#: ⭐ JSON keys whose value is legitimately the HOST LOGIN, not the user
#: identity — so a bare ``old`` under one of these must survive the rewrite.
#: ``users.json`` already stores the distinction this whole change is about::
#:
#:     owner      allenwoods   ← user identity   (rewritten)
#:     ownerKey   h2oslabs     ← host login      (kept)
#:     loginName  h2oslabs     ← host login      (kept)
#:
#: Keeping them is spec item 5: they stay as host-side lookup keys and #335's
#: ``user:<x>`` resolution still reads them.
#:
#: ⚠️ Data, not branches — same contract as :data:`ALLOWED_RESIDUALS`.  This is
#: matched on the PARSED document rather than on text so it does not depend on
#: how the file happens to be spaced.
HOST_LOGIN_JSON_KEYS = ("ownerKey", "loginName")


class OwnerMigrationAborted(RuntimeError):
    """Classification found a value it cannot account for; nothing was written."""

    def __init__(self, unclassified: tuple[Unclassified, ...]) -> None:
        self.unclassified = unclassified
        lines = "\n".join(
            f"  {item.source}: {item.column} = {item.sample!r}"
            for item in unclassified[:20]
        )
        more = "" if len(unclassified) <= 20 else f"\n  … and {len(unclassified) - 20} more"
        super().__init__(
            "owner migration aborted before writing anything: "
            f"{len(unclassified)} value(s) still contain the old owner after the "
            "anchored rewrite and match no allowed residual form.\n"
            f"{lines}{more}\n"
            "Each is either a new address spelling (extend ADDRESS_PREFIXES) or a "
            "new legitimate residual (add a ResidualForm). Do not widen the "
            "rewrite to make this pass."
        )


@dataclass(frozen=True, slots=True)
class Unclassified:
    source: str
    column: str
    sample: str


@dataclass(frozen=True, slots=True)
class PendingWrite:
    source: str
    table: str
    column: str
    rowid: int
    new_value: str


@dataclass
class MigrationPlan:
    """What the rewrite would do.  Built entirely before anything is written."""

    writes: list[PendingWrite] = field(default_factory=list)
    unclassified: list[Unclassified] = field(default_factory=list)
    files: dict[Path, str] = field(default_factory=dict)
    scanned_tables: int = 0
    scanned_columns: int = 0
    matched_cells: int = 0
    skipped_archival_columns: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.writes or self.files)


#: ⭐ JSON keys whose value IS the user identity, wherever they are nested.
#: Found by the rehearsal: ``lifecycle_resources.payload`` stores a serialized
#: agent record, so the same bare owner that ``agents.owner`` holds in a column
#: also appears as ``"owner": "<old>"`` *inside* a JSON blob — reachable by
#: neither the anchored rewrite nor the whole-cell exact match.
#:
#: ⚠️ Deliberately does NOT include ``ownerKey``/``loginName``: those are the
#: host login and must survive (:data:`HOST_LOGIN_JSON_KEYS`).  The two lists
#: are the same distinction this whole change exists to draw, applied inside
#: documents instead of across columns.
OWNER_JSON_KEYS = ("owner",)

#: ⭐ Columns that record what someone WROTE, not where anything is addressed.
#: hq-adjutant's ruling (2026-09-04), the same boundary as #351's "peer inbox
#: history is not migrated": ``runs`` holds completed run records, while live
#: routing lives in ``targets.target``.  Rewriting run text — which contains
#: prose quoting the old owner as an observation — would falsify a record of
#: what someone saw at the time.
#:
#: ⚠️ An EXCLUSION list, so its failure mode stays loud: a historical column
#: missing from here does not get silently skipped, it aborts like any other
#: unaccounted value.  A listed column is a completed record and is skipped
#: byte-for-byte, including any address quoted inside it.
ARCHIVAL_COLUMNS = (
    ("runs", "yaml_text"),
    ("runs", "report_text"),
    ("targets", "reply_excerpt"),
)


def _rewrite_json_owner_keys(value: str, old: str, new: str) -> str:
    """Rewrite ``"owner": "<old>"`` wherever it is nested inside a JSON value.

    Re-serialises only when something actually changed, so a value that needs
    no rewrite keeps its bytes exactly.
    """

    try:
        document = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    changed = False

    def walk(node: object) -> None:
        nonlocal changed
        if isinstance(node, dict):
            for key, child in node.items():
                if key in OWNER_JSON_KEYS and child == old:
                    node[key] = new
                    changed = True
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(document)
    if not changed:
        return value
    return json.dumps(document, ensure_ascii=False)


def rewrite_value(value: str, old: str, new: str) -> str:
    """Apply every anchored owner rewrite to one value.

    ⛔ Never a bare ``value.replace(old, new)``: that corrupts ``/Users/<old>``
    paths and rewrites a machine name that merely starts with ``<old>``.
    """

    if value == old:
        # A naked owner column (``agents.owner``), not an address.
        return new
    for prefix in ADDRESS_PREFIXES:
        value = value.replace(f"{prefix}{old}:", f"{prefix}{new}:")
    value = re.sub(
        rf"user:{re.escape(old)}(?![\w.{re.escape(USER_IDENTIFIER_EXTRA_CHARACTERS)}])",
        lambda _match: f"user:{new}",
        value,
    )
    if old in value:
        value = _rewrite_json_owner_keys(value, old, new)
    return value



def _hit_context(value: str, old: str, width: int = 70) -> str:
    """The neighbourhood of the first unaccounted hit, not the value's start.

    ⚠️ Earned in the first rehearsal: the report printed ``value[:120]``, and
    for a long JSON payload or a page of YAML that window does not contain the
    match at all -- every one of the four aborts needed a second script before
    anyone could see what the problem was.  A stop that makes you re-investigate
    before you can act has spent half of what stopping bought you.
    """

    index = value.find(old)
    if index < 0:
        return value[:120]
    window = value[max(0, index - width) : index + width]
    return window.replace("\n", " / ")


def _is_accounted_for(value: str, old: str) -> bool:
    """True when a value still holding ``old`` is a known-legitimate residual."""

    return any(form.matches(value, old) for form in ALLOWED_RESIDUALS)


def _text_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1]
        for row in db.execute(f'PRAGMA table_info("{table}")')
        if (row[2] or "").upper() in ("TEXT", "") or "CHAR" in (row[2] or "").upper()
    ]


def plan_database(path: Path, old: str, new: str, plan: MigrationPlan) -> None:
    """Classify one database.  Reads only — no statement here writes."""

    if not path.exists():
        return
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.text_factory = lambda raw: raw.decode("utf-8", "replace")
    try:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        plan.scanned_tables += len(tables)
        for table in tables:
            columns = _text_columns(db, table)
            plan.scanned_columns += len(columns)
            for column in columns:
                if (table, column) in ARCHIVAL_COLUMNS:
                    # Completed records are not migration inputs.  Skipped by
                    # name and only by name, so an unlisted column still aborts
                    # rather than passing quietly.
                    plan.skipped_archival_columns.append(
                        f"{path.name}:{table}.{column}"
                    )
                    continue
                try:
                    rows = db.execute(
                        f'SELECT rowid, "{column}" FROM "{table}" '
                        f'WHERE CAST("{column}" AS TEXT) LIKE ?',
                        (f"%{old}%",),
                    ).fetchall()
                except sqlite3.OperationalError:
                    # WITHOUT ROWID tables have no rowid to address; such a
                    # table cannot be rewritten row-wise, so it must not be
                    # silently skipped either.
                    sample = db.execute(
                        f'SELECT "{column}" FROM "{table}" '
                        f'WHERE CAST("{column}" AS TEXT) LIKE ? LIMIT 1',
                        (f"%{old}%",),
                    ).fetchone()
                    if sample is not None:
                        plan.unclassified.append(
                            Unclassified(
                                str(path.name),
                                f"{table}.{column} (no rowid)",
                                str(sample[0])[:120],
                            )
                        )
                    continue
                plan.matched_cells += len(rows)
                for rowid, value in rows:
                    if not isinstance(value, str):
                        continue
                    rewritten = rewrite_value(value, old, new)
                    if old in rewritten and not _is_accounted_for(rewritten, old):
                        plan.unclassified.append(
                            Unclassified(
                                str(path.name),
                                f"{table}.{column}",
                                _hit_context(rewritten, old),
                            )
                        )
                        continue
                    if rewritten != value:
                        plan.writes.append(
                            PendingWrite(
                                str(path), table, column, int(rowid), rewritten
                            )
                        )
    finally:
        db.close()


def plan_text_file(path: Path, old: str, new: str, plan: MigrationPlan) -> None:
    """Classify a JSON document by its text.

    ⭐ Same rule as the databases, deliberately: ``desired-state.json.v1`` and
    ``users.json`` carry the same address spellings, and giving them a second
    mechanism would be a second thing to keep correct.
    """

    if not path.exists():
        return
    original = path.read_text(encoding="utf-8")
    rewritten = rewrite_value(original, old, new)
    if old in rewritten:
        for location, value in _unaccounted_json_values(rewritten, old):
            plan.unclassified.append(
                Unclassified(str(path.name), location, value[:120])
            )
        if any(item.source == path.name for item in plan.unclassified):
            return
    if rewritten != original:
        plan.files[path] = rewritten


def _unaccounted_json_values(
    text: str, old: str
) -> list[tuple[str, str]]:
    """Every place a rewritten document still holds ``old`` without a reason.

    Walks the PARSED document so the check does not depend on the file's
    whitespace.  A remaining occurrence is accounted for when it is either a
    known residual form (a path, a machine segment) or a bare value under
    one of :data:`HOST_LOGIN_JSON_KEYS`.
    """

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        # Not JSON: fall back to the text rule, reporting the neighbourhood of
        # the first unaccounted occurrence.
        if _is_accounted_for(text, old):
            return []
        index = text.index(old)
        return [("<document text>", text[max(0, index - 60) : index + 60])]

    found: list[tuple[str, str]] = []

    def walk(node: object, path: str, key: str | None) -> None:
        if isinstance(node, str):
            if old not in node:
                return
            if key in HOST_LOGIN_JSON_KEYS and node == old:
                return  # the host login, deliberately kept
            if _is_accounted_for(node, old):
                return
            found.append((path or "<root>", node))
        elif isinstance(node, dict):
            for name, value in node.items():
                walk(value, f"{path}.{name}" if path else str(name), str(name))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]", key)

    walk(document, "", None)
    return found


def build_plan(
    *, state_dir: Path, hyprial_home: Path, old: str, new: str
) -> MigrationPlan:
    """Classify everything.  ⭐ Nothing is written by this function."""

    plan = MigrationPlan()
    for name in MIGRATION_DATABASES:
        plan_database(state_dir / name, old, new, plan)
    for name in MIGRATION_TEXT_FILES:
        plan_text_file(state_dir / name, old, new, plan)
    return plan


def apply_plan(plan: MigrationPlan) -> int:
    """Write a fully-classified plan.  One transaction per database."""

    by_database: dict[str, list[PendingWrite]] = {}
    for write in plan.writes:
        by_database.setdefault(write.source, []).append(write)
    applied = 0
    for source, writes in by_database.items():
        db = sqlite3.connect(source)
        try:
            with db:
                for write in writes:
                    db.execute(
                        f'UPDATE "{write.table}" SET "{write.column}" = ? '
                        "WHERE rowid = ?",
                        (write.new_value, write.rowid),
                    )
                    applied += 1
        finally:
            db.close()
    for path, text in plan.files.items():
        temporary = path.with_suffix(path.suffix + ".owner-migration.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        applied += 1
    return applied


def detect_previous_owner(state_dir: Path, current: str) -> str | None:
    """The owner this node's state was written under, or ``None`` if current.

    ⭐ Derived from the data, not from a stored "previous owner" setting.  A
    setting would be one more thing that can be wrong, and it would have to be
    written by the very version that did not know it needed to.

    ``agents.owner`` is the read: it is the one column holding a **bare** owner
    value (every other occurrence is embedded in an address), and every agent
    this node registered carries it.

    ⚠️ Fail-closed, same as the rewrite: more than one distinct foreign owner
    means the state was written under several identities and this function
    will not pick one.
    """

    path = state_dir / "agents.sqlite3"
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            rows = db.execute("SELECT DISTINCT owner FROM agents").fetchall()
        except sqlite3.OperationalError:
            return None
    finally:
        db.close()
    owners = {
        str(row[0]) for row in rows if row[0] and str(row[0]) != current
    }
    if not owners:
        return None
    if len(owners) > 1:
        raise OwnerMigrationAborted(
            tuple(
                Unclassified("agents.sqlite3", "agents.owner", owner)
                for owner in sorted(owners)
            )
        )
    return owners.pop()


def migrate_owner_if_needed(*, state_dir: Path, hyprial_home: Path, owner: str) -> int:
    """Startup entry point: rewrite the owner segment once, if state predates it.

    Idempotent by construction — after a successful run no bare foreign owner
    remains in ``agents.owner``, so :func:`detect_previous_owner` returns
    ``None`` and this does nothing on every subsequent start.

    ⚠️ An abort propagates and the daemon does not start.  That is deliberate
    and matches the identity rule it belongs to: a daemon whose state it cannot
    account for should refuse to run rather than serve half-rewritten
    addresses.  The exception names the (db, table, column, sample) to look at.
    """

    previous = detect_previous_owner(Path(state_dir), owner)
    if previous is None:
        return 0
    return migrate_owner(
        state_dir=Path(state_dir), hyprial_home=Path(hyprial_home), old=previous, new=owner
    )


def migrate_owner(
    *, state_dir: Path, hyprial_home: Path, old: str, new: str
) -> int:
    """Rewrite ``old`` to ``new`` everywhere it names this node's owner.

    Idempotent: a second run finds nothing to rewrite (the anchored forms no
    longer contain ``old``) and writes nothing.

    :raises OwnerMigrationAborted: classification could not account for a
        value.  ⭐ Raised **before any write**, so the state is still entirely
        pre-migration and the operator can look at the reported sample.
    """

    if old == new:
        return 0
    plan = build_plan(state_dir=state_dir, hyprial_home=hyprial_home, old=old, new=new)
    if plan.unclassified:
        raise OwnerMigrationAborted(tuple(plan.unclassified))
    return apply_plan(plan)
