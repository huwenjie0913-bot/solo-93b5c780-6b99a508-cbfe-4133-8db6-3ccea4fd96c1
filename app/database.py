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
    bearing_attention_ratio REAL NOT NULL DEFAULT 0.05,
    bearing_critical_ratio  REAL NOT NULL DEFAULT 0.15,
    bearing_band_tolerance  REAL NOT NULL DEFAULT 0.02,
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

CREATE TABLE IF NOT EXISTS spectrum_diagnoses (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id         TEXT NOT NULL,
    source_record_id     INTEGER NOT NULL REFERENCES analysis_records(id),
    sampling_frequency   REAL NOT NULL,
    sample_count         INTEGER NOT NULL,
    rpm                  REAL NOT NULL,
    window_type          TEXT NOT NULL DEFAULT 'hann',
    frequency_resolution REAL NOT NULL,
    nyquist_frequency    REAL NOT NULL,
    main_peak            TEXT NOT NULL,
    order_result         TEXT NOT NULL,
    bearing              TEXT NOT NULL,
    level                TEXT NOT NULL CHECK (level IN ('normal', 'attention', 'critical', 'unavailable')),
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS baselines (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id           TEXT NOT NULL,
    version                INTEGER NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'inactive', 'superseded')),
    grouping_type          TEXT NOT NULL CHECK (grouping_type IN ('exact', 'bins')),
    rpm_bins               TEXT,
    groups                 TEXT NOT NULL,
    deviation_thresholds   TEXT NOT NULL,
    source_diagnosis_ids   TEXT NOT NULL,
    source_count           INTEGER NOT NULL,
    sample_count_total     INTEGER NOT NULL,
    min_samples_per_group  INTEGER NOT NULL,
    effective_from         TEXT NOT NULL,
    note                   TEXT NOT NULL DEFAULT '',
    created_by             TEXT NOT NULL DEFAULT 'system',
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (equipment_id, version)
);

CREATE TABLE IF NOT EXISTS order_tracking_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id         TEXT NOT NULL,
    source_record_id     INTEGER NOT NULL REFERENCES analysis_records(id),
    sampling_frequency   REAL NOT NULL,
    sample_count         INTEGER NOT NULL,
    pulse_count          INTEGER NOT NULL,
    pulses_per_revolution INTEGER NOT NULL,
    samples_per_revolution INTEGER NOT NULL,
    window_revolutions   REAL NOT NULL,
    overlap_revolutions  REAL NOT NULL,
    order_resolution     REAL NOT NULL,
    order_nyquist        REAL NOT NULL,
    resampled_sample_count INTEGER NOT NULL,
    resonance_ratio_threshold REAL NOT NULL,
    min_consecutive_windows INTEGER NOT NULL,
    order_bands          TEXT NOT NULL DEFAULT '[]',
    pulse_summary        TEXT NOT NULL,
    window_count         INTEGER NOT NULL DEFAULT 0,
    windows              TEXT NOT NULL,
    resonance_zones      TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS baseline_audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_id INTEGER NOT NULL REFERENCES baselines(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN ('created', 'activated', 'deactivated')),
    operator    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_records_equipment ON analysis_records(equipment_id, id);
