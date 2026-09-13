"""SQLite 持久化层：阈值配置、分析记录、原始样本。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS thresholds (
    equipment_id     TEXT PRIMARY KEY,
    rms_attention    REAL NOT NULL,
    rms_critical     REAL NOT NULL,
    p2p_attention    REAL NOT NULL,
    p2p_critical     REAL NOT NULL,
    crest_attention  REAL NOT NULL,
    crest_critical   REAL NOT NULL,
    peak_attention   REAL NOT NULL,
    peak_critical    REAL NOT NULL,
    window_seconds   REAL NOT NULL,
    min_samples      INTEGER NOT NULL,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analysis_records (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id       TEXT NOT NULL,
    sampling_frequency REAL NOT NULL,
    sample_count       INTEGER NOT NULL,
    duration_seconds   REAL NOT NULL,
    rms                REAL NOT NULL,
    peak_to_peak       REAL NOT NULL,
    crest_factor       REAL NOT NULL,
    peak_abs           REAL NOT NULL,
    level              TEXT NOT NULL,
    triggered_rules    TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS samples (
    record_id INTEGER NOT NULL REFERENCES analysis_records(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    t         REAL NOT NULL,
    a         REAL NOT NULL,
    PRIMARY KEY (record_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_records_equipment ON analysis_records(equipment_id, id);
"""


def _db_path() -> str:
    return os.environ.get("VIBRATION_DB", os.path.join(os.getcwd(), "vibration.db"))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------- 阈值 ----------

def upsert_threshold(equipment_id: str, cfg: dict) -> dict:
    cols = [
        "rms_attention", "rms_critical", "p2p_attention", "p2p_critical",
        "crest_attention", "crest_critical", "peak_attention", "peak_critical",
        "window_seconds", "min_samples",
    ]
    values = [cfg[c] for c in cols]
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO thresholds (equipment_id, {', '.join(cols)}, updated_at)
                VALUES (?, {', '.join('?' * len(cols))}, datetime('now'))
                ON CONFLICT(equipment_id) DO UPDATE SET
                {', '.join(f'{c}=excluded.{c}' for c in cols)}, updated_at=datetime('now')""",
            [equipment_id, *values],
        )
    return get_threshold(equipment_id)  # type: ignore[return-value]


def get_threshold(equipment_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM thresholds WHERE equipment_id = ?", (equipment_id,)).fetchone()
    return dict(row) if row else None


def list_thresholds() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM thresholds ORDER BY equipment_id").fetchall()
    return [dict(r) for r in rows]


def delete_threshold(equipment_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM thresholds WHERE equipment_id = ?", (equipment_id,))
        return cur.rowcount > 0


# ---------- 分析记录与样本 ----------

def save_analysis(record: dict, samples: list[dict]) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO analysis_records
               (equipment_id, sampling_frequency, sample_count, duration_seconds,
                rms, peak_to_peak, crest_factor, peak_abs, level, triggered_rules)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["equipment_id"], record["sampling_frequency"], record["sample_count"],
                record["duration_seconds"], record["rms"], record["peak_to_peak"],
                record["crest_factor"], record["peak_abs"], record["level"],
                json.dumps(record["triggered_rules"], ensure_ascii=False),
            ),
        )
        record_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO samples (record_id, idx, t, a) VALUES (?, ?, ?, ?)",
            [(record_id, i, s["t"], s["a"]) for i, s in enumerate(samples)],
        )
    return int(record_id)


def _row_to_record(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["triggered_rules"] = json.loads(d["triggered_rules"])
    return d


def get_record(record_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM analysis_records WHERE id = ?", (record_id,)).fetchone()
    return _row_to_record(row) if row else None


def query_records(
    equipment_id: str | None = None,
    level: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = [], []
    if equipment_id:
        where.append("equipment_id = ?")
        params.append(equipment_id)
    if level:
        where.append("level = ?")
        params.append(level)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM analysis_records {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM analysis_records {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [_row_to_record(r) for r in rows], total


def get_samples(record_id: int, offset: int = 0, limit: int = 1000) -> tuple[list[dict], int]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM samples WHERE record_id = ?", (record_id,)).fetchone()["c"]
        rows = conn.execute(
            "SELECT idx, t, a FROM samples WHERE record_id = ? ORDER BY idx LIMIT ? OFFSET ?",
            (record_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total
