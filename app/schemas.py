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


# ---------- 基线对比 ----------

class DeviationThresholds(BaseModel):
    """各指标的相对偏差阈值（|新值-中位数|/|中位数|），attention 必须小于 critical。"""

    rms_attention: float = Field(default=0.15, gt=0, le=10, description="RMS 关注相对偏差")
    rms_critical: float = Field(default=0.30, gt=0, le=10, description="RMS 严重相对偏差")
    crest_factor_attention: float = Field(default=0.20, gt=0, le=10, description="峰值因子关注相对偏差")
    crest_factor_critical: float = Field(default=0.40, gt=0, le=10, description="峰值因子严重相对偏差")
    main_peak_frequency_attention: float = Field(
        default=0.05, gt=0, le=10, description="主峰频率关注相对偏差")
    main_peak_frequency_critical: float = Field(
        default=0.10, gt=0, le=10, description="主峰频率严重相对偏差")
    peak_order_attention: float = Field(default=0.05, gt=0, le=10, description="峰值阶次关注相对偏差")
    peak_order_critical: float = Field(default=0.10, gt=0, le=10, description="峰值阶次严重相对偏差")

    def to_metric_map(self) -> dict[str, dict[str, float]]:
        return {
            "rms": {"attention": self.rms_attention, "critical": self.rms_critical},
            "crest_factor": {
                "attention": self.crest_factor_attention,
                "critical": self.crest_factor_critical,
            },
            "main_peak_frequency_hz": {
                "attention": self.main_peak_frequency_attention,
                "critical": self.main_peak_frequency_critical,
            },
            "peak_order": {
                "attention": self.peak_order_attention,
                "critical": self.peak_order_critical,
            },
        }

    @model_validator(mode="after")
    def attention_below_critical(self) -> "DeviationThresholds":
        pairs = [
            ("rms_attention", "rms_critical"),
            ("crest_factor_attention", "crest_factor_critical"),
            ("main_peak_frequency_attention", "main_peak_frequency_critical"),
            ("peak_order_attention", "peak_order_critical"),
        ]
        for lo, hi in pairs:
            if getattr(self, lo) >= getattr(self, hi):
                raise ValueError(f"{lo} 必须小于 {hi}")
        return self


