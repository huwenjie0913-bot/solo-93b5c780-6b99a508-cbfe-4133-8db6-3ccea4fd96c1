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
    SpectrumDiagnosisPage,
    SpectrumDiagnosisRequest,
    SpectrumDiagnosisResult,
    ThresholdConfig,
    WindowAnalysisRequest,
    WindowAnalysisResult,
)
from .spectrum import (
    diagnose_bearings,
    find_main_peak,
    order_analysis,
    single_sided_spectrum,
    validate_uniform_sampling,
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


# ---------- 频谱诊断 ----------

def _parse_query_time(value: str | None, field: str) -> str | None:
    """GET 查询参数 ISO 8601 → SQLite UTC 存储格式 'YYYY-MM-DD HH:MM:SS'。"""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "INVALID_TIME_RANGE", f"{field} 不是合法的 ISO 8601 时间：{value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@app.post("/api/v1/spectrum/diagnoses", response_model=SpectrumDiagnosisResult, status_code=201)
def diagnose_spectrum(req: SpectrumDiagnosisRequest) -> dict:
    """以已有采样记录为输入：去直流 + Hann 窗单边 FFT，阶次换算与轴承频带能量诊断。"""
    record = db.get_record(req.record_id)
    if record is None:
        raise ApiError(404, "RECORD_NOT_FOUND", f"分析记录 {req.record_id} 不存在")

    th = db.get_threshold(record["equipment_id"]) or DEFAULT_THRESHOLDS
    thresholds = {
        "bearing_attention_ratio": (
            req.bearing_attention_ratio
            if req.bearing_attention_ratio is not None
            else th["bearing_attention_ratio"]
        ),
        "bearing_critical_ratio": (
            req.bearing_critical_ratio
            if req.bearing_critical_ratio is not None
            else th["bearing_critical_ratio"]
        ),
        "bearing_band_tolerance": (
            req.bearing_band_tolerance
            if req.bearing_band_tolerance is not None
            else th["bearing_band_tolerance"]
        ),
    }

    # 采样记录可能很长，分段取出全部样本
    samples: list[dict] = []
    offset = 0
    while True:
        batch, total = db.get_samples(req.record_id, offset=offset, limit=10000)
        samples.extend(batch)
        offset += len(batch)
        if offset >= total:
            break
    fs = float(record["sampling_frequency"])
    validate_samples(samples, int(th["min_samples"]))
    validate_uniform_sampling(samples, int(th["min_samples"]), fs)

    spec = single_sided_spectrum(samples, fs)
    main_peak = find_main_peak(spec)
    order = order_analysis(spec, req.rpm, [b.model_dump() for b in req.order_bands])
    geom = req.bearing_geometry.model_dump() if req.bearing_geometry else None
    bearing = diagnose_bearings(spec, geom, req.rpm, thresholds)

    result = {
        "equipment_id": record["equipment_id"],
        "source_record_id": req.record_id,
        "sampling_frequency": fs,
        "sample_count": spec["n"],
        "rpm": req.rpm,
        "window": "hann",
        "frequency_resolution_hz": round(spec["resolution"], 6),
        "nyquist_frequency_hz": round(spec["nyquist"], 6),
        "main_peak": main_peak,
        "order": order,
        "bearing": bearing,
        "level": bearing["status"],
    }
    diagnosis_id = db.save_spectrum_diagnosis(result)
    saved = db.get_spectrum_diagnosis(diagnosis_id)
    return {**result, "diagnosis_id": diagnosis_id, "created_at": saved["created_at"]}


@app.get("/api/v1/spectrum/diagnoses", response_model=SpectrumDiagnosisPage)
def list_spectrum_diagnoses(
    equipment_id: str | None = Query(default=None),
    start_time: str | None = Query(default=None, description="创建时间下界（ISO 8601，含）"),
    end_time: str | None = Query(default=None, description="创建时间上界（ISO 8601，含）"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """按设备、创建时间范围分页查询频谱诊断记录。"""
    start = _parse_query_time(start_time, "start_time")
    end = _parse_query_time(end_time, "end_time")
    if start is not None and end is not None and start > end:
        raise ApiError(400, "INVALID_TIME_RANGE", "start_time 不能晚于 end_time")
    items, total = db.query_spectrum_diagnoses(
        equipment_id=equipment_id, start_time=start, end_time=end, limit=limit, offset=offset
    )
    return {"total": total, "items": items}


@app.get("/api/v1/spectrum/diagnoses/{diagnosis_id}", response_model=SpectrumDiagnosisResult)
def get_spectrum_diagnosis_detail(diagnosis_id: int) -> dict:
    """查看单条频谱诊断详情（谱指标、阶次带、轴承特征频带与命中依据）。"""
    diag = db.get_spectrum_diagnosis(diagnosis_id)
    if diag is None:
        raise ApiError(404, "DIAGNOSIS_NOT_FOUND", f"频谱诊断 {diagnosis_id} 不存在")
    return diag


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
