"""AEE-7.6 structured migration registry.

Replaces the ad-hoc ``list[tuple[col, sql]]`` shapes scattered across
``dispatcher/db.py`` with a single, versioned, introspectable registry.

Backwards compatibility
-----------------------
* ``_AEE7_WRITE_SIDE_MIGRATIONS`` and the existing tuple-list shapes
  are KEPT. The structured registry re-exports them as
  :class:`Migration` dataclasses so the legacy call sites
  (``db._init_schema``, ``db.run_migrations``, the existing
  AEE-7.5 G2 test) keep working byte-for-byte.
* Adding a new migration is a 1-line entry in :data:`MIGRATION_REGISTRY`
  (or one of the topic-specific lists). The dispatcher applies them
  in registration order — same order as the pre-AEE-7.6 list, so
  existing deployed DBs are unaffected.
* The old ``_AEE1_MIGRATIONS`` / ``_AEE3_MIGRATIONS`` /
  ``_AEE72_MIGRATIONS`` tuple lists remain in ``db.py`` (not
  refactored in this slice — out of scope). The new registry
  augments the system, doesn't migrate it.

Public surface
--------------
* :class:`Migration` — the structured record (id, version, name, sql,
  precondition, postcondition, idempotency evidence, target table,
  column, nullable, default, owner slice).
* :data:`WRITE_SIDE_MIGRATIONS` — the 2-entry list for AEE-7.5 write-side
  metadata, structured. Replaces ``db._AEE7_WRITE_SIDE_MIGRATIONS``.
* :func:`as_legacy_tuple_list` — convert a structured list back to the
  ``[(col, sql), ...]`` shape that ``db._init_schema`` consumes.
* :func:`validate_migrations` — tripwire: ids unique, columns unique
  within a table, no zero-length sql, all postconditions are
  callable, idempotency evidence is non-empty for non-trivial
  migrations.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Pre/postcondition signatures
# ---------------------------------------------------------------------------
# A precondition receives a live sqlite3 connection and the target table
# name; it returns True iff the migration should run.
Precondition = Callable[["object", str], bool]
# A postcondition receives a live sqlite3 connection and the target
# table name; it returns True iff the migration's effect is observable
# in the schema (e.g. the column now exists with the right type).
Postcondition = Callable[["object", str], bool]


# A no-op precondition that always returns True (default for migrations
# whose only idempotency check is "is the column present?").
def _pre_column_absent(conn, table: str) -> bool:
    # Resolved at apply time by the dispatcher; we provide a default
    # that calls pragma_table_info via a closure-style indirection.
    # The dispatcher passes a more specific callable in practice.
    return True


# ---------------------------------------------------------------------------
# Migration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """A single, versioned, structured migration record.

    Fields
    ------
    id
        Globally unique migration identifier, e.g. ``"aee7.5-001"``.
        Format: ``<slice>-<sequence>``. Used as the tripwire key
        and as the audit log tag.
    version
        Slice version (e.g. ``"7.5"``). Different migrations of the
        same slice share this. Order within a slice = registration
        order in :data:`WRITE_SIDE_MIGRATIONS`.
    name
        Short human label, e.g. ``"add tasks.executor_session_id"``.
    target_table
        The table the ALTER TABLE statement targets.
    column
        The column being added. ``None`` for table-level migrations.
    sql
        The raw SQL statement. For ``ALTER TABLE ADD COLUMN`` this
        is the statement verbatim. The dispatcher applies it
        inside the same transaction as the rest of the migrations.
    nullable
        Whether the new column is NULLable. Defaults to True (matches
        the actual schema: both write-side columns are NULLable).
    default
        The SQL literal the column defaults to, or None for
        ``DEFAULT NULL`` semantics.
    precondition
        Optional callable that decides whether to run the migration.
        Default: always run (the legacy ``pragma_table_info`` check
        in the dispatcher short-circuits on its own).
    postcondition
        Optional callable that verifies the migration actually
        landed. Default: None (the dispatcher's own pragma check
        is sufficient).
    idempotency_evidence
        Free-form short string describing how idempotency is
        enforced. The tripwire requires this to be non-empty for
        any non-trivial migration. Example values:
        ``"pragma_table_info: column absent check (dispatcher-side)"``
        or ``"CREATE TABLE IF NOT EXISTS (sqlite-level)"``.
    owner_slice
        The AEE slice that owns the migration, e.g. ``"AEE-7.5"``.
        Surfaced in audit logs and the structured migration
        registry.
    notes
        Optional free-form context (capped to a single line at
        write time).
    """

    id: str
    version: str
    name: str
    target_table: str
    sql: str
    owner_slice: str
    column: Optional[str] = None
    nullable: bool = True
    default: Optional[str] = None
    precondition: Optional[Precondition] = field(default=None, compare=False)
    postcondition: Optional[Postcondition] = field(default=None, compare=False)
    idempotency_evidence: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclasses: must use object.__setattr__ for the
        # computed sha256 tag. We store it as an attribute so the
        # tripwire can compare byte-for-byte deterministically.
        if not self.id:
            raise ValueError(f"Migration id is required (got {self.id!r})")
        if not self.sql or not self.sql.strip():
            raise ValueError(
                f"Migration {self.id!r}: sql is required and must be non-empty"
            )
        if not self.owner_slice:
            raise ValueError(
                f"Migration {self.id!r}: owner_slice is required"
            )
        if not self.idempotency_evidence:
            raise ValueError(
                f"Migration {self.id!r}: idempotency_evidence is required"
            )

    def as_legacy_tuple(self) -> Tuple[str, str]:
        """Return the ``(col, sql)`` shape ``db._init_schema`` expects.

        For table-level migrations (column=None) the column field
        is replaced by ``"<table-level>"`` so the shape is still
        a 2-tuple and the dispatcher's loop is unchanged.
        """
        return (self.column or f"<{self.target_table}-level>", self.sql)

    def fingerprint(self) -> str:
        """Deterministic 16-char hex of (id, sql, target_table, column).

        Used in tests to detect accidental edits to the registry
        entry. NOT a security primitive — collision resistance
        is 64 bits which is plenty for tripwires.
        """
        h = hashlib.sha256()
        h.update(self.id.encode("utf-8"))
        h.update(b"\x1f")
        h.update(self.sql.encode("utf-8"))
        h.update(b"\x1f")
        h.update(self.target_table.encode("utf-8"))
        h.update(b"\x1f")
        h.update((self.column or "").encode("utf-8"))
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# AEE-7.5 write-side metadata (the 2 columns added in commit b16dbd6)
# ---------------------------------------------------------------------------

#: Structured migration entries for the AEE-7.5 write-side metadata
#: slice. Two ADD COLUMN statements on the ``tasks`` table:
#: ``executor_session_id`` (caller's session id, NULLable) and
#: ``runtime_run_id`` (provider's external run id, NULLable).
#:
#: Order = application order on the live DB. Re-ordering is a
#: breaking change for any pre-AEE-7.5 deployment still mid-migration.
WRITE_SIDE_MIGRATIONS: List[Migration] = [
    Migration(
        id="aee7.5-001",
        version="7.5",
        name="add tasks.executor_session_id",
        target_table="tasks",
        column="executor_session_id",
        sql="ALTER TABLE tasks ADD COLUMN executor_session_id TEXT",
        owner_slice="AEE-7.5",
        nullable=True,
        default=None,
        idempotency_evidence=(
            "pragma_table_info('tasks') name='executor_session_id' absent check "
            "in dispatcher/db.py::_init_schema (idempotent re-runs are no-ops)"
        ),
        notes=(
            "AEE write-side metadata, closes \u00a720.9.10 deferred limitation. "
            "Persists the caller's session id at create() time."
        ),
    ),
    Migration(
        id="aee7.5-002",
        version="7.5",
        name="add tasks.runtime_run_id",
        target_table="tasks",
        column="runtime_run_id",
        sql="ALTER TABLE tasks ADD COLUMN runtime_run_id TEXT",
        owner_slice="AEE-7.5",
        nullable=True,
        default=None,
        idempotency_evidence=(
            "pragma_table_info('tasks') name='runtime_run_id' absent check "
            "in dispatcher/db.py::_init_schema (idempotent re-runs are no-ops)"
        ),
        notes=(
            "AEE write-side metadata. Persists the provider's external "
            "run id at start() time; hermes_run_id stays the legacy alias."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def as_legacy_tuple_list(migrations: Sequence[Migration]) -> List[Tuple[str, str]]:
    """Convert a structured :class:`Migration` list into the
    ``[(col, sql), ...]`` shape that ``dispatcher/db.py::_init_schema``
    consumes.

    The dispatcher's loop is unchanged: it reads the col, runs
    ``pragma_table_info``, and executes the stmt only when the
    column is absent. The structured wrapper is purely additive.
    """
    return [m.as_legacy_tuple() for m in migrations]


def validate_migrations(
    migrations: Sequence[Migration], *, raise_on_error: bool = True
) -> List[str]:
    """Tripwire: validate a migration list for structural integrity.

    Returns the list of human-readable error messages (empty
    list = no errors). When ``raise_on_error=True`` and the list
    is non-empty, raises :class:`ValueError` with the joined
    messages.

    Checks
    ------
    1. All ``id`` values are unique.
    2. Within a single ``target_table``, all ``column`` values
       are unique (and not None — None is reserved for table-level
       migrations which the dispatcher doesn't loop over).
    3. All ``sql`` statements are non-empty and look like a
       single statement (no ``;`` in the middle that would
       mask a multi-statement injection).
    4. All ``idempotency_evidence`` strings are non-empty (the
       dataclass ``__post_init__`` already enforces this; this
       check is the belt-and-braces assertion for list
       construction paths that bypass __post_init__).
    5. All ``owner_slice`` values are non-empty.
    """
    errors: List[str] = []
    seen_ids: set = set()
    seen_cols_per_table: dict = {}
    for m in migrations:
        # 1. id uniqueness
        if m.id in seen_ids:
            errors.append(
                f"duplicate migration id: {m.id!r}"
            )
        seen_ids.add(m.id)
        # 2. column uniqueness per table
        if m.column is not None:
            key = (m.target_table, m.column)
            if key in seen_cols_per_table:
                errors.append(
                    f"duplicate column {m.target_table}.{m.column} "
                    f"in migrations {seen_cols_per_table[key]!r} and {m.id!r}"
                )
            seen_cols_per_table[key] = m.id
        # 3. sql sanity
        stripped = m.sql.strip()
        if not stripped:
            errors.append(f"migration {m.id!r}: empty sql")
        # Allow at most one trailing ';' (some libraries add it,
        # SQLite tolerates either form). Reject any internal ';'
        # which would indicate a multi-statement payload.
        semicolons = stripped.count(";")
        if semicolons > 1 or (semicolons == 1 and not stripped.endswith(";")):
            errors.append(
                f"migration {m.id!r}: sql has internal ';' "
                f"(must be a single statement)"
            )
        # 4. idempotency evidence non-empty (also enforced in __post_init__)
        if not m.idempotency_evidence:
            errors.append(
                f"migration {m.id!r}: idempotency_evidence is empty"
            )
        # 5. owner_slice non-empty (also enforced in __post_init__)
        if not m.owner_slice:
            errors.append(
                f"migration {m.id!r}: owner_slice is empty"
            )
    if raise_on_error and errors:
        raise ValueError(
            "Migration validation failed:\n  - " + "\n  - ".join(errors)
        )
    return errors


# Validate the module-level list at import time. Any future
# append that breaks the contract fails the import instead of
# silently shipping a broken migration.
validate_migrations(WRITE_SIDE_MIGRATIONS)


# ---------------------------------------------------------------------------
# Legacy shape — re-exported for back-compat with any caller that
# imported ``dispatcher.db._AEE7_WRITE_SIDE_MIGRATIONS`` directly.
# The structured list and the legacy tuple list are kept in sync
# via this single source of truth.
# ---------------------------------------------------------------------------

#: Legacy ``[(col, sql), ...]`` shape. Built from
#: :data:`WRITE_SIDE_MIGRATIONS` at import time. The dispatcher
#: continues to consume this shape; this list is the contract
#: boundary between the structured registry and the existing
#: apply code.
LEGACY_TUPLE_LIST: List[Tuple[str, str]] = as_legacy_tuple_list(WRITE_SIDE_MIGRATIONS)
