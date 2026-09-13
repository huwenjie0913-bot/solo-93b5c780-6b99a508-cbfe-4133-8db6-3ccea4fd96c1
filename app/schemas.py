"""Pydantic 请求/响应模型。缺字段、类型错误由 FastAPI 统一转为 422。"""
from __future__ import annotations

import math
from datetime import datetime
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


class WindowSampleIn(BaseModel):
    """带转速的窗口样本：rpm 为瞬时转速（转/分）。"""

    t: float
    a: float
    rpm: float

    @field_validator("t", "a", "rpm")
    @classmethod
    def must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("样本值必须是有限数值，不允许 NaN/Inf")
        return v

    @field_validator("rpm")
    @classmethod
    def rpm_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("转速不能为负数")
        return v


class AnalysisRequest(BaseModel):
    equipment_id: str = Field(min_length=1, max_length=64, description="设备标识")
    sampling_frequency: float = Field(gt=0, le=1_000_000, description="采样频率 Hz")
    samples: list[SampleIn] = Field(min_length=1, description="按时间排列的加速度样本")


class WindowAnalysisRequest(BaseModel):
    """时间窗 × 转速工况分析请求。"""

    equipment_id: str = Field(min_length=1, max_length=64, description="设备标识")
    window_seconds: float = Field(default=1.0, gt=0, le=3600, description="时间窗长度（秒）")
    rpm_bins: list[float] = Field(min_length=2, description="转速区间边界（升序，区间左闭右开）")
    reference_time: datetime | None = Field(
        default=None, description="t=0 对应的绝对时间（ISO 8601），用于告警时间戳；缺省取当前 UTC 时间"
    )
    samples: list[WindowSampleIn] = Field(min_length=1, description="按时间排列、含转速的样本")

    @field_validator("rpm_bins")
    @classmethod
    def bins_strictly_increasing(cls, v: list[float]) -> list[float]:
        if any(v[i] >= v[i + 1] for i in range(len(v) - 1)):
            raise ValueError("转速区间边界必须严格递增")
        if any(not math.isfinite(x) for x in v):
            raise ValueError("转速区间边界必须是有限数值")
        return v


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
    kurtosis_attention: float = Field(default=4.0, gt=0, description="窗口峭度关注阈值")
    kurtosis_critical: float = Field(default=8.0, gt=0, description="窗口峭度严重阈值")
    bearing_attention_ratio: float = Field(
        default=0.05, gt=0, lt=1, description="轴承特征频带能量占比关注阈值")
    bearing_critical_ratio: float = Field(
        default=0.15, gt=0, lt=1, description="轴承特征频带能量占比严重阈值")
    bearing_band_tolerance: float = Field(
        default=0.02, gt=0, lt=1, description="轴承特征频带相对中心频率的半宽（±比例）")
    window_seconds: float = Field(gt=0, le=3600, description="连续超限窗口时长（秒）")
    window_min_samples: int = Field(default=4, ge=1, le=1_000_000, description="窗口指标判定所需最少样本数")
    min_samples: int = Field(default=8, ge=2, le=1_000_000, description="分析所需最少样本数")

    @model_validator(mode="after")
    def attention_below_critical(self) -> "ThresholdConfig":
        pairs = [
            ("rms_attention", "rms_critical"),
            ("p2p_attention", "p2p_critical"),
            ("crest_attention", "crest_critical"),
            ("peak_attention", "peak_critical"),
            ("kurtosis_attention", "kurtosis_critical"),
            ("bearing_attention_ratio", "bearing_critical_ratio"),
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


class WindowMetrics(BaseModel):
    """单个“时间窗 × 转速区间”的聚合结果。"""

    window_index: int
    window_start: float
    window_end: float
    rpm_bin_index: int
    rpm_min: float
    rpm_max: float
    t_start: float
    t_end: float
    sample_count: int
    rms: float
    peak_abs: float
    kurtosis: float
    peak_to_peak: float
    evaluated: bool
    level: Literal["normal", "attention", "critical", "insufficient"]
    triggered_rules: list[TriggeredRule] = []
    alarm_id: int | None = None


class WindowAnalysisResult(BaseModel):
    equipment_id: str
    window_seconds: float
    rpm_bins: list[float]
    window_count: int
    sample_count: int
    overall_level: Literal["normal", "attention", "critical"]
    alarm_count: int
    windows: list[WindowMetrics]


class AlarmAckRequest(BaseModel):
    """确认告警（交接给下一班）。"""

    operator: str = Field(min_length=1, max_length=64, description="操作者标识")
    note: str = Field(default="", max_length=1000, description="确认备注")


class AlarmNoteRequest(BaseModel):
    """为告警追加备注。"""

    operator: str = Field(min_length=1, max_length=64, description="操作者标识")
    note: str = Field(min_length=1, max_length=1000, description="备注内容")


class AlarmEvent(BaseModel):
    id: int
    alarm_id: int
    event_type: Literal["created", "acknowledged", "noted"]
    operator: str
    note: str
    created_at: str


class Alarm(BaseModel):
    id: int
    equipment_id: str
    severity: Literal["attention", "critical"]
    status: Literal["open", "acknowledged"]
    metric: str
    value: float
    threshold: float
    rule: str
    message: str
    triggered_rules: list[dict]
    window_index: int
    window_start: float
    window_end: float
    rpm_min: float
    rpm_max: float
    sample_count: int
    window_time: str
    reference_time: str
    created_at: str
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    note: str
    events: list[AlarmEvent] = []


class AlarmPage(BaseModel):
    total: int
    items: list[Alarm]


# ---------- 频谱诊断 ----------

class OrderBandIn(BaseModel):
    """用户指定的阶次带（以转频的倍数为单位）。"""

    name: str | None = Field(default=None, max_length=64, description="阶次带名称，如 1X/2X/叶片通过")
    order_min: float = Field(gt=0, description="阶次下界（转频倍数，>0）")
    order_max: float = Field(gt=0, description="阶次上界")

    @model_validator(mode="after")
    def min_below_max(self) -> "OrderBandIn":
        if self.order_min >= self.order_max:
            raise ValueError("order_min 必须小于 order_max")
        return self


class BearingGeometryIn(BaseModel):
    """轴承几何参数：n 个滚动体、滚动体直径 d、节径 D、接触角 α（度）。

    字段均为可选：只提供部分几何参数时轴承诊断标记 unavailable 并说明缺少的字段；
    字段齐全后才进行物理关系校验。
    """

    ball_count: int | None = Field(default=None, gt=0, le=1000, description="滚动体数量 n")
    ball_diameter: float | None = Field(default=None, gt=0, description="滚动体直径 d（mm）")
    pitch_diameter: float | None = Field(default=None, gt=0, description="轴承节径 D（mm）")
    contact_angle: float | None = Field(default=None, ge=0, le=90, description="接触角 α（度）")

    @model_validator(mode="after")
    def diameter_within_pitch(self) -> "BearingGeometryIn":
        if self.ball_diameter is not None and self.pitch_diameter is not None:
            if self.ball_diameter >= self.pitch_diameter:
                raise ValueError("滚动体直径 ball_diameter 必须小于节径 pitch_diameter")
        return self


class SpectrumDiagnosisRequest(BaseModel):
    """以已有采样记录为输入的频谱诊断请求。"""

    record_id: int = Field(ge=1, description="已有采样分析记录 ID")
    rpm: float = Field(gt=0, le=1_000_000, description="本次采样期间的恒定转速（转/分）")
    order_bands: list[OrderBandIn] = Field(default_factory=list, description="用户指定的阶次带")
    bearing_geometry: BearingGeometryIn | None = Field(
        default=None, description="轴承几何参数；不提供时轴承诊断标记 unavailable")
    bearing_attention_ratio: float | None = Field(
        default=None, gt=0, lt=1, description="覆盖设备配置：频带能量占比关注阈值")
    bearing_critical_ratio: float | None = Field(
        default=None, gt=0, lt=1, description="覆盖设备配置：频带能量占比严重阈值")
    bearing_band_tolerance: float | None = Field(
        default=None, gt=0, lt=1, description="覆盖设备配置：特征频带相对半宽")

    @model_validator(mode="after")
    def bearing_thresholds_ordered(self) -> "SpectrumDiagnosisRequest":
        if (self.bearing_attention_ratio is not None and self.bearing_critical_ratio is not None
                and self.bearing_attention_ratio >= self.bearing_critical_ratio):
            raise ValueError("bearing_attention_ratio 必须小于 bearing_critical_ratio")
        return self


class SpectrumPeak(BaseModel):
    frequency_hz: float
    amplitude: float
    bin_index: int


class OrderBandResult(BaseModel):
    name: str
    order_min: float
    order_max: float
    frequency_min_hz: float
    frequency_max_hz: float
    energy_ratio: float


class OrderAnalysisResult(BaseModel):
    rpm: float
    shaft_frequency_hz: float
    peak_order: float
    max_order: float
    bands: list[OrderBandResult]


class BearingBandResult(BaseModel):
    fault: str
    name: str
    center_frequency_hz: float
    band_min_hz: float
    band_max_hz: float
    energy_ratio: float | None
    level: Literal["normal", "attention", "critical", "out_of_range"]
    message: str


class BearingHit(BaseModel):
    fault: str
    name: str
    level: Literal["attention", "critical"]
    center_frequency_hz: float
    energy_ratio: float
    threshold: float
    message: str


class BearingDiagnosis(BaseModel):
    status: Literal["normal", "attention", "critical", "unavailable"]
    reason: str | None = None
    missing_fields: list[str] = []
    characteristic_frequencies_hz: dict[str, float] = {}
    bands: list[BearingBandResult] = []
    hits: list[BearingHit] = []
    level: Literal["normal", "attention", "critical"] | None = None
    attention_ratio: float
    critical_ratio: float
    band_tolerance: float


class SpectrumDiagnosisResult(BaseModel):
    diagnosis_id: int
    equipment_id: str
    source_record_id: int
    sampling_frequency: float
    sample_count: int
    rpm: float
    window: Literal["hann"]
    frequency_resolution_hz: float
    nyquist_frequency_hz: float
    main_peak: SpectrumPeak
    order: OrderAnalysisResult
    bearing: BearingDiagnosis
    level: Literal["normal", "attention", "critical", "unavailable"]
    created_at: str | None = None


class SpectrumDiagnosisPage(BaseModel):
    total: int
    items: list[dict]
