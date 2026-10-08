"""Owner migration vocabulary: residual forms, plans, custody errors and archival columns."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from hyprial.kernel import ipc_errors


ADDRESS_PREFIXES = ("agent:", "adapter:", "channel:", "route:")


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
        name="connector / Lark app name under the <owner>-hyprial-<purpose> convention",
        template="{old}-hyprial",
        why=(
            "the E2E Lark app is named allenwoods-hyprial-e2e and its bot "
            "display name is the same string truncated by Lark; adapters.sqlite3 "
            "identities.adapter / display_name carry them.  The owner login is a "
            "naming convention inside an EXTERNAL identifier: the app name "
            "belongs to Lark, and the connector name is the key behind every "
            "adapter:lark:<name> address in routes, the delivery ledger and "
            "desired state -- renaming it would sever those references.  Kept, "
            "not rewritten; the exact form avoids widening the gate to `{old}-`."
        ),
    ),
    ResidualForm(
        name="Linux home-directory path",
        template="/home/{old}",
        why=(
            "same class as /Users/{old}: the old owner is also the host login "
            "on Linux/WSL nodes, so it names a home directory in stored cwd "
            "values (a member's switch-account, 2026-09-26: 3 lifecycle "
            "payload cwds from when the node ran on WSL)"
        ),
    ),
    ResidualForm(
        name="squire adapter name under the <owner>-squire convention",
        template="{old}-squire",
        why=(
            "squire setup names the owner's Lark adapter f\"{owner}-squire\" "
            "(squire/setup.py); the name is the key behind every "
            "lark:<name> / adapter:lark:<name> / harness:lark:<name> row, the "
            "delivery ledger and pins.  External connector identity, same "
            "semantics as the app-name convention rows above; renaming it would sever "
            "history.  ~93 of 99 unclassified cells in the 2026-09-26 member "
            "switch.  Exact suffix, not `{old}-`."
        ),
    ),
    ResidualForm(
        name="manual provenance note",
        template="manual:{old}-confirmed-",
        why=(
            "adapters identities.source records who confirmed a binding as "
            "free text (`manual:<login>-confirmed-<date> ...`); it is a "
            "historical note, not an address (2 cells, 2026-09-26)"
        ),
    ),
)


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


class OwnerMigrationHostedConflict(RuntimeError):
    """An alias change would re-own a hosted agent or collide with its owner."""

    def __init__(self, *, old: str, new: str, actor: str, hosted_owner: str) -> None:
        self.code = ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT
        self.data = {"old": old, "new": new, "actor": actor, "hostedOwner": hosted_owner}
        remedy = (
            "Move or migrate the hosted agent away before renaming this host."
            if hosted_owner == old else
            "Choose a new owner that does not collide with a hosted agent."
        )
        super().__init__(
            f"owner migration {old!r} -> {new!r} refused before writing: "
            f"hosted agent {actor!r} belongs to {hosted_owner!r}. {remedy} "
            "The visitor's identity must not be rewritten or adopted. "
            "If configuration was changed manually, restore the previous "
            f"login configuration (owner {old!r}) and restart the daemon "
            "under that identity; the stored state has not been migrated."
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

    ⚠️ Honesty note (hyprial-developer, #513 comment 12723 附条件 3 and comment
    12899 ②): the refusal message may only name commands that actually
    exist, and a destructive one only with its cost stated in the same
    breath.  The first recourse it names is **non-destructive**: switch
    the login identity back to the previous spelling with
    ``hyprial login --switch-account`` (a switch requires the daemon
    stopped — which it is, since this refusal *is* the failed start) and
    bring the daemon up under that spelling, where no rewrite triggers
    and nothing is deleted.  It also states the known blind spot of that
    recourse (hyprial-developer comment on head 89063212): when the previous
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


OWNER_JSON_KEYS = ("owner",)


ARCHIVAL_COLUMNS = (
    # Capability journal values describe the original decision, not current
    # routing. Owner rename must not rewrite its attribution or scope.
    *(("agent_grant_journal", column) for column in (
        "actor", "entity_token", "action", "grant_id", "capability", "scope", "by", "note"
    )),
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
    ("workflow_graphs", "parent_graph_id"),
    ("workflow_graphs", "parent_node_id"),
    ("workflow_nodes", "node_kind"),
    *(("workflow_expansions", column) for column in (
        "parent_graph_id", "parent_node_id", "child_graph_id", "planner_node_id",
        "planner_request_id", "expansion_digest", "limits_json",
    )),
    *(("workflow_outcome_receipts", column) for column in (
        "graph_id", "node_id", "expansion_digest",
    )),
    ("remote_workflow_outbox", "expansion_text"),
    ("remote_workflow_outbox", "expansion_digest"),
    *(("workflow_worktrees", column) for column in (
        "graph_id", "node_id", "actor_node", "plan_json", "state", "reason",
    )),
)


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
