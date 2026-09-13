"""Pydantic 请求/响应模型。缺字段、类型错误由 FastAPI 统一转为 422。"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SampleIn(BaseModel):
    """单个加速度样本：t 为时间戳（秒，相对或绝对均可），a 为加速度（m/s²）。"""

    t: float
    a: float

    @field_validator("t", "a")
    @classmethod
    def must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("样本值必须是有限数值，不允许 NaN/Inf")
        return v


class AnalysisRequest(BaseModel):
    equipment_id: str = Field(min_length=1, max_length=64, description="设备标识")
    sampling_frequency: float = Field(gt=0, le=1_000_000, description="采样频率 Hz")
    samples: list[SampleIn] = Field(min_length=1, description="按时间排列的加速度样本")


class ThresholdConfig(BaseModel):
    """设备阈值配置。attention 为“关注”线，critical 为“严重”线。"""

    rms_attention: float = Field(gt=0, description="RMS 关注阈值 m/s²")
    rms_critical: float = Field(gt=0, description="RMS 严重阈值 m/s²")
    p2p_attention: float = Field(gt=0, description="峰峰值关注阈值 m/s²")
    p2p_critical: float = Field(gt=0, description="峰峰值严重阈值 m/s²")
    crest_attention: float = Field(gt=1, description="峰值因子关注阈值")
    crest_critical: float = Field(gt=1, description="峰值因子严重阈值")
    peak_attention: float = Field(gt=0, description="瞬时幅值关注阈值 m/s²（连续超限判定用）")
    peak_critical: float = Field(gt=0, description="瞬时幅值严重阈值 m/s²（连续超限判定用）")
    window_seconds: float = Field(gt=0, le=3600, description="连续超限窗口时长（秒）")
    min_samples: int = Field(default=8, ge=2, le=1_000_000, description="分析所需最少样本数")

    @model_validator(mode="after")
    def attention_below_critical(self) -> "ThresholdConfig":
        pairs = [
            ("rms_attention", "rms_critical"),
            ("p2p_attention", "p2p_critical"),
            ("crest_attention", "crest_critical"),
            ("peak_attention", "peak_critical"),
        ]
        for lo, hi in pairs:
            if getattr(self, lo) >= getattr(self, hi):
                raise ValueError(f"{lo} 必须小于 {hi}")
        return self


class TriggeredRule(BaseModel):
    rule: str
    metric: str
    value: float
    threshold: float
    level: str
    message: str


class AnalysisResult(BaseModel):
    record_id: int
    equipment_id: str
    sampling_frequency: float
    sample_count: int
    duration_seconds: float
    rms: float
    peak_to_peak: float
    crest_factor: float
    peak_abs: float
    level: Literal["normal", "attention", "critical"]
    triggered_rules: list[TriggeredRule]
    created_at: str | None = None


class RecordPage(BaseModel):
    total: int
    items: list[dict]


class SamplePage(BaseModel):
    record_id: int
    total: int
    offset: int
    limit: int
    samples: list[dict]