CREATE INDEX IF NOT EXISTS idx_alarms_query ON alarms(equipment_id, status, severity, id);
CREATE INDEX IF NOT EXISTS idx_alarm_events ON alarm_events(alarm_id, id);
CREATE INDEX IF NOT EXISTS idx_spectrum_query ON spectrum_diagnoses(equipment_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_order_tracking_query ON order_tracking_results(equipment_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_baselines_query ON baselines(equipment_id, status, version);
CREATE INDEX IF NOT EXISTS idx_baseline_events ON baseline_audit_events(baseline_id, id);
"""

# 旧库增量迁移：为 thresholds 补齐窗口/轴承指标列
THRESHOLD_MIGRATIONS = {
    "kurtosis_attention": "ALTER TABLE thresholds ADD COLUMN kurtosis_attention REAL NOT NULL DEFAULT 4.0",
    "kurtosis_critical": "ALTER TABLE thresholds ADD COLUMN kurtosis_critical REAL NOT NULL DEFAULT 8.0",
    "window_min_samples": "ALTER TABLE thresholds ADD COLUMN window_min_samples INTEGER NOT NULL DEFAULT 4",
    "bearing_attention_ratio": "ALTER TABLE thresholds ADD COLUMN bearing_attention_ratio REAL NOT NULL DEFAULT 0.05",
    "bearing_critical_ratio": "ALTER TABLE thresholds ADD COLUMN bearing_critical_ratio REAL NOT NULL DEFAULT 0.15",
    "bearing_band_tolerance": "ALTER TABLE thresholds ADD COLUMN bearing_band_tolerance REAL NOT NULL DEFAULT 0.02",
}

# 新表增量迁移：为早期 order_tracking_results 补窗口计数列
ORDER_TRACKING_MIGRATIONS = {
    "window_count": "ALTER TABLE order_tracking_results ADD COLUMN window_count INTEGER NOT NULL DEFAULT 0",
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
        ot = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='order_tracking_results'").fetchone()
        if ot is not None:
            existing_ot = {r["name"] for r in conn.execute(
                "PRAGMA table_info(order_tracking_results)").fetchall()}
            for col, ddl in ORDER_TRACKING_MIGRATIONS.items():
                if col not in existing_ot:
                    conn.execute(ddl)


# ---------- 阈值 ----------

def upsert_threshold(equipment_id: str, cfg: dict) -> dict:
    cols = [
        "rms_attention", "rms_critical", "p2p_attention", "p2p_critical",
        "crest_attention", "crest_critical", "peak_attention", "peak_critical",
        "kurtosis_attention", "kurtosis_critical",
        "bearing_attention_ratio", "bearing_critical_ratio", "bearing_band_tolerance",
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


# ---------- 频谱诊断 ----------

def save_spectrum_diagnosis(diag: dict) -> int:
    """持久化一次频谱诊断结果（主峰、阶次、轴承诊断以 JSON 存储）。"""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO spectrum_diagnoses
               (equipment_id, source_record_id, sampling_frequency, sample_count, rpm,
                window_type, frequency_resolution, nyquist_frequency,
                main_peak, order_result, bearing, level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                diag["equipment_id"], diag["source_record_id"], diag["sampling_frequency"],
                diag["sample_count"], diag["rpm"], diag.get("window", "hann"),
                diag["frequency_resolution_hz"], diag["nyquist_frequency_hz"],
                json.dumps(diag["main_peak"], ensure_ascii=False),
                json.dumps(diag["order"], ensure_ascii=False),
                json.dumps(diag["bearing"], ensure_ascii=False),
                diag["level"],
            ),
        )
        return int(cur.lastrowid)


def _row_to_diagnosis(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["diagnosis_id"] = d.pop("id")
    d["window"] = d.pop("window_type")
    d["frequency_resolution_hz"] = d.pop("frequency_resolution")
    d["nyquist_frequency_hz"] = d.pop("nyquist_frequency")
    d["main_peak"] = json.loads(d["main_peak"])
    d["order"] = json.loads(d["order_result"])
    d.pop("order_result", None)
    d["bearing"] = json.loads(d["bearing"])
    return d


def get_spectrum_diagnosis(diagnosis_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM spectrum_diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()
    return _row_to_diagnosis(row) if row else None


def query_spectrum_diagnoses(
    equipment_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """按设备、创建时间范围（SQLite UTC，'YYYY-MM-DD HH:MM:SS'）分页查询频谱诊断。"""
    where, params = [], []
    if equipment_id:
        where.append("equipment_id = ?")
        params.append(equipment_id)
    if start_time:
        where.append("created_at >= ?")
        params.append(start_time)
    if end_time:
        where.append("created_at <= ?")
        params.append(end_time)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM spectrum_diagnoses {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM spectrum_diagnoses {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [_row_to_diagnosis(r) for r in rows], total


# ---------- 变速阶次跟踪 ----------

def save_order_tracking(result: dict) -> int:
    """持久化一次变速阶次跟踪分析（参数、脉冲摘要、分析窗、共振区间以 JSON 存储）。"""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO order_tracking_results
               (equipment_id, source_record_id, sampling_frequency, sample_count,
                pulse_count, pulses_per_revolution, samples_per_revolution,
                window_revolutions, overlap_revolutions, order_resolution, order_nyquist,
                resampled_sample_count, resonance_ratio_threshold, min_consecutive_windows,
                order_bands, pulse_summary, window_count, windows, resonance_zones)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["equipment_id"], result["source_record_id"], result["sampling_frequency"],
                result["sample_count"], result["pulse_count"], result["pulses_per_revolution"],
                result["samples_per_revolution"], result["window_revolutions"],
                result["overlap_revolutions"], result["order_resolution"], result["order_nyquist"],
                result["resampled_sample_count"], result["resonance_ratio_threshold"],
                result["min_consecutive_windows"],
                json.dumps(result["order_bands"], ensure_ascii=False),
                json.dumps(result["pulse_summary"], ensure_ascii=False),
                result["window_count"],
                json.dumps(result["windows"], ensure_ascii=False),
                json.dumps(result["resonance_zones"], ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def _row_to_tracking(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tracking_id"] = d.pop("id")
    for key in ("order_bands", "pulse_summary", "windows", "resonance_zones"):
        d[key] = json.loads(d[key])
    return d


def get_order_tracking(tracking_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM order_tracking_results WHERE id = ?", (tracking_id,)
        ).fetchone()
    return _row_to_tracking(row) if row else None


def query_order_tracking(
    equipment_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """按设备、创建时间范围（SQLite UTC，'YYYY-MM-DD HH:MM:SS'）分页查询阶次跟踪结果。"""
    where, params = [], []
    if equipment_id:
        where.append("equipment_id = ?")
        params.append(equipment_id)
    if start_time:
        where.append("created_at >= ?")
        params.append(start_time)
    if end_time:
        where.append("created_at <= ?")
        params.append(end_time)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM order_tracking_results {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM order_tracking_results {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [_row_to_tracking(r) for r in rows], total


# ---------- 振动基线 ----------

def get_baseline_sources(
    equipment_id: str,
    diagnosis_ids: list[int] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[dict]:
    """取构建基线所需的诊断 + 源分析记录（按诊断 id 升序，保证构建可复现）。"""
    where = ["d.equipment_id = ?"]
    params: list = [equipment_id]
    if diagnosis_ids:
        where.append(f"d.id IN ({', '.join('?' * len(diagnosis_ids))})")
        params.extend(diagnosis_ids)
    if start_time:
        where.append("d.created_at >= ?")
        params.append(start_time)
    if end_time:
        where.append("d.created_at <= ?")
        params.append(end_time)
    clause = " AND ".join(where)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT d.id AS diagnosis_id, d.rpm, d.main_peak, d.order_result,
                       d.source_record_id, r.rms, r.crest_factor
                FROM spectrum_diagnoses d
                JOIN analysis_records r ON r.id = d.source_record_id
                WHERE {clause}
                ORDER BY d.id""",
            params,
        ).fetchall()
    sources = []
    for row in rows:
        main_peak = json.loads(row["main_peak"])
        order_result = json.loads(row["order_result"])
        sources.append({
            "diagnosis_id": row["diagnosis_id"],
            "source_record_id": row["source_record_id"],
            "rpm": row["rpm"],
            "main_peak_frequency_hz": main_peak["frequency_hz"],
            "peak_order": order_result["peak_order"],
            "rms": row["rms"],
            "crest_factor": row["crest_factor"],
        })
    return sources


def _row_to_baseline(row: sqlite3.Row, events: list[dict] | None = None) -> dict:
    d = dict(row)
    d["baseline_id"] = d.pop("id")
    d["rpm_bins"] = json.loads(d["rpm_bins"]) if d["rpm_bins"] else None
    d["groups"] = json.loads(d["groups"])
    d["deviation_thresholds"] = json.loads(d["deviation_thresholds"])
    d["source_diagnosis_ids"] = json.loads(d["source_diagnosis_ids"])
    d["events"] = events or []
    return d


def _load_baseline_events(conn: sqlite3.Connection, baseline_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, baseline_id, event_type, operator, note, details, created_at "
        "FROM baseline_audit_events WHERE baseline_id = ? ORDER BY id",
        (baseline_id,),
    ).fetchall()
    events = []
    for r in rows:
        e = dict(r)
        e["details"] = json.loads(e["details"]) if e["details"] else {}
        events.append(e)
    return events


def create_baseline(
    equipment_id: str,
    groups: list[dict],
    grouping_type: str,
    rpm_bins: list[float] | None,
    deviation_thresholds: dict,
    source_diagnosis_ids: list[int],
    source_count: int,
    sample_count_total: int,
    min_samples_per_group: int,
    effective_from: str,
    note: str,
    operator: str,
    audit_details: dict,
) -> dict:
    """创建新基线（同设备版本号自增），并把该设备既有 active 基线置为 superseded。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM baselines WHERE equipment_id = ?",
            (equipment_id,),
        ).fetchone()
        version = int(row["next_version"])
        conn.execute("UPDATE baselines SET status = 'superseded', updated_at = datetime('now') "
                     "WHERE equipment_id = ? AND status = 'active'", (equipment_id,))
        cur = conn.execute(
            """INSERT INTO baselines
               (equipment_id, version, status, grouping_type, rpm_bins, groups,
                deviation_thresholds, source_diagnosis_ids, source_count, sample_count_total,
                min_samples_per_group, effective_from, note, created_by)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                equipment_id, version, grouping_type,
                json.dumps(rpm_bins, ensure_ascii=False) if rpm_bins is not None else None,
                json.dumps(groups, ensure_ascii=False),
                json.dumps(deviation_thresholds, ensure_ascii=False),
                json.dumps(sorted(source_diagnosis_ids), ensure_ascii=False),
                source_count, sample_count_total, min_samples_per_group,
                effective_from, note, operator,
            ),
        )
        baseline_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO baseline_audit_events (baseline_id, event_type, operator, note, details) "
            "VALUES (?, 'created', ?, ?, ?)",
            (baseline_id, operator, note, json.dumps(audit_details, ensure_ascii=False)),
        )
        row = conn.execute("SELECT * FROM baselines WHERE id = ?", (baseline_id,)).fetchone()
        return _row_to_baseline(row, _load_baseline_events(conn, baseline_id))