class BaselineCreateRequest(BaseModel):
    """基于同一设备多条已保存频谱诊断构建基线。"""

    source_diagnosis_ids: list[int] | None = Field(
        default=None, min_length=1, description="来源诊断 ID；不传则取设备全部诊断")
    rpm_bins: list[float] | None = Field(
        default=None, min_length=2, description="转速分组边界（升序，左闭右开）；不传则按相同转速精确归组")
    min_samples_per_group: int = Field(
        default=3, ge=1, le=10_000, description="每个转速分组参与统计所需的最少诊断条数")
    start_time: datetime | None = Field(default=None, description="来源诊断创建时间下界（ISO 8601，含）")
    end_time: datetime | None = Field(default=None, description="来源诊断创建时间上界（ISO 8601，含）")
    deviation_thresholds: DeviationThresholds | None = Field(
        default=None, description="偏差阈值配置；不传使用内置默认值")
    effective_from: datetime | None = Field(
        default=None, description="基线生效时间（ISO 8601）；缺省取当前 UTC 时间")
    operator: str = Field(default="system", min_length=1, max_length=64, description="创建操作者")
    note: str = Field(default="", max_length=1000, description="创建备注（检修说明等）")

    @field_validator("source_diagnosis_ids")
    @classmethod
    def ids_positive(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(i < 1 for i in v):
            raise ValueError("source_diagnosis_ids 必须为正整数")
        return v

    @field_validator("rpm_bins")
    @classmethod
    def bins_strictly_increasing(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return v
        if any(not math.isfinite(x) for x in v):
            raise ValueError("转速分组边界必须是有限数值")
        if any(v[i] >= v[i + 1] for i in range(len(v) - 1)):
            raise ValueError("转速分组边界必须严格递增")
        return v

    @model_validator(mode="after")
    def time_range_ordered(self) -> "BaselineCreateRequest":
        if self.start_time is not None and self.end_time is not None and self.start_time > self.end_time:
            raise ValueError("start_time 不能晚于 end_time")
        return self


class BaselineStatusRequest(BaseModel):
    """启用 / 停用基线的操作者留痕。"""

    operator: str = Field(min_length=1, max_length=64, description="操作者标识")
    note: str = Field(default="", max_length=1000, description="操作备注")


class BaselineCompareRequest(BaseModel):
    """把一条已保存的频谱诊断与基线（同工况转速分组）对比。"""

    diagnosis_id: int = Field(ge=1, description="待对比的已保存频谱诊断 ID")
    deviation_thresholds: DeviationThresholds | None = Field(
        default=None, description="临时覆盖该基线保存的偏差阈值")


class BaselineMetricStat(BaseModel):
    median: float
    min: float
    max: float


class BaselineRpmGroup(BaseModel):
    group_index: int
    rpm_min: float
    rpm_max: float
    label: str
    sample_count: int
    metrics: dict[str, BaselineMetricStat]
    source_diagnosis_ids: list[int]


class BaselineAuditEvent(BaseModel):
    id: int
    baseline_id: int
    event_type: Literal["created", "activated", "deactivated"]
    operator: str
    note: str
    details: dict
    created_at: str


class BaselineResult(BaseModel):
    baseline_id: int
    equipment_id: str
    version: int
    status: Literal["active", "inactive", "superseded"]
    grouping_type: Literal["exact", "bins"]
    rpm_bins: list[float] | None
    groups: list[BaselineRpmGroup]
    deviation_thresholds: dict
    source_diagnosis_ids: list[int]
    source_count: int
    sample_count_total: int
    min_samples_per_group: int
    effective_from: str
    note: str
    created_by: str
    created_at: str
    updated_at: str
    events: list[BaselineAuditEvent] = []


class BaselineDetail(BaselineResult):
    """基线详情，创建接口额外回传样本数不足被跳过的转速分组。"""

    skipped_groups: list[dict] = []


class BaselinePage(BaseModel):
    total: int
    items: list[BaselineResult]


class MetricComparison(BaseModel):
    metric: str
    label: str
    unit: str
    value: float
    baseline_median: float
    baseline_min: float
    baseline_max: float
    deviation: float
    change_rate: float | None
    within_dispersion: bool
    attention_threshold: float
    critical_threshold: float
    level: Literal["normal", "attention", "critical"]
    message: str


class BaselineComparison(BaseModel):
    diagnosis_id: int
    equipment_id: str
    rpm: float
    baseline_id: int
    baseline_version: int
    baseline_status: Literal["active", "inactive", "superseded"]
    group_index: int
    rpm_min: float
    rpm_max: float
    overall_level: Literal["normal", "attention", "critical"]
    metrics: list[MetricComparison]
    compared_at: str


# ---------- 变速阶次跟踪 ----------

class OrderTrackingRequest(BaseModel):
    """以已保存振动记录为输入的变速阶次跟踪请求。"""

    record_id: int = Field(ge=1, description="已有采样分析记录 ID")
    pulse_times: list[float] = Field(min_length=2, description="严格递增的转速脉冲时间（秒，与记录同时基）")
    pulses_per_revolution: int = Field(
        ge=1, le=10_000, description="每转脉冲数（键相为 1，齿轮盘按齿数）")
    samples_per_revolution: int = Field(
        default=64, ge=4, le=100_000, description="等角度重采样密度：每转采样点数（决定角域奈奎斯特）")
    window_revolutions: float = Field(
        default=10.0, gt=0, le=100_000, description="分析窗长度（转）")
    overlap_revolutions: float = Field(
        default=0.0, ge=0, description="相邻分析窗重叠转数（须小于窗长）")
    order_bands: list[OrderBandIn] = Field(
        default_factory=list, description="关注阶次带（如 1X/2X），用于能量占比与共振区间合并")
    resonance_ratio_threshold: float = Field(
        default=0.2, gt=0, lt=1, description="任一阶次带能量占比超过该值即标记为共振候选窗")
    min_consecutive_windows: int = Field(
        default=3, ge=1, le=10_000, description="合并共振转速区间所需的最少连续命中窗数")

    @field_validator("pulse_times")
    @classmethod
    def pulse_times_finite(cls, v: list[float]) -> list[float]:
        if any(not math.isfinite(x) for x in v):
            raise ValueError("转速脉冲时间必须为有限数值，不允许 NaN/Inf")
        return v

    @model_validator(mode="after")
    def window_and_band_params_consistent(self) -> "OrderTrackingRequest":
        if self.overlap_revolutions >= self.window_revolutions:
            raise ValueError("overlap_revolutions 必须小于 window_revolutions")
        if round(self.window_revolutions * self.samples_per_revolution) < 4:
            raise ValueError("窗长与每转采样点数乘积不足 4 点，无法计算阶次谱")
        return self


class PulseSummary(BaseModel):
    """转速脉冲摘要：时间范围、间隔统计与由相邻脉冲推算的瞬时转速。"""

    pulse_count: int
    first_pulse_time: float
    last_pulse_time: float
    covered_revolutions: float
    pulse_interval_min_s: float
    pulse_interval_max_s: float
    pulse_interval_mean_s: float
    instantaneous_rpm_min: float
    instantaneous_rpm_max: float
    instantaneous_rpm_mean: float


class OrderTrackingBandResult(BaseModel):
    name: str
    order_min: float
    order_max: float
    energy_ratio: float | None = None


class OrderTrackingWindow(BaseModel):
    """单个按转数切分的分析窗的阶次谱摘要。"""

    window_index: int
    t_start: float
    t_end: float
    duration_seconds: float
    revolution_start: float
    revolution_end: float
    average_rpm: float
    order_resolution: float
    sample_count: int
    main_peak_order: float
    main_peak_amplitude: float
    bands: list[OrderTrackingBandResult]


class ResonanceRpmZone(BaseModel):
    """连续命中窗合并得到的共振转速区间。"""

    zone_index: int
    window_start_index: int
    window_end_index: int
    window_count: int
    t_start: float
    t_end: float
    rpm_min: float
    rpm_max: float
    average_rpm: float
    main_peak_order_min: float
    main_peak_order_max: float
    dominant_band: str | None
    max_energy_ratio: float


class OrderTrackingResult(BaseModel):
    tracking_id: int
    equipment_id: str
    source_record_id: int
    sampling_frequency: float
    sample_count: int
    pulse_count: int
    pulses_per_revolution: int
    samples_per_revolution: int
    window_revolutions: float
    overlap_revolutions: float
    order_resolution: float
    order_nyquist: float
    resampled_sample_count: int
    resonance_ratio_threshold: float
    min_consecutive_windows: int
    order_bands: list[OrderTrackingBandResult]
    pulse_summary: PulseSummary
    window_count: int
    windows: list[OrderTrackingWindow]
    resonance_zones: list[ResonanceRpmZone]
    created_at: str | None = None


class OrderTrackingPage(BaseModel):
    total: int
    items: list[dict]


# ---------- 包络解调诊断 ----------

class CarrierBandIn(BaseModel):
    """显式指定的载波解调频带（Hz）。"""

    low_hz: float = Field(ge=0, description="载波频带下界 Hz（不得越过奈奎斯特）")
    high_hz: float = Field(gt=0, description="载波频带上界 Hz")

    @model_validator(mode="after")
    def low_below_high(self) -> "CarrierBandIn":
        if not (math.isfinite(self.low_hz) and math.isfinite(self.high_hz)):
            raise ValueError("载波频带边界必须为有限数值")
        if self.low_hz >= self.high_hz:
            raise ValueError("low_hz 必须小于 high_hz")
        return self


class AutoBandIn(BaseModel):
    """峭度自动选带参数：在候选区域内按固定带宽生成候选带，按滤波后峭度选带。"""

    region_min_hz: float | None = Field(
        default=None, ge=0, description="候选区域下界 Hz；缺省取 0.02·奈奎斯特")
    region_max_hz: float | None = Field(
        default=None, gt=0, description="候选区域上界 Hz；缺省取奈奎斯特频率")
    band_width_hz: float | None = Field(
        default=None, gt=0, description="候选带宽 Hz；缺省取 0.2·奈奎斯特")

    @model_validator(mode="after")
    def region_ordered(self) -> "AutoBandIn":
        if (self.region_min_hz is not None and self.region_max_hz is not None
                and self.region_min_hz >= self.region_max_hz):
            raise ValueError("region_min_hz 必须小于 region_max_hz")
        for name in ("region_min_hz", "region_max_hz", "band_width_hz"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} 必须为有限数值")
        return self


class EnvelopeDiagnosisRequest(BaseModel):
    """以已保存采样记录、转速和轴承几何参数为输入的包络解调诊断请求。"""

    record_id: int = Field(ge=1, description="已有采样分析记录 ID")
    rpm: float = Field(gt=0, le=1_000_000, description="本次采样期间的恒定转速（转/分）")
    bearing_geometry: BearingGeometryIn | None = Field(
        default=None, description="轴承几何参数；不提供时故障族匹配标记 unavailable")
    carrier_band: CarrierBandIn | None = Field(
        default=None, description="显式载波频带；与 auto_band 二选一，均缺省时按默认区域峭度自动选带")
    auto_band: AutoBandIn | None = Field(
        default=None, description="自动选带参数；与 carrier_band 同时提供时拒绝")
    max_harmonics: int = Field(
        default=4, ge=1, le=50, description="每个故障族匹配的最高谐波次数")
    sideband_orders: list[int] = Field(
        default_factory=lambda: [1], description="1X 边带阶次（转频倍数），缺省 [1]")
    match_tolerance_hz: float | None = Field(
        default=None, gt=0, le=10000, description="谱峰匹配半宽 Hz；缺省 2.0")
    envelope_attention_ratio: float | None = Field(
        default=None, gt=0, lt=1, description="覆盖设备配置：故障族能量占比关注阈值")
    envelope_critical_ratio: float | None = Field(
        default=None, gt=0, lt=1, description="覆盖设备配置：故障族能量占比严重阈值")

    @model_validator(mode="after")
    def request_consistent(self) -> "EnvelopeDiagnosisRequest":
        if self.carrier_band is not None and self.auto_band is not None:
            raise ValueError("carrier_band 与 auto_band 不能同时指定，请二选一")
        if (self.envelope_attention_ratio is not None
                and self.envelope_critical_ratio is not None
                and self.envelope_attention_ratio >= self.envelope_critical_ratio):
            raise ValueError("envelope_attention_ratio 必须小于 envelope_critical_ratio")
        if any(o < 1 for o in self.sideband_orders):
            raise ValueError("sideband_orders 阶次必须为不小于 1 的正整数")
        if len(set(self.sideband_orders)) != len(self.sideband_orders):
            raise ValueError("sideband_orders 不能包含重复阶次")
        return self


class EnvelopePeakMatch(BaseModel):
    fault: str
    kind: Literal["fundamental", "harmonic", "lower_sideband", "upper_sideband"]
    harmonic: int
    sideband_order: int
    target_frequency_hz: float
    peak_frequency_hz: float
    peak_amplitude: float
    deviation_hz: float
    matched: bool


class EnvelopeSidebandHit(BaseModel):
    fault: str
    kind: Literal["lower_sideband", "upper_sideband"]
    harmonic: int
    sideband_order: int
    target_frequency_hz: float
    peak_frequency_hz: float
    peak_amplitude: float
    deviation_hz: float
    matched: bool


class EnvelopeFaultFamily(BaseModel):
    fault: str
    name: str
    characteristic_frequency_hz: float
    energy_ratio: float
    level: Literal["normal", "attention", "critical"]
    confidence: Literal["high", "medium", "low", "none"]
    fundamental_matched: bool
    matched_harmonic_orders: list[int]
    matched_sideband_harmonics: list[int]
    lower_sideband_hits: list[EnvelopeSidebandHit]
    upper_sideband_hits: list[EnvelopeSidebandHit]
    peak_matches: list[EnvelopePeakMatch]
    message: str


class CarrierBandResult(BaseModel):
    low_hz: float
    high_hz: float
    band_kurtosis: float


class AutoBandCandidate(BaseModel):
    band_min_hz: float
    band_max_hz: float
    kurtosis: float


class BandSelectionResult(BaseModel):
    mode: Literal["manual", "auto"]
    region_min_hz: float | None
    region_max_hz: float | None
    band_width_hz: float
    candidate_count: int
    selected_index: int
    selected_band_min_hz: float
    selected_band_max_hz: float
    selected_kurtosis: float | None
    candidates: list[AutoBandCandidate]
    reason: str


class EnvelopeDiagnosisResult(BaseModel):
    envelope_id: int
    equipment_id: str
    source_record_id: int
    sampling_frequency: float
    sample_count: int
    rpm: float
    shaft_frequency_hz: float
    nyquist_frequency_hz: float
    frequency_resolution_hz: float
    carrier_band: CarrierBandResult
    band_selection: BandSelectionResult
    max_harmonics: int
    sideband_orders: list[int]
    match_tolerance_hz: float
    attention_ratio: float
    critical_ratio: float
    bearing_geometry: dict | None
    characteristic_frequencies_hz: dict[str, float]
    fault_families: list[EnvelopeFaultFamily]
    peak_matches: list[EnvelopePeakMatch]
    dominant_fault: str | None
    confidence: Literal["high", "medium", "low", "none"]
    level: Literal["normal", "attention", "critical", "unavailable"]
    status: Literal["normal", "attention", "critical", "unavailable"]
    reason: str | None = None
    missing_fields: list[str] = []
    conclusion: str
    created_at: str | None = None


class EnvelopeDiagnosisPage(BaseModel):
    total: int
    items: list[dict]
