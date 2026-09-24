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

from hyprial.contracts import ipc_errors

#: Address forms whose second segment is the owner.  Both delimiters matter:
#: the trailing colon is what stops ``agent:h2oslabs:`` from matching the
#: machine name ``h2oslabsmac-studio``.
ADDRESS_PREFIXES = ("agent:", "adapter:", "channel:", "route:")

#: The complete durable input set.  Preview and apply intentionally import
#: these names instead of maintaining parallel inventories.
#:
#: ``pac-graph.sqlite3`` (P1b B0, Allen 2026-09-18): PAC authorizes by exact
#: principal-URI equality (``user:<owner>`` / ``agent:<owner>:<machine>:
#: <actor>``) and a stale owner spelling locks a graph out of flag/close/stop
#: with no error anywhere — the task just runs to timeout.  Every
#: owner-bearing column participates through the same shape-driven scan as
#: the other databases: ``graphs.created_by`` / ``activated_by`` /
#: ``closed_by``, ``nodes.owner`` / ``flag_set_by``, ``flag_events.actor``,
#: principal URIs inside ``journal.data_json`` envelopes, and
#: ``notifications.recipient`` / ``sender``.  Fenced the same way as the rest:
#: the migration only runs under the daemon state-ownership fence
#: (``daemon run`` acquires ``daemon.lock`` BEFORE constructing
#: DaemonApplication), so a live previous generation — including PAC's
#: resident clock writer — blocks startup and thus the migration.
MIGRATION_DATABASES = (
    "agents.sqlite3",
    "adapters.sqlite3",
    "inbox.sqlite3",
    "lifecycle-operations.sqlite3",
    "routines.sqlite3",
    "workflows.sqlite3",
    "pac-graph.sqlite3",
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
#: (``build_plan`` additionally appends the per-run hyprial-home path-prefix
#: residual from :func:`_home_root_residual` — same class as the first row
#: below, but machine-specific, so it is constructed rather than listed.)
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


class OwnerMigrationCustodyConflict(RuntimeError):
    """The startup auto-rewrite refused: live per-agent grants would ride it.

    ``migrate_owner_if_needed`` models exactly one legal transition — the
    benign host-login → user-identity alias rewrite, where the same human
    keeps their agents, homes and grants under the new spelling.  A *real*
    account or hosting switch must not inherit that authority (the agent-home
    design: 账号切换不因短名相同沿用凭据授权), and an owner-string rewrite
    cannot tell the two apart.  When live secret grants are present the
    daemon refuses to start until the operator chooses explicitly; nothing
    has been written.  The account-switch re-authorization fence itself is
    deferred to #493 (design b9e2ccae §7) — this gate is what keeps that
    deferred wiring from being an open inheritance path in the meantime.

    ⚠️ Scope note (why ``grants`` gates and ``homes`` only reports): every
    ``session.register`` auto-creates an agent, and P1a provisions that
    agent's home — so active ``agent-home`` resources exist on any node with
    registered sessions, including ones with zero credential custody.  A
    home without a grant carries no delivery authority (the resolver needs
    a named grant), and alias rewrites *must* keep homes (T01: the directory
    key survives an owner-alias rewrite).  Gating on homes would therefore
    brick the #352 alias rewrite for every realistic state while closing no
    credential hole; homes are counted and reported for diagnosis instead.

    ⚠️ Honesty note (h2b-developer, #513 comment 12723 附条件 3 and comment
    12899 ②): the refusal message may only name commands that actually
    exist, and a destructive one only with its cost stated in the same
    breath.  The first recourse it names is **non-destructive**: switch
    the login identity back to the previous spelling with
    ``hyprial login --switch-account`` (a switch requires the daemon
    stopped — which it is, since this refusal *is* the failed start) and
    bring the daemon up under that spelling, where no rewrite triggers
    and nothing is deleted.  It also states the known blind spot of that
    recourse (h2b-developer comment on head 89063212): when the previous
    spelling came from the host login (#352 alias rewrite) there is no
    account to switch back to, and this build has no non-destructive CLI
    path for that case — the message says so instead of letting the
    operator discover it after trying.  ``revoke_secret_grant`` (drops
    the grants, keeps the agents) and the confirmed alias path
    (``migrate_owner``) are internal API with **no CLI entry point** in
    this build — the message says so, points the revoke entry at its
    follow-up and the account-switch re-authorization wiring at #493,
    and invents no commands.  ``hyprial agent destroy`` is named only as
    a **last resort**, ordered last, with its consequences spelled out
    (irreversible; the cascade deletes the agent record with its pins
    and grants; historical addressees stop resolving; a same-name
    rebuild does not restore the destroyed history).
    ``tests/test_agent_home_dirs.py`` pins that every ``hyprial …``
    invocation the message names — flags included — is registered in the
    real CLI tree, that ``destroy`` is ordered last, that naming it
    obliges the consequences clause, and that the host-login blind spot
    is stated.

    ⭐ Carries ``.code``/``.data`` (``OWNER_MIGRATION_CUSTODY_CONFLICT``)
    so the refused ``daemon run`` child emits a machine-readable code on
    its startup log and the launcher / login orchestration can report
    the **named** switch outcome instead of a plain startup failure
    (infra-op acceptance on #513: S3 merged first, so #513 wires its
    exception codes into S3's failure_data).
    """

    def __init__(self, *, old: str, new: str, grants: int, homes: int) -> None:
        self.old = old
        self.new = new
        self.grants = grants
        self.homes = homes
        self.code = ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT
        self.data = {
            "old": old,
            "new": new,
            "grants": grants,
            "homes": homes,
        }
        signals = [f"{grants} agent_secret_grants row(s)"]
        if homes:
            signals.append(f"{homes} active agent-home lifecycle resource(s)")
        super().__init__(
            f"owner migration refused before writing anything: state under "
            f"{old!r} holds live per-agent secret custody ({'; '.join(signals)}). "
            "The startup rewrite only models the benign host-login alias "
            "change, where the same human keeps their grants; a real account "
            "switch must not inherit them. Nothing was written, and nothing "
            "needs to be deleted to get the daemon back. First recourse "
            "(non-destructive): switch the login identity back to the "
            f"previous spelling {old!r} — `hyprial login --switch-account` "
            "(a switch requires the daemon stopped; this refusal happened "
            "at startup, so it is) — then start under that spelling "
            "(`hyprial daemon run`, or the platform service): no rewrite "
            f"triggers under {old!r}. If the previous spelling came from "
            "the host login (an alias rewrite, not an account — there is "
            "no account to switch back to), this build has no "
            "non-destructive CLI path for it; see #493. Revoking only the "
            "grants while keeping "
            "the agents (`revoke_secret_grant`) and the confirmed alias "
            "path (`migrate_owner`) have no CLI entry point in this build — "
            "the revoke entry is a tracked follow-up, and the "
            "account-switch re-authorization wiring is #493. Last resort, "
            "only if you mean it: `hyprial agent destroy` on the agents "
            "holding grants — irreversible: deletes the agent record, its "
            "pins and grants, and discards its undelivered messages; "
            "historical messages to it will no longer resolve, and "
            "recreating the same name does not restore it."
        )


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
    # PAC record columns (P1b B0): records of what was written/decided at the
    # time, not addresses — rewriting them would falsify the v8 era report
    # that authorization refusals point at (schema_era.report_json), or break
    # the notifications byte-for-byte resend contract (text / plan_json,
    # store.py docstring).  Delivery ROUTING lives in
    # notifications.recipient/sender, which ARE rewritten.  Descriptive
    # columns (graphs.name, nodes.brief_ref, flag_reason_ref, reason_ref) are
    # deliberately NOT archived: a stale old-owner spelling there stops the
    # migration fail-closed like every other database, and a legitimate
    # residual gets classified rather than skipped.
    ("schema_era", "report_json"),
    ("notifications", "text"),
    ("notifications", "plan_json"),
    # The PAC journal is append-only BY TRIGGER (migrations.py installs
    # journal_no_update / journal_no_delete with RAISE(ABORT)); an UPDATE
    # rewrite is refused by the database itself ("PAC journal is
    # append-only").  PAC's own v8 era migration took the same stance by
    # design (§2.3: journal rows stay untouched; B1 appends nothing).  No
    # authorization surface compares journal rows — flag/close/stop compare
    # nodes.owner and graphs.created_by — so a stale actor string in the log
    # is a dated fact, not a lockout.  Flagged as a deviation from the B0
    # spec text ("journal actor fields participate in the rewrite") in the
    # PR and the receipt, with this trigger reading as the reason.
    ("journal", "data_json"),
    # Workflow definitions and request identities are immutable references,
    # not principals. Routing uses graphs.created_by and nodes.owner.
    ("workflow_graphs", "specification_ref"),
    ("workflow_graphs", "specification_digest"),
    ("workflow_nodes", "input_token"),
    ("workflow_nodes", "request_id"),
    ("workflow_deliveries", "message_id"),
    ("workflow_deliveries", "request_id"),
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


def _is_accounted_for(
    value: str, old: str, *, residuals: tuple[ResidualForm, ...] = ALLOWED_RESIDUALS
) -> bool:
    """True when a value still holding ``old`` is a known-legitimate residual."""

    return any(form.matches(value, old) for form in residuals)


def _home_root_residual(hyprial_home: Path) -> ResidualForm | None:
    """The home's own path prefix: a machine-local location, not an address.

    Earned on CI (task 15458, 2026-09-17): P1a's home provisioning stores the
    agent-home path in ``lifecycle_resources.payload``, and the owner
    spelling can appear inside that path as a coincidental substring of an
    unrelated directory segment — measured: pytest-xdist's ``popen-gw<N>``
    worker directories contain ``op``.  Same class as the ``/Users/{old}``
    residual above (a filesystem location is not an owner segment); this row
    is constructed per-run because the home path is machine-specific.

    The anchored rewrite still fires first on any real ``agent:<old>:`` /
    ``user:<old>`` spelling *inside* such a value — this row only forgives
    what remains after the rewrite.  Returns ``None`` for a degenerate home
    path (empty or ``/``), where prefix-matching would forgive everything.
    """

    text = str(hyprial_home)
    # Degenerate roots (empty, "/", and Path("")→".") add no row: a
    # one-character prefix would substring-match almost every value.
    if len(text) <= 1:
        return None
    # ``matches`` formats the template with ``old=`` — escape braces so a
    # home path containing ``{``/``}`` cannot turn into a format error or a
    # different string.
    return ResidualForm(
        name="hyprial-home path prefix",
        template=text.replace("{", "{{").replace("}", "}}"),
        why=(
            "the hyprial home is a machine-local directory tree; the owner "
            "spelling can appear inside its path only as a coincidental "
            "substring of unrelated path segments (measured: pytest-xdist "
            "popen-gw<N> worker directories contain 'op'), never as an "
            "owner segment. The anchored rewrite already handled real "
            "address spellings in the same value; this row forgives only "
            "the path remainder."
        ),
    )


def _text_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1]
        for row in db.execute(f'PRAGMA table_info("{table}")')
        if (row[2] or "").upper() in ("TEXT", "") or "CHAR" in (row[2] or "").upper()
    ]


