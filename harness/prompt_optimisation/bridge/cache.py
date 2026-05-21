from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from filelock import FileLock


_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    prompt_hash  TEXT NOT NULL,
    repo_id      TEXT NOT NULL,
    score        REAL NOT NULL,
    result_json  TEXT NOT NULL,
    evaluated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (prompt_hash, repo_id)
);
"""


class EvalCache:
    def __init__(self, db_path: Path) -> None:
        self._db = db_path
        self._lock = FileLock(str(db_path) + ".lock")
        with self._lock:
            conn = sqlite3.connect(self._db)
            conn.execute(_SCHEMA)
            conn.commit()
            conn.close()

    def get(self, prompt_hash: str, repo_id: str) -> float | None:
        with self._lock:
            conn = sqlite3.connect(self._db)
            row = conn.execute(
                "SELECT score FROM results WHERE prompt_hash=? AND repo_id=?",
                (prompt_hash, repo_id),
            ).fetchone()
            conn.close()
        return row[0] if row else None

    def put(self, prompt_hash: str, repo_id: str, score: float, result: Any) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db)
            conn.execute(
                "INSERT OR REPLACE INTO results (prompt_hash, repo_id, score, result_json) "
                "VALUES (?, ?, ?, ?)",
                (prompt_hash, repo_id, score, json.dumps(result)),
            )
            conn.commit()
            conn.close()

    def count(self) -> int:
        with self._lock:
            conn = sqlite3.connect(self._db)
            n = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            conn.close()
        return n
