"""Cumulative, all-time savings ledger for smush compress calls.

Stdlib-only SQLite file living alongside the repo (not under ~/.claude), so
`smush gain`-style totals survive across sessions. Every `smush compress
--log-stats PATH` call appends one row; nothing here ever deletes or
aggregates in place, so the raw history is always available.

Beyond the totals, the ledger exists to answer one diagnostic question:
*is smush actually working?* That is the identity rate — the share of commands
that fell through every codec and saved nothing. Totals alone hide it, because
a handful of well-compressed payloads can mask a majority that compress to
nothing at all. `breakdown()` reports it alongside the per-compressor and
per-command splits.
"""

from __future__ import annotations

import datetime
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .result import CompressResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    compressor TEXT NOT NULL,
    original_tokens INTEGER NOT NULL,
    compressed_tokens INTEGER NOT NULL,
    saved_tokens INTEGER NOT NULL,
    session_id TEXT
)
"""

# Columns added after the first release. Applied additively so that ledgers
# written by older versions keep working: every one is nullable, and every
# reader below tolerates NULL.
_ADDED_COLUMNS = (
    ("session_id", "TEXT"),
    # The shell command that produced the payload, verbatim. Kept whole rather
    # than pre-summarized so a future question ("which flags compress badly?")
    # can still be asked of history already recorded.
    ("command", "TEXT"),
    # Stored as 0/1 ints: SQLite has no boolean, and NULL then unambiguously
    # means "written before this column existed" rather than "False".
    ("lossy", "INTEGER"),
    ("reversible", "INTEGER"),
)

# How many leading words of a command form its grouping label. Two is enough
# to separate `git status` from `git diff` while keeping paths and flags —
# the parts that differ every single run — out of the key.
_COMMAND_LABEL_WORDS = 2


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(compressions)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE compressions ADD COLUMN {name} {decl}")


def command_label(command: Optional[str]) -> str:
    """Group key for a command string, e.g. `git status -sb .` -> `git status`.

    Grouping on the raw string would scatter one command across dozens of rows
    that differ only by path or flag, which is exactly the noise the breakdown
    is meant to cut through.
    """
    if not command or not command.strip():
        return "(unknown)"
    return " ".join(command.split()[:_COMMAND_LABEL_WORDS])


def record(db_path: str, result: CompressResult, session_id: Optional[str] = None,
           command: Optional[str] = None) -> None:
    """Append one row for this compression. Best-effort: never raises."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO compressions "
                "(timestamp, compressor, original_tokens, compressed_tokens, "
                " saved_tokens, session_id, command, lossy, reversible) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    result.compressor,
                    result.original_tokens,
                    result.compressed_tokens,
                    result.tokens_saved,
                    session_id,
                    command,
                    int(result.lossy),
                    int(result.reversible),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _scope(session_id: Optional[str]) -> Tuple[str, tuple]:
    """WHERE clause + params restricting a query to one session, or to all."""
    if session_id:
        return " WHERE session_id = ?", (session_id,)
    return "", ()


def total_saved(db_path: str) -> Optional[int]:
    """Sum of saved_tokens across all history, or None if the db is unavailable."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT COALESCE(SUM(saved_tokens), 0) FROM compressions").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return None


def session_saved(db_path: str, session_id: str) -> Optional[int]:
    """Sum of saved_tokens for one session_id, or None if the db is unavailable."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT COALESCE(SUM(saved_tokens), 0) FROM compressions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return None


def breakdown(db_path: str, session_id: Optional[str] = None,
              top_commands: int = 10) -> Optional[Dict[str, Any]]:
    """Aggregate the ledger. Returns None if the db is unreadable.

    Keys:
        calls, original_tokens, compressed_tokens, saved_tokens, reduction
        identity_calls, identity_rate  -- the health metric; see module docstring
        lossy_calls
        compressors: [{name, calls, original_tokens, saved_tokens, reduction}]
        commands:    [{label, calls, original_tokens, saved_tokens, reduction}]
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_schema(conn)
            where, params = _scope(session_id)

            totals = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(original_tokens), 0), "
                "COALESCE(SUM(compressed_tokens), 0), COALESCE(SUM(saved_tokens), 0), "
                "COALESCE(SUM(CASE WHEN compressor = 'identity' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN lossy = 1 THEN 1 ELSE 0 END), 0) "
                "FROM compressions" + where,
                params,
            ).fetchone()
            calls, original, compressed, saved, identity_calls, lossy_calls = totals

            per_compressor = conn.execute(
                "SELECT compressor, COUNT(*), COALESCE(SUM(original_tokens), 0), "
                "COALESCE(SUM(saved_tokens), 0) FROM compressions" + where +
                " GROUP BY compressor ORDER BY SUM(saved_tokens) DESC",
                params,
            ).fetchall()

            # Command labels are derived in Python rather than SQL: SQLite has
            # no split function, and the row counts here are tiny.
            per_command: Dict[str, List[int]] = {}
            for command, orig, sav in conn.execute(
                "SELECT command, original_tokens, saved_tokens FROM compressions" + where,
                params,
            ):
                bucket = per_command.setdefault(command_label(command), [0, 0, 0])
                bucket[0] += 1
                bucket[1] += orig
                bucket[2] += sav
        finally:
            conn.close()
    except Exception:
        return None

    def _reduction(orig: int, sav: int) -> float:
        return (sav / orig) if orig > 0 else 0.0

    commands = sorted(
        ({"label": label, "calls": c, "original_tokens": o, "saved_tokens": s,
          "reduction": _reduction(o, s)}
         for label, (c, o, s) in per_command.items()),
        key=lambda d: d["saved_tokens"], reverse=True,
    )[:top_commands]

    return {
        "calls": int(calls),
        "original_tokens": int(original),
        "compressed_tokens": int(compressed),
        "saved_tokens": int(saved),
        "reduction": _reduction(original, saved),
        "identity_calls": int(identity_calls),
        "identity_rate": (identity_calls / calls) if calls else 0.0,
        "lossy_calls": int(lossy_calls),
        "compressors": [
            {"name": name, "calls": int(c), "original_tokens": int(o),
             "saved_tokens": int(s), "reduction": _reduction(o, s)}
            for name, c, o, s in per_compressor
        ],
        "commands": commands,
    }
