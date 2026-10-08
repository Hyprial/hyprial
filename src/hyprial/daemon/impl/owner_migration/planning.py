"""Owner migration planning: scan databases/text files and account for every rewritten cell."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path

from hyprial.kernel import legacy_user_uri

from .vocabulary import (
    ADDRESS_PREFIXES,
    ALLOWED_RESIDUALS,
    ARCHIVAL_COLUMNS,
    HOST_LOGIN_JSON_KEYS,
    MIGRATION_DATABASES,
    MIGRATION_TEXT_FILES,
    MigrationPlan,
    OWNER_JSON_KEYS,
    OwnerMigrationHostedConflict,
    PendingWrite,
    ResidualForm,
    USER_IDENTIFIER_EXTRA_CHARACTERS,
    Unclassified,
    _table_exists,
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
        rf"{re.escape(legacy_user_uri(old))}(?![\w.{re.escape(USER_IDENTIFIER_EXTRA_CHARACTERS)}])",
        lambda _match: legacy_user_uri(new),
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

    _check_hosted_owner_collision(state_dir, old, new)
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


def _check_hosted_owner_collision(state_dir: Path, old: str, new: str) -> None:
    """Read before planning any writes; absent legacy columns mean no hosting."""
    path = state_dir / "agents.sqlite3"
    if old == new or not path.exists():
        return
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _table_exists(db, "agents"):
            return
        columns = {row[1] for row in db.execute("PRAGMA table_info(agents)")}
        if "hosted_by" not in columns:
            return
        row = db.execute(
            "SELECT actor, owner FROM agents WHERE hosted_by IS NOT NULL "
            "AND owner IN (?, ?) ORDER BY actor LIMIT 1", (old, new),
        ).fetchone()
        if row is not None:
            raise OwnerMigrationHostedConflict(
                old=old, new=new, actor=str(row[0]), hosted_owner=str(row[1]),
            )
    finally:
        db.close()
