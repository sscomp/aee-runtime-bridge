"""AEE-6 Artifact Pipeline — domain model, collection, storage.

Pipeline overview
-----------------
    Runtime Adapter (Hermes / Claude Code / future)
        │  writes files to /home/ubuntu/...
        ▼
    ArtifactPipeline.collect(task_id, paths, classifications=None)
        │   • stat() each path
        │   • sha256 the file content
        │   • classify (report / patch / log / coverage / artifact / unknown)
        ▼
    ArtifactRepository (SQLite `artifacts` table, idempotent on
                       (task_id, path) for re-collect safety)
        │
        ▼
    GET /v1/artifacts  ─►  orchestrator (ChatGPT Custom GPT)

AEE-6 is the foundation; the dispatcher hot path does not depend
on it yet. The existing Phase 4 `delivery_json` blob is left
untouched in this slice — a follow-up iteration can re-implement
`_verify_expected_delivery()` on top of `ArtifactPipeline.collect()`
without breaking the 173-test AEE-5 baseline.

Design notes
------------
* **Why a domain class, not a dict?** Because every consumer
  (orchestrator, audit log, future Web UI) will ask the same
  questions: does it exist, how big, is it a report / patch / log,
  what is its sha256, when was it last modified. A typed `Artifact`
  gives those consumers a stable contract.

* **Why sha256 in this slice?** Because AEE-7's "Event Timeline"
  and AEE-8's "Event Bus" both want a content-addressable handle
  for deduplication and for "did this artifact change across
  retries?" detection. Storing it now is cheap and the schema
  migration is idempotent.

* **Why `classify_by_path()` instead of magic content sniffing?**
  Because AEE-5 already taught us that conventions (`report.md`,
  `patch.diff`, `test.log`, `coverage.xml`) are the only signal we
  can rely on at Job-submit time. Mime sniffing at collect time
  would be a Phase 2 feature and is deferred.

* **Why no Python file dependency at import time?** The
  `ArtifactPipeline` lives in aee/artifacts/ but the dispatcher
  hot path imports nothing from this package yet. The schema
  migration is opt-in (call `ensure_aee6_schema(conn)` from
  `_init_schema()` in a follow-up — not in this slice).
"""
from __future__ import annotations

from .collect import (  # noqa: F401
    ArtifactCollector,
    ArtifactPipeline,
    PolicyViolationError,
)
from .errors import (  # noqa: F401
    ArtifactAccessError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
)
from .hashutil import (  # noqa: F401
    DEFAULT_HASH_CHUNK,
    MAX_HASH_BYTES,
    sha256_file,
    sha256_stream,
    sha256_text,
)
from .models import (  # noqa: F401
    ARTIFACT_KIND_REPORT,
    ARTIFACT_KIND_PATCH,
    ARTIFACT_KIND_LOG,
    ARTIFACT_KIND_COVERAGE,
    ARTIFACT_KIND_ARTIFACT,
    ARTIFACT_KIND_UNKNOWN,
    ARTIFACT_KINDS,
    Artifact,
    ArtifactKind,
    classify_by_path,
)
from .policy import (  # noqa: F401
    ArtifactPolicy,
    PolicyCheckError,
    PolicyDecision,
    PolicyViolationCode,
    safe_resolve,
)
from .repository import (  # noqa: F401
    InMemoryArtifactRepository,
    SqliteArtifactRepository,
    ensure_aee6_schema,
)

__all__ = [
    # collect
    "ArtifactCollector",
    "ArtifactPipeline",
    "PolicyViolationError",
    # errors
    "ArtifactAccessError",
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactTooLargeError",
    # hash
    "DEFAULT_HASH_CHUNK",
    "MAX_HASH_BYTES",
    "sha256_file",
    "sha256_stream",
    "sha256_text",
    # models
    "ARTIFACT_KIND_REPORT",
    "ARTIFACT_KIND_PATCH",
    "ARTIFACT_KIND_LOG",
    "ARTIFACT_KIND_COVERAGE",
    "ARTIFACT_KIND_ARTIFACT",
    "ARTIFACT_KIND_UNKNOWN",
    "ARTIFACT_KINDS",
    "Artifact",
    "ArtifactKind",
    "classify_by_path",
    # policy
    "ArtifactPolicy",
    "PolicyCheckError",
    "PolicyDecision",
    "PolicyViolationCode",
    "safe_resolve",
    # repository
    "InMemoryArtifactRepository",
    "SqliteArtifactRepository",
    "ensure_aee6_schema",
]