def get_baseline(baseline_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM baselines WHERE id = ?", (baseline_id,)).fetchone()
        if row is None:
            return None
        return _row_to_baseline(row, _load_baseline_events(conn, baseline_id))


def get_active_baseline(equipment_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM baselines WHERE equipment_id = ? AND status = 'active' "
            "ORDER BY version DESC LIMIT 1",
            (equipment_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_baseline(row, _load_baseline_events(conn, row["id"]))


def query_baselines(
    equipment_id: str | None = None,
    status: str | None = None,
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
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM baselines {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM baselines {clause} ORDER BY equipment_id, version DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [_row_to_baseline(r) for r in rows], total


def set_baseline_status(
    baseline_id: int,
    active: bool,
    operator: str,
    note: str,
    details: dict,
) -> dict | None:
    """启用/停用基线；重复同态操作返回 None（调用方映射为 409），事件与状态在同一事务落库。"""
    new_status = "active" if active else "inactive"
    event_type = "activated" if active else "deactivated"
    with connect() as conn:
        row = conn.execute("SELECT * FROM baselines WHERE id = ?", (baseline_id,)).fetchone()
        if row is None:
            return None
        if row["status"] == new_status:
            return {"conflict": True}
        if active:
            conn.execute("UPDATE baselines SET status = 'superseded', updated_at = datetime('now') "
                         "WHERE equipment_id = ? AND status = 'active'", (row["equipment_id"],))
        conn.execute(
            "UPDATE baselines SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, baseline_id),
        )
        conn.execute(
            "INSERT INTO baseline_audit_events (baseline_id, event_type, operator, note, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (baseline_id, event_type, operator, note, json.dumps(details, ensure_ascii=False)),
        )
        row = conn.execute("SELECT * FROM baselines WHERE id = ?", (baseline_id,)).fetchone()
        return _row_to_baseline(row, _load_baseline_events(conn, baseline_id))
