"""频谱诊断核心：去直流 + Hann 窗的单边 FFT、阶次换算、轴承特征频带能量判定。"""
from __future__ import annotations

import math

import numpy as np

from .errors import ApiError

# 频带能量占比阈值默认值（可按设备在阈值配置中覆盖）
DEFAULT_BEARING_THRESHOLDS: dict = {
    "bearing_attention_ratio": 0.05,
    "bearing_critical_ratio": 0.15,
    "bearing_band_tolerance": 0.02,
}

# BPFO/BPFI/BSF/FTF 推导所需的轴承几何字段
BEARING_GEOMETRY_FIELDS = ("ball_count", "ball_diameter", "pitch_diameter", "contact_angle")

# 各轴承特征频带的元信息：(结果键, 中文名, 疑似故障称谓)
BEARING_FAULTS = (
    ("bpfo", "外圈故障频率 BPFO", "外圈故障"),
    ("bpfi", "内圈故障频率 BPFI", "内圈故障"),
    ("bsf", "滚动体故障频率 BSF", "滚动体故障"),
    ("ftf", "保持架故障频率 FTF", "保持架故障"),
)


def _r6(v: float) -> float:
    """统一保留 6 位小数，便于结果稳定展示。"""
    return round(float(v), 6)


def validate_uniform_sampling(samples: list[dict], min_samples: int, sampling_frequency: float | None = None) -> None:
    """校验样本数量与采样间隔一致性，返回前由调用方保证时间严格递增。"""
    if len(samples) < min_samples:
        raise ApiError(
            400,
            "INSUFFICIENT_SAMPLES",
            f"样本数量不足：收到 {len(samples)} 个，至少需要 {min_samples} 个",
            {"received": len(samples), "required": min_samples},
        )
    times = np.asarray([s["t"] for s in samples], dtype=float)
    dt = np.diff(times)
    nominal = float(np.median(dt))
    tol = max(1e-9, 1e-6 * abs(nominal))
    mismatch = np.abs(dt - nominal) > tol
    if np.any(mismatch):
        bad = int(np.argmax(mismatch))
        raise ApiError(
            400,
            "NON_UNIFORM_SAMPLING",
            f"采样间隔不一致：第 {bad + 1} 个间隔为 {dt[bad]:.6g}s，"
            f"与标称间隔 {nominal:.6g}s 偏差超过容差",
            {
                "index": bad + 1,
                "interval": _r6(dt[bad]),
                "nominal_interval": _r6(nominal),
            },
        )
    if sampling_frequency is not None:
        declared = 1.0 / sampling_frequency
        if abs(nominal - declared) > max(1e-9, 1e-6 * declared):
            raise ApiError(
                400,
                "NON_UNIFORM_SAMPLING",
                f"采样间隔 {nominal:.6g}s 与记录声明的采样频率 {sampling_frequency:g} Hz "
                f"（间隔 {declared:.6g}s）不一致",
                {
                    "index": -1,
                    "nominal_interval": _r6(nominal),
                    "declared_interval": _r6(declared),
                    "sampling_frequency": sampling_frequency,
                },
            )


def check_frequency_band(lo: float, hi: float, nyquist: float, label: str) -> None:
    """频带边界校验：必须为正、下界小于上界、不得越过奈奎斯特频率。"""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ApiError(400, "INVALID_FREQUENCY_BAND", f"{label} 边界必须为有限数值",
                       {"label": label, "lo": lo, "hi": hi, "nyquist": _r6(nyquist)})
    if lo < 0:
        raise ApiError(400, "INVALID_FREQUENCY_BAND", f"{label} 下界不能为负",
                       {"label": label, "lo": lo, "hi": hi, "nyquist": _r6(nyquist)})
    if lo >= hi:
        raise ApiError(400, "INVALID_FREQUENCY_BAND", f"{label} 下界必须小于上界",
                       {"label": label, "lo": lo, "hi": hi, "nyquist": _r6(nyquist)})
    if hi > nyquist + 1e-12:
        raise ApiError(
            400,
            "BAND_OUT_OF_RANGE",
            f"{label}（{lo:.4g}~{hi:.4g} Hz）越过奈奎斯特频率 {nyquist:.4g} Hz",
            {"label": label, "lo": lo, "hi": hi, "nyquist": _r6(nyquist)},
        )


