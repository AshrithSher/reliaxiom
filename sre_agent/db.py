"""Shared SQLite connection helper.

Every persisted store (incidents, tickets, change log, post-mortems) is read by the
out-of-process dashboard *while* the agent writes it, and the dashboard's approval path
opens its own writer connection to the same files. With the default rollback journal and a
zero busy-timeout that concurrency surfaces as `sqlite3.OperationalError: database is locked`
— which, before the main loop was guarded, could freeze detection outright.

`connect()` opens every store the same safe way:
  - WAL journal: readers never block the writer and vice-versa (one writer at a time still,
    but the dashboard only reads);
  - a 30s busy timeout so a brief overlap waits instead of raising;
  - check_same_thread=False because the agent touches a store from its tick loop while the
    poll loop runs on another thread.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL = concurrent reader (dashboard) + writer (agent); busy_timeout = wait, don't raise.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
