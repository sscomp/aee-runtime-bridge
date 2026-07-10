"""Hashing helpers for AEE-6.

We always use sha256. We chunk-read files to avoid loading huge
artifacts (e.g. coverage.xml for a big repo) into memory. We
expose a hard cap (`MAX_HASH_BYTES`) so a runaway worker can't
make the bridge eat disk by writing a 50GB file and asking us
to hash it.
"""
from __future__ import annotations

import hashlib
import os
from typing import BinaryIO, Optional


# 8 KiB — balances syscall overhead vs memory.
DEFAULT_HASH_CHUNK = 8192

# 256 MiB. Anything bigger is rejected with ArtifactTooLargeError.
# This is generous (a 256MB coverage.xml is already absurd) but
# small enough that a malicious / runaway worker can't OOM the
# collector.
MAX_HASH_BYTES = 256 * 1024 * 1024


class _HashTooLarge(Exception):
    """Internal sentinel; the public path raises ArtifactTooLargeError."""


def sha256_file(
    path: str,
    *,
    chunk: int = DEFAULT_HASH_CHUNK,
    max_bytes: int = MAX_HASH_BYTES,
) -> str:
    """Compute the hex sha256 of a file, chunked.

    Raises:
        FileNotFoundError: if `path` does not exist.
        PermissionError: if the process cannot read it.
        OSError: on any other OS-level read failure.
        ValueError: if the file is larger than `max_bytes`
            (re-raised as the public ArtifactTooLargeError by
            `ArtifactPipeline.collect`).
    """
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            total += len(buf)
            if total > max_bytes:
                raise ValueError(
                    f"file too large for hashing: {path!r} "
                    f"({total} bytes > {max_bytes} cap)"
                )
            h.update(buf)
    return h.hexdigest()


def sha256_text(text: str, *, encoding: str = "utf-8") -> str:
    """Compute the sha256 of a text payload (helper for tests)."""
    if not isinstance(text, str):  # pragma: no cover - defensive
        raise TypeError(f"expected str, got {type(text).__name__}")
    return hashlib.sha256(text.encode(encoding)).hexdigest()


def sha256_stream(stream: BinaryIO, *, max_bytes: int = MAX_HASH_BYTES) -> str:
    """Same as `sha256_file` but reads from an open binary stream.

    Used by the in-memory test fixtures. A real worker that
    streams output (e.g. via subprocess.PIPE) would call this
    instead of writing to a temp file first.
    """
    h = hashlib.sha256()
    total = 0
    while True:
        buf = stream.read(DEFAULT_HASH_CHUNK)
        if not buf:
            break
        total += len(buf)
        if total > max_bytes:
            raise ValueError(
                f"stream too large for hashing ({total} > {max_bytes})"
            )
        h.update(buf)
    return h.hexdigest()


__all__ = [
    "DEFAULT_HASH_CHUNK",
    "MAX_HASH_BYTES",
    "sha256_file",
    "sha256_text",
    "sha256_stream",
]
