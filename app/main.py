"""FastAPI 应用与路由。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query

from . import database as db
from .analysis import (
    DEFAULT_THRESHOLDS,
    LEVELS,
    aggregate_windows,
    compute_metrics,
    evaluate,
    evaluate_window,
    validate_samples,
)
from .errors import ApiError, register_error_handlers
from .schemas import (
    AnalysisRequest,
    AnalysisResult,
    Alarm,
    AlarmAckRequest,
    AlarmNoteRequest,
    AlarmPage,
    RecordPage,
    SamplePage,
    ThresholdConfig,
    WindowAnalysisRequest,
    WindowAnalysisResult,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="振动状态分析 API",
    version="1.0.0",
    description="旋转设备振动采样分析：RMS / 峰峰值 / 峰值因子计算，按阈值与连续超限窗口判定等级",
    lifespan=lifespan,
)
register_error_handlers(app)


# ---------- 振动分析 ----------

@app.post("/api/v1/analysis", response_model=AnalysisResult, status_code=201)
def analyze(req: AnalysisRequest) -> dict:
    """接收设备标识、采样频率与按时间排列的加速度样本，返回指标、等级与触发规则。"""
    th = db.get_threshold(req.equipment_id) or DEFAULT_THRESHOLDS
    samples = [s.model_dump() for s in req.samples]

    validate_samples(samples, int(th["min_samples"]))
    metrics = compute_metrics(samples)
    level, rules = evaluate(metrics, samples, req.sampling_frequency, th)

    record = {
        "equipment_id": req.equipment_id,
        "sampling_frequency": req.sampling_frequency,
        "sample_count": len(samples),
        **metrics,
        "level": level,
        "triggered_rules": rules,
    }
    record_id = db.save_analysis(record, samples)
    saved = db.get_record(record_id)
    return {**record, "record_id": record_id, "created_at": saved["created_at"]}


@app.get("/api/v1/analysis", response_model=RecordPage)
def list_records(
    equipment_id: str | None = Query(default=None),
    level: str | None = Query(default=None, pattern="^(normal|attention|critical)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """按设备 / 等级分页查询历史分析记录。"""
    items, total = db.query_records(equipment_id=equipment_id, level=level, limit=limit, offset=offset)
    return {"total": total, "items": items}


@app.get("/api/v1/analysis/{record_id}")
def get_record(record_id: int) -> dict:
    """查询单条分析记录详情。"""
    record = db.get_record(record_id)
    if record is None:
        raise ApiError(404, "RECORD_NOT_FOUND", f"分析记录 {record_id} 不存在")
    return record


@app.get("/api/v1/analysis/{record_id}/samples", response_model=SamplePage)
def replay_samples(
    record_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=10000),
) -> dict:
    """原始样本回放：按采样顺序返回该次分析的原始时间序列。"""
    if db.get_record(record_id) is None:
        raise ApiError(404, "RECORD_NOT_FOUND", f"分析记录 {record_id} 不存在")
    samples, total = db.get_samples(record_id, offset=offset, limit=limit)
    return {"record_id": record_id, "total": total, "offset": offset, "limit": limit, "samples": samples}


# ---------- 时间窗 × 转速工况分析 ----------

@app.post("/api/v1/windows/analysis", response_model=WindowAnalysisResult, status_code=201)
def analyze_windows(req: WindowAnalysisRequest) -> dict:
    """按时间窗与转速区间聚合带转速的样本，返回 RMS / 峰值 / 峭度等指标；越界窗口持久化告警。"""
    th = db.get_threshold(req.equipment_id) or DEFAULT_THRESHOLDS
    samples = [s.model_dump() for s in req.samples]

    validate_samples(samples, int(th["min_samples"]))
    windows = aggregate_windows(samples, req.window_seconds, req.rpm_bins)

    reference_time = req.reference_time
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)
    elif reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_iso = reference_time.astimezone(timezone.utc).isoformat()

    min_samples = int(th["window_min_samples"])
    overall_level = "normal"
    alarm_count = 0
    for w in windows:
        if w["sample_count"] < min_samples:
            w["evaluated"] = False
            w["level"] = "insufficient"
            w["triggered_rules"] = []
            w["alarm_id"] = None
            continue
        w["evaluated"] = True
        rpm_mid = (w["rpm_min"] + w["rpm_max"]) / 2
        level, rules = evaluate_window(w, th, rpm_mid)
        w["level"] = level
        w["triggered_rules"] = rules
        w["alarm_id"] = None
        if level != "normal":
            if LEVELS.index(level) > LEVELS.index(overall_level):
                overall_level = level
            top = max(rules, key=lambda r: LEVELS.index(r["level"]))
            window_time = (reference_time + timedelta(seconds=w["window_start"])).astimezone(timezone.utc).isoformat()
            w["alarm_id"] = db.create_alarm({
                "equipment_id": req.equipment_id,
                "severity": level,
                "metric": top["metric"],
                "value": top["value"],
                "threshold": top["threshold"],
                "rule": top["rule"],
                "message": (
                    f"时间窗 #{w['window_index']}（{w['window_start']:.3f}s~{w['window_end']:.3f}s，"
                    f"转速 {w['rpm_min']:.0f}~{w['rpm_max']:.0f} rpm）：{top['message']}"
                ),
                "triggered_rules": rules,
                "window_index": w["window_index"],
                "window_start": w["window_start"],
                "window_end": w["window_end"],
                "rpm_min": w["rpm_min"],
                "rpm_max": w["rpm_max"],
                "sample_count": w["sample_count"],
                "window_time": window_time,
                "reference_time": reference_iso,
            })
            alarm_count += 1

    return {
        "equipment_id": req.equipment_id,
        "window_seconds": req.window_seconds,
        "rpm_bins": req.rpm_bins,
        "window_count": len(windows),
        "sample_count": len(samples),
        "overall_level": overall_level,
        "alarm_count": alarm_count,
        "windows": windows,
    }


# ---------- 告警管理 ----------

@app.get("/api/v1/alarms", response_model=AlarmPage)
def list_alarms(
    equipment_id: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(open|acknowledged)$"),
    severity: str | None = Query(default=None, pattern="^(attention|critical)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """按设备 / 状态 / 严重级别分页查询告警，含操作留痕。"""
    items, total = db.query_alarms(
        equipment_id=equipment_id, status=status, severity=severity, limit=limit, offset=offset
    )
    return {"total": total, "items": items}


@app.get("/api/v1/alarms/{alarm_id}", response_model=Alarm)
def get_alarm(alarm_id: int) -> dict:
    """查询单条告警详情（含确认/备注事件流）。"""
    alarm = db.get_alarm(alarm_id)
    if alarm is None:
        raise ApiError(404, "ALARM_NOT_FOUND", f"告警 {alarm_id} 不存在")
    return alarm


@app.post("/api/v1/alarms/{alarm_id}/acknowledge", response_model=Alarm)
def acknowledge_alarm(alarm_id: int, req: AlarmAckRequest) -> dict:
    """确认告警（交班处理）：记录操作者与备注，仅 open 告警可确认。"""
    result = db.acknowledge_alarm(alarm_id, req.operator, req.note)
    if result is None:
        raise ApiError(404, "ALARM_NOT_FOUND", f"告警 {alarm_id} 不存在")
    if result.get("conflict"):
        raise ApiError(409, "ALARM_ALREADY_ACKNOWLEDGED", f"告警 {alarm_id} 已被确认，不能重复确认")
    return result


@app.post("/api/v1/alarms/{alarm_id}/notes", status_code=201)
def add_alarm_note(alarm_id: int, req: AlarmNoteRequest) -> dict:
    """为告警追加备注并记录操作者留痕。"""
    result = db.add_alarm_note(alarm_id, req.operator, req.note)
    if result is None:
        raise ApiError(404, "ALARM_NOT_FOUND", f"告警 {alarm_id} 不存在")
    return result["event"]


# ---------- 阈值配置 ----------

@app.put("/api/v1/thresholds/{equipment_id}", status_code=200)
def put_threshold(equipment_id: str, cfg: ThresholdConfig) -> dict:
    """创建或更新某台设备的阈值配置。"""
    return db.upsert_threshold(equipment_id, cfg.model_dump())


@app.get("/api/v1/thresholds/{equipment_id}")
def get_threshold(equipment_id: str) -> dict:
    """查询设备阈值；未配置时返回默认配置并标记 source=default。"""
    cfg = db.get_threshold(equipment_id)
    if cfg is None:
        return {"equipment_id": equipment_id, "source": "default", **DEFAULT_THRESHOLDS}
    return {"source": "custom", **cfg}


@app.get("/api/v1/thresholds")
def list_thresholds() -> dict:
    """列出所有已配置阈值的设备。"""
    return {"items": db.list_thresholds()}


@app.delete("/api/v1/thresholds/{equipment_id}", status_code=204)
def delete_threshold(equipment_id: str) -> None:
    """删除设备自定义阈值（之后分析将回落到默认配置）。"""
    if not db.delete_threshold(equipment_id):
        raise ApiError(404, "THRESHOLD_NOT_FOUND", f"设备 {equipment_id} 未配置自定义阈值")