def plan_database(
    path: Path,
    old: str,
    new: str,
    plan: MigrationPlan,
    *,
    residuals: tuple[ResidualForm, ...] = ALLOWED_RESIDUALS,
) -> None:
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
        if path.name == "workflows.sqlite3" and "pac_cutover" in tables and db.execute("SELECT 1 FROM pac_cutover WHERE id=1").fetchone():
            # A sealed legacy archive records historical identities. Rewriting
            # it would both falsify history and violate its write barrier.
            for table in tables:
                plan.skipped_archival_columns.extend(
                    f"{path.name}:{table}.{column}" for column in _text_columns(db, table)
                )
            return
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
                    if old in rewritten and not _is_accounted_for(
                        rewritten, old, residuals=residuals
                    ):
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


def plan_text_file(
    path: Path,
    old: str,
    new: str,
    plan: MigrationPlan,
    *,
    residuals: tuple[ResidualForm, ...] = ALLOWED_RESIDUALS,
) -> None:
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
        for location, value in _unaccounted_json_values(
            rewritten, old, residuals=residuals
        ):
            plan.unclassified.append(
                Unclassified(str(path.name), location, value[:120])
            )
        if any(item.source == path.name for item in plan.unclassified):
            return
    if rewritten != original:
        plan.files[path] = rewritten


