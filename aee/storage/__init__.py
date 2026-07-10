"""AEE storage.

AEE-1 keeps the existing `dispatcher.db` SQLite store; additive
migration for AEE columns is wired into `dispatcher/db.py`. Future
phases may add Postgres / object-storage adapters.
"""
from __future__ import annotations

__all__: list[str] = []
