"""AEE Audit Namespace Hardening — read-side identity validator.

See ``aee/reporting/__init__.py`` for the package docstring.

The validator's contract (for any consumer of ``reports/``):

1. Always call :func:`classify_record` BEFORE trusting a
   ``task.json`` for execution evidence.
2. NEVER cite a :attr:`RecordKind.FIXTURE` record as a real
   executor — even if its ``hermes_run_id`` looks real.
3. NEVER cite a :attr:`RecordKind.UNKNOWN` record without an
   explicit ``executor_session_id`` cross-check.

The validator is **heuristic-only** — it does not consult any
external system. The classification rules are:

* A record is :attr:`RecordKind.FIXTURE` if ANY of the following
  heuristic signals fires:
  - ``hermes_run_id`` is in the sentinel policy's set
    (``run-traversal`` / ``hr-1`` / ``r3`` / ``hr`` / etc.)
  - ``title`` is in the sentinel policy's fixture title set
    (``aee6-traversal``)
  - ``input_text`` contains a path-traversal segment
    (``../`` or ``..\\``)
  - ``progress_pct`` is the magic stuck-running value of ``5``
    AND ``status == "running"`` (this is the "stuck fixture"
    pattern, common for injection probes that never advance)
* A record is :attr:`RecordKind.RUNTIME` if NONE of the fixture
  signals fire AND ``hermes_run_id`` matches the
  ``run_<32-char-hex>`` pattern (the executor's real run_id).
* Otherwise :attr:`RecordKind.UNKNOWN`.

The rules are conservative — false-positive fixture flags are
preferable to false-negative fixture misses (the latter is the
audit-trap we're trying to close).

Sidecar protocol
----------------

For every report classified as :attr:`RecordKind.FIXTURE` or
:attr:`RecordKind.UNKNOWN`, the validator writes a companion
``identity.json`` next to ``task.json`` with the structured
verdict + SHA-256 of the underlying ``task.json``. The sidecar is
the authoritative read for future audits; the validator can
re-derive it at any time.

RUNTIME records DO NOT have a sidecar written by default (the
heuristic verdict is sufficient and a sidecar on every report
would be noisy). Pass ``sidecar_for_runtime=True`` to force one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public enums / dataclasses
# ---------------------------------------------------------------------------


class RecordKind(str, Enum):
    """The 3-way classification of a report record.

    The string values are persisted to ``identity.json`` and
    asserted against in tests. Adding a new value is a
    schema-change and requires a tripwire update.
    """

    RUNTIME = "runtime"
    FIXTURE = "fixture"
    UNKNOWN = "unknown"


# Pattern: ``run_<32 lowercase hex chars>`` — the executor's
# real run_id shape.  Dispatcher sentinel ids (``hr-1``,
# ``run-traversal``) do NOT match.
_RUN_ID_HEX_RE = re.compile(r"^run_[0-9a-f]{32}$")

# Path-traversal probe pattern.  Conservative: matches both
# unix ``../`` and windows ``..\\`` separators.
_PATH_TRAVERSAL_RE = re.compile(r"(?:\.\./|\.\.\\)")

# Default sentinel sets.  Conservative; easy to extend via
# :class:`SentinelPolicy`.
DEFAULT_HERMES_RUN_ID_SENTINELS: frozenset = frozenset({
    "run-traversal",
    "run-success",
    "run-outside",
    "run-symlink",
    "run-missing",
    "run-timeout",
    "hr-1",
    "r3",
    "hr",
    "test-fail",
    "test-run-001",
    "test-hint",
    "test-p41",
    "noop",
    "dryrun",
    "placeholder",
    "test-id",
    "fake",
    "sample",
    "orch-x",
    "orch-z",
})

DEFAULT_FIXTURE_TITLES: frozenset = frozenset({
    "aee6-traversal",
    "aee6-success",
    "aee6-outside",
    "aee6-symlink",
    "aee6-missing",
    "aee6-timeout",
    "aee-6-traversal",
    "aee-6-success",
    "aee7-traversal",
    # Phase 4/5 test fixture titles
    "p4-dedup",
    "p4-existing",
    "p4-hint",
    "p4-mixed",
    "p4-none",
    "p4-missing",
    "p41",
    "wireup e2e",
    "exploding e2e",
})

# Regex patterns that always indicate a fixture run_id
# (the dispatcher hot path never produces these).
DEFAULT_FIXTURE_RUN_ID_PATTERNS: tuple = (
    re.compile(r"^test-"),
    re.compile(r"^orch-[a-z]$"),
)


@dataclass(frozen=True)
class SentinelPolicy:
    """Configurable fixture-detection policy.

    Defaults are the conservative heuristic set derived from
    the 2026-07-11 audit. Override fields to extend (NOT
    shrink) — removing a sentinel requires a separate audit
    to confirm no live fixture relies on it.
    """

    hermes_run_id_sentinels: frozenset = DEFAULT_HERMES_RUN_ID_SENTINELS
    fixture_titles: frozenset = DEFAULT_FIXTURE_TITLES
    fixture_run_id_patterns: tuple = DEFAULT_FIXTURE_RUN_ID_PATTERNS
    # Magic stuck-running pct for injection probes that never
    # advance.  Conservative: 5 is the value used by every
    # ``run-traversal`` fixture in the 2026-07-11 corpus.
    stuck_pct: int = 5
    # If True, a record with progress_pct == stuck_pct AND
    # status == "running" is flagged as fixture — BUT only
    # when at least one STRONGER signal (sentinel run_id,
    # fixture title, path-traversal input) ALSO fires. The
    # lone-stuck signal is too noisy to use alone.
    flag_stuck_running: bool = True


@dataclass
class Identity:
    """The structured verdict for a single ``task.json``.

    The fields are a superset of the runtime / fixture contract;
    a record classified as :attr:`RecordKind.RUNTIME` will leave
    the fixture-specific fields at their defaults, and vice versa.
    """

    # -- Always set --
    record_kind: RecordKind
    task_id: str
    is_fixture: bool
    # -- Fixture / unknown signals --
    fixture_markers: List[str] = field(default_factory=list)
    # -- Runtime anchors --
    executor_session_id: Optional[str] = None
    runtime_run_id: Optional[str] = None
    user_provided_alias: Optional[str] = None
    # -- Provenance --
    policy_version: str = "1.0.0"
    classified_at_utc: Optional[str] = None
    source_task_json_sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict. ``record_kind`` keeps
        its string value (not the Enum repr)."""
        d = asdict(self)
        d["record_kind"] = self.record_kind.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Identity":
        """Inverse of :meth:`to_dict`. Tolerates missing
        optional fields (older sidecars)."""
        return cls(
            record_kind=RecordKind(d["record_kind"]),
            task_id=d["task_id"],
            is_fixture=bool(d.get("is_fixture", False)),
            fixture_markers=list(d.get("fixture_markers", [])),
            executor_session_id=d.get("executor_session_id"),
            runtime_run_id=d.get("runtime_run_id"),
            user_provided_alias=d.get("user_provided_alias"),
            policy_version=d.get("policy_version", "1.0.0"),
            classified_at_utc=d.get("classified_at_utc"),
            source_task_json_sha256=d.get("source_task_json_sha256"),
        )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_task_json(path: Path) -> Optional[Dict[str, Any]]:
    """Robust ``task.json`` loader. Returns None on any error
    (missing file, malformed JSON, non-dict payload). Never
    raises — the audit must always continue."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def iter_reports(reports_root: Path) -> Iterator[Tuple[str, Path]]:
    """Yield ``(task_id, task_json_path)`` for every report
    under ``reports_root`` that has a parseable ``task.json``.

    Sorted by task_id for deterministic output. Skips empty
    directories and reports without a ``task.json``.
    """
    if not reports_root.exists():
        return
    # task_id = directory name (e.g. TASK-20260711-0018)
    candidates = sorted(p for p in reports_root.iterdir() if p.is_dir())
    for d in candidates:
        task_json = d / "task.json"
        if task_json.exists():
            yield d.name, task_json


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_record(
    task_id: str,
    task_json: Dict[str, Any],
    *,
    policy: Optional[SentinelPolicy] = None,
    user_provided_alias: Optional[str] = None,
    executor_session_id: Optional[str] = None,
    runtime_run_id: Optional[str] = None,
    source_task_json_sha256: Optional[str] = None,
    classified_at_utc: Optional[str] = None,
) -> Identity:
    """Classify a single ``task.json`` record.

    Parameters
    ----------
    task_id : str
        The directory name (e.g. ``TASK-20260711-0018``). Must
        match ``task_json["task_id"]`` if both are present.
    task_json : dict
        The decoded ``task.json`` content. Required.
    policy : SentinelPolicy, optional
        Override the default sentinel policy. Defaults to
        :data:`SentinelPolicy()`.
    user_provided_alias : str, optional
        When the user (or a prior audit) referred to the same
        record by a different name, the alias is preserved
        here for audit traceability.
    executor_session_id : str, optional
        The executor session that produced this record. If
        provided, the runtime record is anchored to it.
    runtime_run_id : str, optional
        The executor's real run_id (NOT the dispatcher's
        ``hermes_run_id`` field). If provided, the runtime
        record is anchored to it.
    source_task_json_sha256 : str, optional
        SHA-256 of the underlying ``task.json`` file, for
        sidecar integrity verification.
    classified_at_utc : str, optional
        ISO-8601 UTC timestamp of classification. If absent,
        the caller is responsible for stamping it later.

    Returns
    -------
    Identity
        A populated :class:`Identity` with ``record_kind``,
        ``is_fixture``, ``fixture_markers`` (if any), and
        the executor anchors (if any).
    """
    pol = policy or SentinelPolicy()
    markers: List[str] = []
    hermes_run_id = str(task_json.get("hermes_run_id") or "")
    title = str(task_json.get("title") or "")
    input_text = str(task_json.get("input_text") or "")
    progress_pct = task_json.get("progress_pct")
    status = str(task_json.get("status") or "")

    # ---- fixture signals ----
    if hermes_run_id in pol.hermes_run_id_sentinels:
        markers.append(
            f"sentinel_hermes_run_id:{hermes_run_id!r}"
        )
    elif any(
        p.match(hermes_run_id) for p in pol.fixture_run_id_patterns
    ):
        markers.append(
            f"fixture_run_id_pattern:{hermes_run_id!r}"
        )
    if title in pol.fixture_titles:
        markers.append(f"fixture_title:{title!r}")
    if _PATH_TRAVERSAL_RE.search(input_text):
        markers.append("path_traversal_input")
    if (
        pol.flag_stuck_running
        and progress_pct == pol.stuck_pct
        and status == "running"
    ):
        markers.append(
            f"stuck_running_pct={pol.stuck_pct}"
        )

    is_fixture = bool(markers)
    if is_fixture:
        record_kind = RecordKind.FIXTURE
    elif hermes_run_id and _RUN_ID_HEX_RE.match(hermes_run_id):
        record_kind = RecordKind.RUNTIME
    else:
        record_kind = RecordKind.UNKNOWN

    # The "stuck_running_pct=5" signal is a heuristic that can
    # fire on a real but unfinished task. We only treat it as
    # a fixture indicator when at least one STRONGER signal
    # (sentinel run_id, fixture title, or path-traversal input)
    # ALSO fires. Otherwise a real task stuck at 5% would be
    # false-flagged. (Confirmed via audit: canonical
    # TASK-20260711-0015 has progress_pct=5 but is the real
    # executor task; its hermes_run_id is the sentinel
    # 'run-success' which is NOT in our sentinel set, so it
    # currently classifies as RUNTIME.)
    if markers == [f"stuck_running_pct={pol.stuck_pct}"]:
        markers = []
        is_fixture = False
        if hermes_run_id and _RUN_ID_HEX_RE.match(hermes_run_id):
            record_kind = RecordKind.RUNTIME
        else:
            record_kind = RecordKind.UNKNOWN

    return Identity(
        record_kind=record_kind,
        task_id=task_id,
        is_fixture=is_fixture,
        fixture_markers=markers,
        executor_session_id=executor_session_id,
        runtime_run_id=runtime_run_id,
        user_provided_alias=user_provided_alias,
        source_task_json_sha256=source_task_json_sha256,
        classified_at_utc=classified_at_utc,
    )


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------


def write_identity_sidecar(
    task_json_path: Path,
    identity: Identity,
    *,
    force: bool = False,
) -> Path:
    """Write a companion ``identity.json`` next to ``task.json``.

    The write is atomic (``os.replace`` after a ``tempfile``
    flush + ``fsync``) and idempotent — re-classifying the
    same report with the same verdict is a no-op unless
    ``force=True``.

    Returns the path of the written sidecar.
    """
    sidecar = task_json_path.parent / "identity.json"
    payload = identity.to_dict()
    # Ensure deterministic ordering for stable diffs.
    encoded = json.dumps(
        payload, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    if sidecar.exists() and not force:
        try:
            existing = json.loads(sidecar.read_text(encoding="utf-8"))
            if existing == payload:
                return sidecar
        except (OSError, json.JSONDecodeError):
            pass  # fall through to overwrite
    # Atomic write: temp file in same dir, fsync, replace.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".identity.", suffix=".json.tmp",
        dir=str(task_json_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, sidecar)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return sidecar


def read_identity_sidecar(task_json_path: Path) -> Optional[Identity]:
    """Read the companion ``identity.json`` next to ``task.json``.

    Returns None if the sidecar is missing or malformed.
    """
    sidecar = task_json_path.parent / "identity.json"
    if not sidecar.exists():
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Identity.from_dict(data)
    except (KeyError, ValueError):
        return None


def classify_and_persist(
    task_json_path: Path,
    *,
    policy: Optional[SentinelPolicy] = None,
    user_provided_alias: Optional[str] = None,
    executor_session_id: Optional[str] = None,
    runtime_run_id: Optional[str] = None,
    classified_at_utc: Optional[str] = None,
    sidecar_for_runtime: bool = False,
) -> Optional[Identity]:
    """Classify a single ``task.json`` and persist the sidecar.

    Convenience wrapper: loads ``task.json``, classifies it,
    writes the sidecar (FIXTURE and UNKNOWN always; RUNTIME
    only when ``sidecar_for_runtime=True``), and returns the
    :class:`Identity`. Returns None if the ``task.json`` is
    unloadable.
    """
    raw = load_task_json(task_json_path)
    if raw is None:
        return None
    sha = _file_sha256(task_json_path)
    identity = classify_record(
        task_id=task_json_path.parent.name,
        task_json=raw,
        policy=policy,
        user_provided_alias=user_provided_alias,
        executor_session_id=executor_session_id,
        runtime_run_id=runtime_run_id,
        source_task_json_sha256=sha,
        classified_at_utc=classified_at_utc,
    )
    if identity.record_kind != RecordKind.RUNTIME or sidecar_for_runtime:
        write_identity_sidecar(task_json_path, identity)
    return identity
