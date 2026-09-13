"""FastAPI 应用与路由。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from . import database as db
from .analysis import DEFAULT_THRESHOLDS, compute_metrics, evaluate, validate_samples
from .errors import ApiError, register_error_handlers
from .schemas import (
    AnalysisRequest,
    AnalysisResult,
    RecordPage,
    SamplePage,
    ThresholdConfig,
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
