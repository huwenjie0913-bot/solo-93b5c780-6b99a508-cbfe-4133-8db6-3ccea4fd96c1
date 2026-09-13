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
    kurtosis_attention REAL NOT NULL DEFAULT 4.0,
    kurtosis_critical  REAL NOT NULL DEFAULT 8.0,
    window_min_samples  INTEGER NOT NULL DEFAULT 4,
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

CREATE TABLE IF NOT EXISTS alarms (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id   TEXT NOT NULL,
    severity       TEXT NOT NULL CHECK (severity IN ('attention', 'critical')),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged')),
    metric         TEXT NOT NULL,
    value          REAL NOT NULL,
    threshold      REAL NOT NULL,
    rule           TEXT NOT NULL,
    message        TEXT NOT NULL,
    triggered_rules TEXT NOT NULL DEFAULT '[]',
    window_index   INTEGER NOT NULL,
    window_start   REAL NOT NULL,
    window_end     REAL NOT NULL,
    rpm_min        REAL NOT NULL,
    rpm_max        REAL NOT NULL,
    sample_count   INTEGER NOT NULL,
    window_time    TEXT NOT NULL,
    reference_time TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alarm_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alarm_id   INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('created', 'acknowledged', 'noted')),
    operator   TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_records_equipment ON analysis_records(equipment_id, id);
CREATE INDEX IF NOT EXISTS idx_alarms_query ON alarms(equipment_id, status, severity, id);
CREATE INDEX IF NOT EXISTS idx_alarm_events ON alarm_events(alarm_id, id);
"""

# 旧库增量迁移：为 thresholds 补齐窗口指标列
THRESHOLD_MIGRATIONS = {
    "kurtosis_attention": "ALTER TABLE thresholds ADD COLUMN kurtosis_attention REAL NOT NULL DEFAULT 4.0",
    "kurtosis_critical": "ALTER TABLE thresholds ADD COLUMN kurtosis_critical REAL NOT NULL DEFAULT 8.0",
    "window_min_samples": "ALTER TABLE thresholds ADD COLUMN window_min_samples INTEGER NOT NULL DEFAULT 4",
}


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
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(thresholds)").fetchall()}
        for col, ddl in THRESHOLD_MIGRATIONS.items():
            if col not in existing:
                conn.execute(ddl)


# ---------- 阈值 ----------

def upsert_threshold(equipment_id: str, cfg: dict) -> dict:
    cols = [
        "rms_attention", "rms_critical", "p2p_attention", "p2p_critical",
        "crest_attention", "crest_critical", "peak_attention", "peak_critical",
        "kurtosis_attention", "kurtosis_critical",
        "window_seconds", "window_min_samples", "min_samples",
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


# ---------- 告警与操作者留痕 ----------

def create_alarm(alarm: dict) -> int:
    """持久化一条越界告警，并写入 created 事件（同一事务）。"""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO alarms
               (equipment_id, severity, metric, value, threshold, rule, message, triggered_rules,
                window_index, window_start, window_end, rpm_min, rpm_max, sample_count,
                window_time, reference_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                alarm["equipment_id"], alarm["severity"], alarm["metric"], alarm["value"],
                alarm["threshold"], alarm["rule"], alarm["message"],
                json.dumps(alarm["triggered_rules"], ensure_ascii=False),
                alarm["window_index"], alarm["window_start"], alarm["window_end"],
                alarm["rpm_min"], alarm["rpm_max"], alarm["sample_count"],
                alarm["window_time"], alarm["reference_time"],
            ),
        )
        alarm_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO alarm_events (alarm_id, event_type, operator, note) VALUES (?, 'created', 'system', '')",
            (alarm_id,),
        )
    return alarm_id


def _row_to_alarm(row: sqlite3.Row, events: list[dict] | None = None) -> dict:
    d = dict(row)
    d["triggered_rules"] = json.loads(d["triggered_rules"]) if d["triggered_rules"] else []
    d["events"] = events or []
    return d


def _load_events(conn: sqlite3.Connection, alarm_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, alarm_id, event_type, operator, note, created_at "
        "FROM alarm_events WHERE alarm_id = ? ORDER BY id",
        (alarm_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_alarm(alarm_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        if row is None:
            return None
        return _row_to_alarm(row, _load_events(conn, alarm_id))


def query_alarms(
    equipment_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = [], []
    if equipment_id:
        where.append("equipment_id = ?")
        params.append(equipment_id)
    if status:
        where.append("status = ?")
        params.append(status)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM alarms {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM alarms {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_alarm(r, _load_events(conn, r["id"])) for r in rows], total


def acknowledge_alarm(alarm_id: int, operator: str, note: str) -> dict | None:
    """确认告警：仅 open 状态可确认，成功后写入 acknowledged 事件；重复确认返回 None。"""
    with connect() as conn:
        row = conn.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != "open":
            return {"conflict": True}
        conn.execute(
            """UPDATE alarms
               SET status = 'acknowledged', acknowledged_at = datetime('now'),
                   acknowledged_by = ?, note = CASE WHEN ? != '' THEN ? ELSE note END
               WHERE id = ?""",
            (operator, note, note, alarm_id),
        )
        conn.execute(
            "INSERT INTO alarm_events (alarm_id, event_type, operator, note) VALUES (?, 'acknowledged', ?, ?)",
            (alarm_id, operator, note),
        )
        row = conn.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        return _row_to_alarm(row, _load_events(conn, alarm_id))


def add_alarm_note(alarm_id: int, operator: str, note: str) -> dict | None:
    """为告警追加备注（最新备注同时写入 alarms.note 便于列表展示），并留痕。"""
    with connect() as conn:
        row = conn.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE alarms SET note = ? WHERE id = ?",
            (note, alarm_id),
        )
        conn.execute(
            "INSERT INTO alarm_events (alarm_id, event_type, operator, note) VALUES (?, 'noted', ?, ?)",
            (alarm_id, operator, note),
        )
        event = conn.execute(
            "SELECT id, alarm_id, event_type, operator, note, created_at "
            "FROM alarm_events WHERE alarm_id = ? ORDER BY id DESC LIMIT 1",
            (alarm_id,),
        ).fetchone()
        return {"event": dict(event)}
