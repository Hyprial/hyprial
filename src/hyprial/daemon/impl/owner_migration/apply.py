"""Owner migration execution: apply plans, detect previous owners and run the startup migration."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import sqlite3
from pathlib import Path
from hyprial.kernel import ipc_errors

from .planning import (
    build_plan,
)
from .vocabulary import (
    MigrationPlan,
    OwnerMigrationAborted,
    OwnerMigrationCustodyConflict,
    PendingWrite,
    Unclassified,
    _table_exists,
)


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


class OwnerMigrationCustodyUnreadable(RuntimeError):
    """The custody state could not be read, so the rewrite refuses to guess.

    ``sqlite3.OperationalError`` covers far more than "the table does not
    exist yet": ``database is locked``, ``disk I/O error``, a file that is
    not a database at all.  Counting any of those as zero grants would
    leave the gate silently open while live custody rides the rewrite
    (hyprial-developer, #513 comment 12723: 读不到 ≠ 没有).  Only a table
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
    value (every other occurrence is embedded in an address). Only native
    agents describe this node's owner: hosted rows belong to visitors and
    must never trigger a host alias rewrite or a multiple-owner refusal.

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
            columns = {row[1] for row in db.execute("PRAGMA table_info(agents)")}
            native_filter = " WHERE hosted_by IS NULL" if "hosted_by" in columns else ""
            rows = db.execute("SELECT DISTINCT owner FROM agents" + native_filter).fetchall()
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