def single_sided_spectrum(samples: list[dict], sampling_frequency: float) -> dict:
    """去直流并加 Hann 窗后执行单边 FFT。

    返回频率轴、幅值谱（按 Hann 窗 coherent gain 2.0 修正，端点/奈奎斯特点不翻倍）、
    单边功率谱（用于能量占比）与频率分辨率。
    """
    values = np.asarray([s["a"] for s in samples], dtype=float)
    n = values.size
    window = np.hanning(n)
    windowed = (values - values.mean()) * window

    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(n, d=1.0 / sampling_frequency)

    amplitude = 2.0 * np.abs(spectrum) / window.sum()
    amplitude[0] = np.abs(spectrum[0]) / window.sum()
    if n % 2 == 0:
        amplitude[-1] = np.abs(spectrum[-1]) / window.sum()

    power = (np.abs(spectrum) ** 2) / (window ** 2).sum()
    return {
        "n": n,
        "fs": sampling_frequency,
        "freqs": freqs,
        "amplitude": amplitude,
        "power": power,
        "resolution": sampling_frequency / n,
        "nyquist": sampling_frequency / 2.0,
    }


def find_main_peak(spec: dict) -> dict:
    """在谱线中找主峰（跳过 0 Hz 直流线）。"""
    freqs, amp = spec["freqs"], spec["amplitude"]
    search = amp[1:]
    idx = int(np.argmax(search)) + 1
    return {
        "frequency_hz": _r6(freqs[idx]),
        "amplitude": _r6(amp[idx]),
        "bin_index": idx,
    }


def band_energy_ratio(spec: dict, lo: float, hi: float) -> float:
    """频带 [lo, hi] 内功率占全谱（直流除外）功率的比例。"""
    freqs, power = spec["freqs"], spec["power"]
    mask = (freqs >= lo) & (freqs <= hi)
    total = power[1:].sum()
    if total <= 0:
        return 0.0
    return float(power[mask].sum() / total)


def order_analysis(spec: dict, rpm: float, order_bands: list[dict]) -> dict:
    """按恒定转速把频谱换算为阶次（order = f / (rpm/60)），并汇总用户指定阶次带能量占比。"""
    shaft_hz = rpm / 60.0
    order_axis = spec["freqs"] / shaft_hz
    peak_order = spec["freqs"][find_main_peak(spec)["bin_index"]] / shaft_hz

    bands: list[dict] = []
    for ob in order_bands:
        lo_o, hi_o = ob["order_min"], ob["order_max"]
        check_frequency_band(lo_o * shaft_hz, hi_o * shaft_hz, spec["nyquist"],
                             f"阶次带 {label_for_band(ob)}")
        ratio = band_energy_ratio(spec, lo_o * shaft_hz, hi_o * shaft_hz)
        bands.append({
            "name": ob.get("name") or label_for_band(ob),
            "order_min": _r6(lo_o),
            "order_max": _r6(hi_o),
            "frequency_min_hz": _r6(lo_o * shaft_hz),
            "frequency_max_hz": _r6(hi_o * shaft_hz),
            "energy_ratio": _r6(ratio),
        })
    return {
        "rpm": rpm,
        "shaft_frequency_hz": _r6(shaft_hz),
        "peak_order": _r6(peak_order),
        "max_order": _r6(order_axis[-1]),
        "bands": bands,
    }


def label_for_band(ob: dict) -> str:
    return ob.get("name") or f"{ob['order_min']:g}~{ob['order_max']:g} 阶"


def bearing_characteristic_frequencies(geom: dict, rpm: float) -> dict:
    """由轴承几何推导 BPFO / BPFI / BSF / FTF（接触角以度为单位）。"""
    n = geom["ball_count"]
    d = geom["ball_diameter"]
    d_m = geom["pitch_diameter"]
    cos_a = math.cos(math.radians(geom["contact_angle"]))
    fr = rpm / 60.0
    ratio_term = d * cos_a / d_m
    return {
        "bpfo": fr * n / 2.0 * (1.0 - ratio_term),
        "bpfi": fr * n / 2.0 * (1.0 + ratio_term),
        "bsf": fr * d_m / d * (1.0 - ratio_term ** 2),
        "ftf": fr / 2.0 * (1.0 - ratio_term),
    }


def missing_geometry_fields(geom: dict | None) -> list[str]:
    """返回缺失的轴承几何字段（字段存在但取值非法由 Pydantic 422 拦截）。"""
    if not geom:
        return list(BEARING_GEOMETRY_FIELDS)
    return [f for f in BEARING_GEOMETRY_FIELDS if geom.get(f) is None]