def _unaccounted_json_values(
    text: str, old: str, *, residuals: tuple[ResidualForm, ...] = ALLOWED_RESIDUALS
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
        if _is_accounted_for(text, old, residuals=residuals):
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
            if _is_accounted_for(node, old, residuals=residuals):
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
    """Classify everything.  ⭐ Nothing is written by this function.

    The home's own path prefix is admitted as a per-run residual (see
    :func:`_home_root_residual`): the owner spelling inside it is a
    coincidental path substring, not an owner segment.  Fail-closed is
    untouched for every other shape — a ``None`` from the helper just means
    the residual table stays at its frozen membership.
    """

    home_root = _home_root_residual(hyprial_home)
    residuals = (
        ALLOWED_RESIDUALS + (home_root,) if home_root is not None else ALLOWED_RESIDUALS
    )
    plan = MigrationPlan()
    for name in MIGRATION_DATABASES:
        plan_database(state_dir / name, old, new, plan, residuals=residuals)
    for name in MIGRATION_TEXT_FILES:
        plan_text_file(state_dir / name, old, new, plan, residuals=residuals)
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


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """Whether ``table`` exists, read from the schema itself.

    The schema read fails under the same conditions as the data read (a
    locked or corrupt database refuses ``sqlite_master`` too), which is
    exactly what makes it a sound witness: a ``False`` return can only mean
    the table is absent, never that we could not look.
    """

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


class OwnerMigrationCustodyUnreadable(RuntimeError):
    """The custody state could not be read, so the rewrite refuses to guess.

    ``sqlite3.OperationalError`` covers far more than "the table does not
    exist yet": ``database is locked``, ``disk I/O error``, a file that is
    not a database at all.  Counting any of those as zero grants would
    leave the gate silently open while live custody rides the rewrite
    (h2b-developer, #513 comment 12723: 读不到 ≠ 没有).  Only a table
    *verified absent* from ``sqlite_master`` counts as zero — that database
    predates the custody entirely.  Everything else fails closed with the
    original error attached, and nothing has been written.

    ⭐ Carries ``.code``/``.data``
    (``OWNER_MIGRATION_CUSTODY_UNREADABLE``) for the same reason as
    :class:`OwnerMigrationCustodyConflict`: the refused ``daemon run``
    child emits the code on its startup log so the login orchestration
    can report the named switch outcome.
    """

    def __init__(self, *, database: str, table: str, error: Exception) -> None:
        self.database = database
        self.table = table
        self.error = error
        self.code = ipc_errors.OWNER_MIGRATION_CUSTODY_UNREADABLE
        self.data = {
            "database": database,
            "table": table,
            "errorType": type(error).__name__,
        }
        super().__init__(
            f"custody state unreadable, refusing to count it as zero "
            f"(读不到 ≠ 没有): {type(error).__name__} while reading "
            f"{table!r} in {database}: {error}. Way out: clear the cause — "
            "a lock that outlasts the read-only connection's busy-timeout "
            "wait, an I/O failure, or a file that is not a database must "
            "not silently re-open the owner-migration gate — then retry "
            "the start. Nothing was written."
        )


def _count_or_zero_if_absent(
    db: sqlite3.Connection, *, database: str, table: str, sql: str
) -> int:
    """Count rows; the only unreadable state that means zero is no table.

    Existence is established from ``sqlite_master`` *before* the count,
    not parsed from the error text: ``database is locked`` and
    ``no such table`` are both ``OperationalError``-shaped, so the message
    string cannot carry the distinction — the schema can.  Any failure
    besides a verified-absent table raises
    :class:`OwnerMigrationCustodyUnreadable`, and the rewrite that asked
    for the count fails closed instead of treating silence as zero.
    """

    try:
        if not _table_exists(db, table):
            return 0  # verified absent: this database predates the table
        return int(db.execute(sql).fetchone()[0])
    except sqlite3.Error as exc:
        raise OwnerMigrationCustodyUnreadable(
            database=database, table=table, error=exc
        ) from exc


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
            if not _table_exists(db, "agents"):
                return None  # verified absent: this state predates agents
            rows = db.execute("SELECT DISTINCT owner FROM agents").fetchall()
        except sqlite3.Error as exc:
            # 读不到 ≠ 没有 applies one level up as well: an unreadable
            # agents table must not masquerade as "no previous owner", or
            # the custody gate below is never reached at all.
            raise OwnerMigrationCustodyUnreadable(
                database=str(path), table="agents", error=exc
            ) from exc
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


def _live_custody_counts(state_dir: Path) -> tuple[int, int]:
    """Count the P1a custody signals that must not silently change owner.

    Read-only, and tolerant of pre-P1a databases in exactly one way: a
    table *verified absent* from the schema predates this custody entirely
    and counts as zero.  Anything unreadable — a lock, an I/O error, a file
    that is not a database — raises :class:`OwnerMigrationCustodyUnreadable`
    so the gate fails closed instead of silently opening (读不到 ≠ 没有,
    #513 comment 12723).  Only *active* home resources count — a revoked
    residue is already fenced and carries no delivery authority.
    """

    path = state_dir / "agents.sqlite3"
    if not path.exists():
        return (0, 0)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        grants = _count_or_zero_if_absent(
            db,
            database=str(path),
            table="agent_secret_grants",
            sql="SELECT count(*) FROM agent_secret_grants",
        )
        homes = _count_or_zero_if_absent(
            db,
            database=str(path),
            table="lifecycle_resources",
            sql=(
                "SELECT count(*) FROM lifecycle_resources "
                "WHERE resource_key LIKE 'agent-home:%' AND active = 1"
            ),
        )
    finally:
        db.close()
    return (grants, homes)


def migrate_owner_if_needed(*, state_dir: Path, hyprial_home: Path, owner: str) -> int:
    """Startup entry point: rewrite the owner segment once, if state predates it.

    Idempotent by construction — after a successful run no bare foreign owner
    remains in ``agents.owner``, so :func:`detect_previous_owner` returns
    ``None`` and this does nothing on every subsequent start.

    ⚠️ An abort propagates and the daemon does not start.  That is deliberate
    and matches the identity rule it belongs to: a daemon whose state it cannot
    account for should refuse to run rather than serve half-rewritten
    addresses.  The exception names the (db, table, column, sample) to look at.

    ⚠️ Custody fail-closed: a previous owner whose state holds live secret
    grants raises :class:`OwnerMigrationCustodyConflict` instead of
    rewriting.  The rewrite cannot distinguish a benign alias change from a
    real account switch, and the latter must not inherit credential
    authority by silence; the operator chooses explicitly.  Active agent
    homes do not gate (see the exception's scope note) but are reported in
    the refusal when a grant triggered it.

    ⚠️ Unreadable fail-closed: custody state that cannot be *read* — a
    locked database, an I/O error, a corrupt file — raises
    :class:`OwnerMigrationCustodyUnreadable` rather than counting as zero
    grants (读不到 ≠ 没有, #513 comment 12723).  Only tables verified
    absent from the schema count as zero, so an unreadable database can
    never silently re-open the gate.
    """

    previous = detect_previous_owner(Path(state_dir), owner)
    if previous is None:
        return 0
    grants, homes = _live_custody_counts(Path(state_dir))
    if grants:
        raise OwnerMigrationCustodyConflict(
            old=previous, new=owner, grants=grants, homes=homes
        )
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
