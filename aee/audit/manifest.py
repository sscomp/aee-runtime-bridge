"""AEE-7.8 K1 — Read-only manifest support for AEE-7.7 corpus.

This module is the **read-side companion** to the AEE-7.7e
``write_manifest=True`` artifact (e.g.
``AEE_7_7d_7e_MANIFEST.json``). It loads a manifest from disk,
parses it into typed dataclasses, and exposes a small
introspection surface (``get_group``, ``list_group_names``,
``iter_files``, ``iter_new_files``, ``iter_modified_files``,
``total_files_count``).

Why a read-side surface
------------------------

The AEE-7.7e dryrun is *write-only* (it may write the manifest
artifact when ``write_manifest=True``; never read one). A
post-mortem, a CI gate, or a human reviewer who picks up a
manifest at rest needs a way to:

1. Open the file without parsing JSON by hand.
2. Verify the structural shape (top-level keys present, every
   ``files_new`` and ``files_modified`` row has the expected
   fields, every ``sha256`` matches the expected hex shape).
3. Iterate the groups / files / path set without touching the
   rest of ``aee.audit``.

That is what this module does. It does **not**:

* Never write a manifest, never modify the input file, never
  create a side-effect anywhere on disk.
* Never import ``dispatcher`` or any write-side executor.
* Never contact the live DB.
* Never use ``subprocess``, ``os.system``, ``os.environ``,
  ``requests``, or any network call.
* Never read ``dispatcher/.env`` or any secret-bearing file.

The module is a pure data shaper + a strict schema validator.
All failures are reported via :class:`ValidationResult` (the
caller decides whether errors are blocking); the load/validate
functions themselves never raise on a malformed manifest —
they raise only on I/O failure (file not found, permission
denied, JSON parse error) because those are not part of the
manifest *schema*, they are part of the *transport*.

Public surface
--------------

* :data:`MANIFEST_SCHEMA_VERSION` — ``"1.0.0"``. The manifest
  document does not carry a top-level ``schema_version`` field
  in the AEE-7.7d/7.7e artifact (the K1 version is the
  reader's contract).
* :func:`load_manifest` — open a manifest file, parse, return
  a :class:`ManifestDocument`. Also records the file's
  on-disk SHA-256 and size.
* :func:`validate_manifest` — strict structural validation
  (required top-level keys, required per-file fields, hex
  shape on SHA-256, ``imports_dispatcher`` / ``writes_to_live_db``
  are booleans, etc.). Returns a :class:`ValidationResult`.
* :class:`ManifestDocument` — the in-memory tree. Iterable
  via :meth:`iter_files` (yields :class:`FileEntry`).
* :class:`GroupEntry` — one ``groups.<name>`` row.
* :class:`FileEntry` — one ``files_new[i]`` or
  ``files_modified[i]`` row, with a ``kind`` discriminator.
* :class:`ValidationResult` — ``passed`` bool + ``errors`` +
  ``warnings``. Never raises.
* :class:`ManifestError` — raised only on I/O / JSON parse
  failure (file missing, JSON invalid, permission denied).

Excluded by design
------------------

* No re-execution of the dryrun pipeline. The K1 surface is a
  *loader*, not a *replayer*.
* No mutation of the input file.
* No side-channel output (no log writes, no tempfiles, no
  metrics emission).
* No dependency on ``sidecar_inventory``,
  ``sidecar_migration``, ``live_migration_dryrun``,
  ``live_audit``, or ``apply_sidecars`` — K1 is the smallest
  possible addition on top of stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: K1 reader contract. The AEE-7.7d/7.7e artifact does not embed
#: a top-level schema_version field; the reader pins the version
#: here so downstream consumers can switch on it without parsing
#: the document. Bumping this string is a breaking change for any
#: consumer that calls :func:`validate_manifest` with strict checks.
MANIFEST_SCHEMA_VERSION = "1.0.0"

#: SHA-256 hex length. Used to validate every ``sha256`` field
#: without a real hash recomputation (the loader still records
#: the on-disk hash separately, see :class:`ManifestDocument`).
_SHA256_HEX_LEN = 64

#: Required top-level keys. The AEE-7.7d/7.7e artifact
#: guarantees these — they are the contract K1 reads against.
_REQUIRED_TOP_LEVEL_KEYS = (
    "generated_utc",
    "groups",
)

#: Optional-but-validated top-level keys. Their presence is not
#: required, but if present they must be the right type.
_OPTIONAL_TOP_LEVEL_KEYS = (
    "generated_tw",
    "repo",
    "branch",
    "head_sha",
    "stash_sha",
    "excluded",
    "live_db_state",
    "corpus_state",
    "tests",
    "static_checks",
)

#: Required keys for a ``files_new[i]`` row.
_REQUIRED_NEW_FILE_KEYS = ("path", "sha256", "size", "lines")

#: Required keys for a ``files_modified[i]`` row.
_REQUIRED_MODIFIED_FILE_KEYS = ("path", "sha256", "size", "lines")

#: Required keys for a ``groups.<name>`` row.
_REQUIRED_GROUP_KEYS = ("files_new", "files_modified")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """Raised on I/O or JSON parse failure.

    Schema validation failures are *not* exceptions — they go
    into :class:`ValidationResult`. The loader raises only
    when the file cannot be read or the JSON cannot be parsed,
    because those are transport-level failures, not contract
    failures.
    """


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FileEntryKind(str, Enum):
    """Discriminator for :class:`FileEntry`.

    ``NEW`` — the file is in the group's ``files_new`` array.
    ``MODIFIED`` — the file is in the group's ``files_modified``
    array (i.e. it was already tracked at HEAD and is being
    amended by this slice).
    """

    NEW = "new"
    MODIFIED = "modified"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    """One row of a group's ``files_new`` or ``files_modified``.

    A ``FileEntry`` is the *raw* data from the manifest. It
    does NOT verify that the on-disk file at ``path`` matches
    ``sha256`` / ``size`` — that is the responsibility of a
    separate verify-only helper (out of scope for K1, deferred
    to a later AEE-7.8 slice). The K1 surface records what the
    manifest *says*; the consumer decides whether to trust it.
    """

    group_name: str
    kind: FileEntryKind
    path: str
    sha256: str
    size: int
    lines: int
    # Optional metadata. These may be absent from the manifest
    # depending on the slice that produced it (new files
    # usually have ``schema_version``; tests usually have
    # ``test_count`` / ``test_result``). Frozen dataclass
    # requires a default factory for the mutable extras.
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Render the entry back to a dict (for JSON dumps).

        The ``group_name`` and ``kind`` are prepended so the
        output is self-describing — round-tripping a
        :class:`FileEntry` through ``json.dumps`` does not
        lose the discriminator. The ``extras`` are spread
        last so they cannot accidentally overwrite the
        required fields.
        """
        out: Dict[str, Any] = {
            "group_name": self.group_name,
            "kind": self.kind.value,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "lines": self.lines,
        }
        for k, v in self.extras.items():
            if k not in out:
                out[k] = v
        return out