def diagnose_bearings(
    spec: dict,
    geom: dict | None,
    rpm: float,
    thresholds: dict,
) -> dict:
    """轴承频带能量诊断。

    以 BPFO/BPFI/BSF/FTF 为中心、按 ``bearing_band_tolerance`` 相对容差取频带，
    依据频带能量占比阈值给出 normal / attention / critical 与命中依据；
    任一派生频带（含容差半宽）越过奈奎斯特频率时直接拒绝，避免把无法评估的
    特征频带保存为 normal。
    """
    missing = missing_geometry_fields(geom)
    if missing:
        return {
            "status": "unavailable",
            "reason": "缺少轴承几何参数，无法推导 BPFO/BPFI/BSF/FTF：" + "、".join(missing),
            "missing_fields": missing,
            "characteristic_frequencies_hz": {},
            "bands": [],
            "hits": [],
            "level": None,
            "attention_ratio": _r6(thresholds["bearing_attention_ratio"]),
            "critical_ratio": _r6(thresholds["bearing_critical_ratio"]),
            "band_tolerance": _r6(thresholds["bearing_band_tolerance"]),
        }

    attention = thresholds["bearing_attention_ratio"]
    critical = thresholds["bearing_critical_ratio"]
    tol = thresholds["bearing_band_tolerance"]
    freqs = bearing_characteristic_frequencies(geom, rpm)

    # 越界预检：任一派生频带越过奈奎斯特都无法评估，直接拒绝整次请求
    out_of_range: list[dict] = []
    for key, cn_name, _ in BEARING_FAULTS:
        fc = freqs[key]
        lo, hi = fc * (1.0 - tol), fc * (1.0 + tol)
        if lo <= 0 or hi > spec["nyquist"] + 1e-12:
            out_of_range.append({
                "fault": key,
                "name": cn_name,
                "center_frequency_hz": _r6(fc),
                "band_min_hz": _r6(lo),
                "band_max_hz": _r6(hi),
            })
    if out_of_range:
        names = "、".join(f"{d['name'].split(' ')[-1]}({d['center_frequency_hz']:.2f} Hz)"
                         for d in out_of_range)
        raise ApiError(
            400,
            "BAND_OUT_OF_RANGE",
            f"轴承特征频带 {names} 越过奈奎斯特频率 {spec['nyquist']:.2f} Hz，无法评估；"
            "请降低转速或提高采样频率",
            {"nyquist": _r6(spec["nyquist"]), "bands": out_of_range},
        )

    bands: list[dict] = []
    hits: list[dict] = []
    levels: list[str] = []
    for key, cn_name, fault_name in BEARING_FAULTS:
        fc = freqs[key]
        lo, hi = fc * (1.0 - tol), fc * (1.0 + tol)
        ratio = band_energy_ratio(spec, lo, hi)
        if ratio >= critical:
            level = "critical"
            message = (f"{cn_name} {fc:.2f} Hz ±{tol:.0%} 频带能量占比 {ratio:.2%} "
                       f"达到严重阈值 {critical:.0%}，疑似{fault_name}，建议停机检查")
        elif ratio >= attention:
            level = "attention"
            message = (f"{cn_name} {fc:.2f} Hz ±{tol:.0%} 频带能量占比 {ratio:.2%} "
                       f"超过关注阈值 {attention:.0%}，建议安排复检")
        else:
            level = "normal"
            message = (f"{cn_name} {fc:.2f} Hz ±{tol:.0%} 频带能量占比 {ratio:.2%}，低于关注阈值")
        bands.append({
            "fault": key,
            "name": cn_name,
            "center_frequency_hz": _r6(fc),
            "band_min_hz": _r6(lo),
            "band_max_hz": _r6(hi),
            "energy_ratio": _r6(ratio),
            "level": level,
            "message": message,
        })
        levels.append(level)
        if level in ("attention", "critical"):
            hits.append({
                "fault": key,
                "name": cn_name,
                "level": level,
                "center_frequency_hz": _r6(fc),
                "energy_ratio": _r6(ratio),
                "threshold": _r6(critical if level == "critical" else attention),
                "message": message,
            })

    overall = "critical" if "critical" in levels else (
        "attention" if "attention" in levels else "normal")
    return {
        "status": overall,
        "reason": None,
        "missing_fields": [],
        "characteristic_frequencies_hz": {k: _r6(v) for k, v in freqs.items()},
        "bands": bands,
        "hits": hits,
        "level": overall,
        "attention_ratio": _r6(attention),
        "critical_ratio": _r6(critical),
        "band_tolerance": _r6(tol),
    }
