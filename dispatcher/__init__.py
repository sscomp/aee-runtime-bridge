"""Hermes M2 Task Dispatcher — Phase 1.

This package provides:
- db: SQLite connection + schema management
- ids: TASK-YYYYMMDD-NNNN generator
- models: Pydantic + dataclass models
- manager: TaskManager (state machine)
- progress: progress reporter (5/10/25/40/60/80/95/100)
"""