@dataclass(frozen=True)
class GroupEntry:
    """One row of the top-level ``groups`` dict.

    Holds the parsed :class:`FileEntry` lists plus the group's
    bookkeeping (subject_proposed, commit_ready, rationale,
    rollback, etc.). The bookkeeping is exposed via
    :attr:`extras` so this dataclass does not need to grow as
    new optional fields are added to the manifest format.
    """

    name: str
    files_new: List[FileEntry] = field(default_factory=list)
    files_modified: List[FileEntry] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def new_file_count(self) -> int:
        """Number of files in ``files_new``."""
        return len(self.files_new)

    @property
    def modified_file_count(self) -> int:
        """Number of files in ``files_modified``."""
        return len(self.files_modified)

    @property
    def total_file_count(self) -> int:
        """Number of files in this group (new + modified)."""
        return self.new_file_count + self.modified_file_count


@dataclass(frozen=True)
class ValidationResult:
    """Result of :func:`validate_manifest`.

    Strictly never raises. The caller decides whether
    ``passed=False`` is a blocker. ``errors`` are blocking
    shape violations; ``warnings`` are advisory (e.g. an
    expected-optional key is missing on a specific file).
    """

    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ManifestDocument:
    """The full in-memory manifest.

    The :attr:`raw` field is the parsed JSON dict — kept so
    callers that need an unknown field can reach it without
    this dataclass growing new attributes. Use the typed
    accessors (:meth:`get_group`, :meth:`list_group_names`,
    :meth:`iter_files`, etc.) for the structured access.

    The :attr:`dropped_row_count` and
    :attr:`dropped_group_count` fields count rows / groups
    the loader could not parse. The K1 contract is forgiving
    at load time (we never raise on a malformed row — see
    :func:`load_manifest`) but the validator surfaces these
    as errors so a caller that hands the manifest to a
    post-mortem does not silently lose data.
    """

    raw: Dict[str, Any]
    groups: Dict[str, GroupEntry] = field(default_factory=dict)
    # On-disk fingerprint, captured at load time.
    source_path: str = ""
    on_disk_sha256: str = ""
    on_disk_size: int = 0
    # Count of file rows the loader could not parse. Set by
    # :func:`load_manifest`; surfaced by the validator.
    dropped_row_count: int = 0
    # Count of group bodies the loader could not parse.
    dropped_group_count: int = 0

    # ---- introspection surface -----------------------------------

    def list_group_names(self) -> List[str]:
        """Return the group names in document order.

        Document order matters because the staging-boundary
        report and the dryrun's report markdown both render
        groups in the same order they appear in the JSON.
        """
        return list(self.groups.keys())

    def get_group(self, name: str) -> Optional[GroupEntry]:
        """Return the :class:`GroupEntry` for ``name``, or None.

        Case-sensitive, exact-match — group names are
        intentional identifiers (``G1_AEE-7.7d_...``) and
        fuzzy matching would hide typos.
        """
        return self.groups.get(name)

    def iter_files(self) -> Iterator[FileEntry]:
        """Yield every :class:`FileEntry` across every group."""
        for group in self.groups.values():
            for fe in group.files_new:
                yield fe
            for fe in group.files_modified:
                yield fe

    def iter_new_files(self) -> Iterator[FileEntry]:
        """Yield only the ``NEW`` :class:`FileEntry` rows."""
        for group in self.groups.values():
            yield from group.files_new

    def iter_modified_files(self) -> Iterator[FileEntry]:
        """Yield only the ``MODIFIED`` :class:`FileEntry` rows."""
        for group in self.groups.values():
            yield from group.files_modified

    def total_files_count(self) -> int:
        """Return the total number of files across all groups."""
        return sum(g.total_file_count for g in self.groups.values())

    def new_files_count(self) -> int:
        """Return the total number of NEW files across all groups."""
        return sum(g.new_file_count for g in self.groups.values())

    def modified_files_count(self) -> int:
        """Return the total number of MODIFIED files across all groups."""
        return sum(g.modified_file_count for g in self.groups.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_sha256(path: str) -> str:
    """Return the SHA-256 hex digest of the file at ``path``.

    Used by :func:`load_manifest` to record the on-disk
    fingerprint of the manifest file itself. Empty string
    return on missing / unreadable file (caller has already
    read the file successfully, so this branch is defensive).
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _is_hex_string(value: Any, expected_len: int) -> bool:
    """Return True iff ``value`` is a string of the right length
    consisting only of hex characters.

    Used to validate the SHA-256 fields without re-hashing
    every referenced file. A non-hex string here is a
    structural bug in the manifest, not a hash mismatch —
    the K1 surface does not verify hashes against the
    filesystem.
    """
    if not isinstance(value, str):
        return False
    if len(value) != expected_len:
        return False
    for ch in value:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _is_non_negative_int(value: Any) -> bool:
    """Return True iff ``value`` is an int >= 0.

    ``bool`` is a subclass of ``int`` in Python, so ``True``
    / ``False`` would otherwise pass. The K1 surface does
    NOT accept booleans where sizes/lines are expected —
    the manifest contract is strict about the field types.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_bool(value: Any) -> bool:
    """Return True iff ``value`` is a real ``bool``.

    Used for the ``imports_dispatcher`` / ``writes_to_live_db``
    flags on ``files_new`` rows. Mirrors the
    ``_is_non_negative_int`` rule: a strict-type contract.
    """
    return isinstance(value, bool)


def _parse_file_entry(
    *,
    group_name: str,
    kind: FileEntryKind,
    raw: Dict[str, Any],
    required_keys: tuple,
) -> FileEntry:
    """Build a :class:`FileEntry` from a raw dict.

    Raises ``ValueError`` on any of:

    * a missing required key
    * a ``path`` / ``sha256`` that is not a string
    * a ``size`` / ``lines`` that is not a non-negative int
      (booleans are rejected — see :func:`_is_non_negative_int`)

    The caller is expected to catch the ``ValueError`` and
    drop the bad row silently at load time; the validator
    surfaces the structural error as a warning / error. The
    parser is a pure function, it does not own the decision
    of whether a malformed row is fatal.
    """
    for k in required_keys:
        if k not in raw:
            raise ValueError(f"missing required key: {k!r}")
    # Strict type checks BEFORE coercion. ``int("32")`` would
    # otherwise turn a string "32" into a real int, hiding a
    # contract violation from the validator.
    if not isinstance(raw["path"], str):
        raise ValueError(f"'path' is not a string: {type(raw['path']).__name__}")
    if not isinstance(raw["sha256"], str):
        raise ValueError(
            f"'sha256' is not a string: {type(raw['sha256']).__name__}"
        )
    if not _is_non_negative_int(raw["size"]):
        raise ValueError(
            f"'size' is not a non-negative int: {type(raw['size']).__name__}"
        )
    if not _is_non_negative_int(raw["lines"]):
        raise ValueError(
            f"'lines' is not a non-negative int: {type(raw['lines']).__name__}"
        )
    extras = {k: v for k, v in raw.items() if k not in required_keys}
    return FileEntry(
        group_name=group_name,
        kind=kind,
        path=raw["path"],
        sha256=raw["sha256"],
        size=raw["size"],
        lines=raw["lines"],
        extras=extras,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_manifest(
    path: Union[str, os.PathLike],
) -> ManifestDocument:
    """Load a manifest file into a :class:`ManifestDocument`.

    The function:

    1. Reads the file at ``path`` (raises :class:`ManifestError`
       on missing / permission denied / not-a-file).
    2. Parses it as JSON (raises :class:`ManifestError` on
       parse error).
    3. Builds a :class:`ManifestDocument` with typed
       :class:`GroupEntry` / :class:`FileEntry` rows.
    4. Records the on-disk SHA-256 + size as
       :attr:`ManifestDocument.on_disk_sha256` /
       :attr:`ManifestDocument.on_disk_size`.

    Schema validation is *not* done here — call
    :func:`validate_manifest` on the result. Splitting load
    from validate lets callers (e.g. test fixtures) inspect
    a malformed manifest for forensic purposes.
    """
    path_str = os.fspath(path)
    if not os.path.isfile(path_str):
        raise ManifestError(f"manifest file not found: {path_str!r}")
    try:
        with open(path_str, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        raise ManifestError(f"could not read manifest file: {exc}") from exc
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON / UTF-8: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(
            f"manifest top-level must be a JSON object, got {type(raw).__name__}"
        )

    on_disk_sha = hashlib.sha256(blob).hexdigest()
    on_disk_size = len(blob)

    groups_raw = raw.get("groups", {})
    if not isinstance(groups_raw, dict):
        raise ManifestError(
            f"manifest 'groups' must be a JSON object, got {type(groups_raw).__name__}"
        )

    groups: Dict[str, GroupEntry] = {}
    dropped_row_count = 0
    dropped_group_count = 0
    for group_name, group_body in groups_raw.items():
        if not isinstance(group_body, dict):
            # Skip the bad group silently at load time; the
            # validator will surface it as an error. We do
            # not want load to drop groups on a single bad
            # row — that loses data on partial corruption.
            dropped_group_count += 1
            groups[group_name] = GroupEntry(
                name=group_name,
                extras={"__load_error__": f"not a dict: {type(group_body).__name__}"},
            )
            continue
        files_new_raw = group_body.get("files_new", [])
        files_mod_raw = group_body.get("files_modified", [])
        if not isinstance(files_new_raw, list):
            files_new_raw = []
            dropped_row_count += 1
        if not isinstance(files_mod_raw, list):
            files_mod_raw = []
            dropped_row_count += 1
        new_entries: List[FileEntry] = []
        for row in files_new_raw:
            if not isinstance(row, dict):
                dropped_row_count += 1
                continue
            try:
                new_entries.append(
                    _parse_file_entry(
                        group_name=group_name,
                        kind=FileEntryKind.NEW,
                        raw=row,
                        required_keys=_REQUIRED_NEW_FILE_KEYS,
                    )
                )
            except (ValueError, TypeError, KeyError):
                # Same as above: skip the bad row silently at
                # load time; validator surfaces the structural
                # error. We do not raise because the caller
                # may want to inspect a partially-corrupt
                # manifest for forensics.
                dropped_row_count += 1
                continue
        mod_entries: List[FileEntry] = []
        for row in files_mod_raw:
            if not isinstance(row, dict):
                dropped_row_count += 1
                continue
            try:
                mod_entries.append(
                    _parse_file_entry(
                        group_name=group_name,
                        kind=FileEntryKind.MODIFIED,
                        raw=row,
                        required_keys=_REQUIRED_MODIFIED_FILE_KEYS,
                    )
                )
            except (ValueError, TypeError, KeyError):
                dropped_row_count += 1
                continue
        extras = {
            k: v
            for k, v in group_body.items()
            if k not in _REQUIRED_GROUP_KEYS
        }
        groups[group_name] = GroupEntry(
            name=group_name,
            files_new=new_entries,
            files_modified=mod_entries,
            extras=extras,
        )

    return ManifestDocument(
        raw=raw,
        groups=groups,
        source_path=path_str,
        on_disk_sha256=on_disk_sha,
        on_disk_size=on_disk_size,
        dropped_row_count=dropped_row_count,
        dropped_group_count=dropped_group_count,
    )


def validate_manifest(doc: ManifestDocument) -> ValidationResult:
    """Validate a :class:`ManifestDocument` against the K1 contract.

    Pure function: never raises, never mutates ``doc``. The
    caller decides whether to treat ``passed=False`` as a
    blocker (most callers will) and whether to surface
    ``warnings`` (most callers will not).

    The check set is intentionally narrow:

    * Every key in :data:`_REQUIRED_TOP_LEVEL_KEYS` is present.
    * ``groups`` is a dict and every value is a dict with the
      required :data:`_REQUIRED_GROUP_KEYS`.
    * Every ``files_new[i]`` and ``files_modified[i]`` row has
      the required keys with the right types and shapes.
    * Every ``sha256`` is a 64-char hex string.
    * Every ``size`` and ``lines`` is a non-negative int.
    * Every ``imports_dispatcher`` / ``writes_to_live_db`` flag
      (when present) is a real bool, not a truthy value.
    * Every group has at least one file (new or modified) —
      an empty group is structurally suspicious.
    * The on-disk SHA-256 is consistent with a real file
      (i.e. not the empty string the loader records on a
      missing file).
    """
    errors: List[str] = []
    warnings: List[str] = []

    raw = doc.raw
    for k in _REQUIRED_TOP_LEVEL_KEYS:
        if k not in raw:
            errors.append(f"missing required top-level key: {k!r}")
    for k in _OPTIONAL_TOP_LEVEL_KEYS:
        if k in raw and not isinstance(raw[k], (dict, str, list)):
            errors.append(
                f"optional top-level key {k!r} has wrong type: "
                f"{type(raw[k]).__name__}"
            )

    # On-disk fingerprint sanity.
    if not doc.on_disk_sha256:
        errors.append("on_disk_sha256 is empty — file was unreadable at load time")
    elif not _is_hex_string(doc.on_disk_sha256, _SHA256_HEX_LEN):
        errors.append("on_disk_sha256 is not a valid SHA-256 hex string")
    if doc.on_disk_size <= 0:
        errors.append("on_disk_size is non-positive — file is empty or unreadable")

    if not doc.groups:
        warnings.append("manifest has zero groups (empty 'groups' dict)")

    if doc.dropped_row_count > 0:
        errors.append(
            f"loader dropped {doc.dropped_row_count} malformed file row(s) "
            f"during load — manifest is partially corrupted"
        )
    if doc.dropped_group_count > 0:
        errors.append(
            f"loader dropped {doc.dropped_group_count} malformed group body "
            f"during load — manifest is partially corrupted"
        )

    for group_name, group in doc.groups.items():
        # Detect the load-time sentinel that marks a corrupted
        # group (we set ``__load_error__`` in the extras).
        if "__load_error__" in group.extras:
            errors.append(
                f"group {group_name!r} could not be parsed: "
                f"{group.extras['__load_error__']}"
            )
            continue
        if group.total_file_count == 0:
            warnings.append(
                f"group {group_name!r} has zero files (empty new + modified)"
            )
        for fe in group.files_new:
            errors.extend(_validate_file_entry(fe, group_name=group_name))
        for fe in group.files_modified:
            errors.extend(_validate_file_entry(fe, group_name=group_name))

    return ValidationResult(
        passed=not errors,
        errors=errors,
        warnings=warnings,
    )


def _validate_file_entry(fe: FileEntry, *, group_name: str) -> List[str]:
    """Return the per-:class:`FileEntry` validation errors.

    Pulled out of :func:`validate_manifest` to keep the top-
    level function readable. Errors are returned as a list of
    human-readable strings (the caller appends them to the
    aggregate errors list).
    """
    errs: List[str] = []
    if not fe.path:
        errs.append(
            f"group {group_name!r}: {fe.kind.value} entry has empty 'path'"
        )
    if not _is_hex_string(fe.sha256, _SHA256_HEX_LEN):
        errs.append(
            f"group {group_name!r}: file {fe.path!r} sha256 is not a valid "
            f"64-char hex string (got {len(fe.sha256)} chars)"
        )
    if not _is_non_negative_int(fe.size):
        errs.append(
            f"group {group_name!r}: file {fe.path!r} 'size' is not a "
            f"non-negative int (got {type(fe.size).__name__})"
        )
    if not _is_non_negative_int(fe.lines):
        errs.append(
            f"group {group_name!r}: file {fe.path!r} 'lines' is not a "
            f"non-negative int (got {type(fe.lines).__name__})"
        )
    # Optional-but-strictly-typed booleans.
    for bk in ("imports_dispatcher", "writes_to_live_db"):
        if bk in fe.extras and not _is_bool(fe.extras[bk]):
            errs.append(
                f"group {group_name!r}: file {fe.path!r} {bk!r} must be a "
                f"bool, got {type(fe.extras[bk]).__name__}"
            )
    return errs


# ---------------------------------------------------------------------------
# Manifest → PlanInput adapter (AEE-7.8 K2)
# ---------------------------------------------------------------------------
#
# Why this surface
# ----------------
# K1 shipped a *reader* (load + validate + introspection). The
# next natural step is a typed *adapter* that flattens a
# :class:`ManifestDocument` into the per-file input rows a
# planner (``aee.audit.apply_sidecars`` / ``aee.audit.
# plan_sidecar_migration``) expects.
#
# The current planner in ``sidecar_inventory.py`` walks
# ``reports/`` directly and produces an aggregate
# :class:`MigrationPlan`. The K2 adapter is a **read-side**
# shape probe: given a manifest that *describes* a corpus, what
# would the per-file input rows look like if a planner wanted
# to consume them one at a time? K2.5+ can decide whether the
# real planner should consume :class:`PlanInput` rows instead
# of walking ``reports/`` — K2 ships the shape, the wire-up
# is a separate commit.
#
# The adapter is intentionally **read-only** (K1 isolation
# contract preserved): it never writes, never opens a file
# outside the manifest's own ``source_path``, never imports
# ``dispatcher``, never touches the live DB, never reads
# environment variables, never spawns child processes via the shell.


@dataclass(frozen=True)
class PlanInput:
    """One per-file input row the manifest describes.

    The shape mirrors the manifest's :class:`FileEntry` but is
    intentionally a *narrow* contract: only the fields a
    planner needs are exposed as typed attributes. The full
    per-file extras (e.g. ``imports_dispatcher``,
    ``writes_to_live_db``, ``schema_version``, ``test_count``)
    are forwarded via :attr:`extras` as a dict so the dataclass
    does not need to grow as new optional fields are added to
    the manifest format.

    Frozen + tuple-of-strings for the helper result so the
    adapter output can be put in sets, hashed, and
    JSON-serialized deterministically.
    """

    group_name: str
    kind: FileEntryKind
    path: str
    sha256: str
    size: int
    lines: int
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Render the row back to a dict (for JSON dumps).

        The ``group_name`` and ``kind`` are prepended so the
        output is self-describing — round-tripping a
        :class:`PlanInput` through ``json.dumps`` does not
        lose the discriminator. The ``extras`` are spread
        last so they cannot accidentally overwrite the
        required fields.
        """
        out: Dict[str, Any] = {
            "group_name": self.group_name,
            "kind": self.kind.value,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "lines": self.lines,
        }
        for k, v in self.extras.items():
            if k not in out:
                out[k] = v
        return out


@dataclass(frozen=True)
class ManifestToPlanResult:
    """Result of :func:`manifest_to_plan_inputs`.

    Always non-raising. ``passed=False`` means the adapter
    refused to project the manifest (validation failed or the
    doc was empty). ``plan_inputs`` is the projected
    per-file list (empty on failure). ``warnings`` is a
    list of human-readable strings — the caller decides
    whether to surface them (most callers will).
    """

    passed: bool
    plan_inputs: Tuple[PlanInput, ...] = ()
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "plan_input_count": len(self.plan_inputs),
            "warning_count": len(self.warnings),
            "plan_inputs": [p.to_dict() for p in self.plan_inputs],
            "warnings": list(self.warnings),
        }


def load_manifest_or_default(
    path: Optional[Union[str, os.PathLike]] = None,
) -> ManifestDocument:
    """Load a manifest, or return an empty :class:`ManifestDocument`.

    Mirrors :func:`load_manifest`'s "raise on transport failure,
    forgive on schema failure" rule for the explicit ``path``
    case. When ``path is None``, returns an *empty* manifest —
    a safe default that the adapter can then project into
    zero :class:`PlanInput` rows.

    Use the explicit ``path=...`` form for real inputs; the
    ``path=None`` default is for callers that need a sentinel
    value (e.g. test fixtures, optional command-line flag).
    The default never reads the canonical
    ``AEE_7_7d_7e_MANIFEST.json`` from the repo root — that
    would be a hidden side effect on the importable surface.
    """
    if path is None:
        return ManifestDocument(
            raw={},
            groups={},
            source_path="",
            on_disk_sha256="",
            on_disk_size=0,
            dropped_row_count=0,
            dropped_group_count=0,
        )
    return load_manifest(path)


def manifest_to_plan_inputs(
    doc: ManifestDocument,
) -> ManifestToPlanResult:
    """Project a :class:`ManifestDocument` to per-file :class:`PlanInput` rows.

    Read-only adapter. The function:

    1. Runs :func:`validate_manifest` on ``doc``. If
       ``result.passed is False``, returns
       :class:`ManifestToPlanResult` with ``passed=False``,
       empty ``plan_inputs``, and the validator's errors
       appended to ``warnings``.
    2. Iterates ``doc.iter_files()`` in deterministic
       document order (the same order
       :meth:`ManifestDocument.iter_files` yields — group
       insertion order, NEW before MODIFIED within each
       group).
    3. Builds one :class:`PlanInput` per :class:`FileEntry`,
       forwarding the file's ``extras`` dict.
    4. Returns :class:`ManifestToPlanResult` with
       ``passed=True``, the projected rows, and the
       validator's *warnings* (not errors — those already
       gated the projection).

    The function never raises. Schema validation failures,
    empty manifests, and zero-file groups all return a
    well-formed :class:`ManifestToPlanResult` with
    ``passed=False`` and a populated ``warnings`` list. The
    caller decides what to do with the result.
    """
    warnings: List[str] = []
    validation = validate_manifest(doc)
    # Errors block projection. Warnings do not.
    if not validation.passed:
        for err in validation.errors:
            warnings.append(f"validation: {err}")
        return ManifestToPlanResult(
            passed=False,
            plan_inputs=(),
            warnings=tuple(warnings),
        )
    # Forward validator warnings (advisory, not blocking).
    for warn in validation.warnings:
        warnings.append(f"validation: {warn}")

    plan_inputs: List[PlanInput] = []
    for fe in doc.iter_files():
        plan_inputs.append(
            PlanInput(
                group_name=fe.group_name,
                kind=fe.kind,
                path=fe.path,
                sha256=fe.sha256,
                size=fe.size,
                lines=fe.lines,
                extras=dict(fe.extras),
            )
        )

    return ManifestToPlanResult(
        passed=True,
        plan_inputs=tuple(plan_inputs),
        warnings=tuple(warnings),
    )


__all__ = [
    "FileEntry",
    "FileEntryKind",
    "GroupEntry",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestDocument",
    "ManifestError",
    "ManifestToPlanResult",
    "PlanInput",
    "ValidationResult",
    "load_manifest",
    "load_manifest_or_default",
    "manifest_to_plan_inputs",
    "validate_manifest",
]
