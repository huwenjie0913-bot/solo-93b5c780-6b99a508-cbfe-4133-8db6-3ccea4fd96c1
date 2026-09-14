"""包络解调诊断核心：带通选带、Hilbert 包络、去直流包络谱与轴承故障族匹配。

早期轴承缺陷产生的周期性冲击能量很弱，在原始频谱中被转频成分和结构共振带淹没，
直接按特征频率频带统计能量占比难以说明调制来源。包络解调的处理链为：

1. 在载波频带（共振带）上做 FFT 零相移理想带通；
2. Hilbert 变换构造解析信号，取模得到包络，凸显冲击的周期调制；
3. 包络去直流后做单边 FFT 得到包络谱；
4. 在包络谱上匹配 BPFO / BPFI / BSF / FTF 的基频、谐波与 1X（转频）边带。

载波频带可由请求显式指定，也可在合法候选频带中按滤波后信号峭度自动选带
（峭度对冲击成分敏感，共振解调常用 Kurtogram 式思路的简化版本）。
"""
from __future__ import annotations

import math

import numpy as np

from .errors import ApiError
from .spectrum import (
    BEARING_FAULTS,
    bearing_characteristic_frequencies,
    check_frequency_band,
    missing_geometry_fields,
)

# 自动选带的默认载波区域与候选带宽（相对奈奎斯特的比例）
DEFAULT_AUTO_REGION_RATIO = (0.02, 1.0)
DEFAULT_AUTO_BAND_WIDTH_RATIO = 0.2
MAX_AUTO_CANDIDATES = 2000

# 匹配参数缺省值
DEFAULT_MATCH_TOLERANCE_HZ = 2.0
DEFAULT_MAX_HARMONICS = 4
DEFAULT_SIDEBAND_ORDERS = (1,)

# 各故障族默认能量占比阈值（包络谱匹配窗并集，可按设备在阈值配置中覆盖）
DEFAULT_ENVELOPE_THRESHOLDS: dict = {
    "envelope_attention_ratio": 0.05,
    "envelope_critical_ratio": 0.15,
}

# 置信等级中文表述
CONFIDENCE_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "none": "无",
}

# 各故障族 1X 边带的物理含义（写入中文判据）
SIDEBAND_HINT = {
    "bpfi": "内圈缺陷随轴转动，冲击幅值受转频调制的典型表现",
    "bsf": "滚动体缺陷同时受保持架与转频调制的典型表现",
    "bpfo": "冲击强度受载荷区周期调制，需结合谐波复核",
    "ftf": "保持架转动对其他部件冲击的调制特征",
}


def _r6(v: float) -> float:
    """统一保留 6 位小数，便于结果稳定展示。"""
    return round(float(v), 6)


def kurtosis(values: np.ndarray) -> float:
    """四阶矩峭度（非超额，与时间窗指标一致：正态分布约为 3），恒值信号返回 0。"""
    n = values.size
    if n < 2:
        return 0.0
    mean = float(values.mean())
    m2 = float(np.mean((values - mean) ** 2))
    if m2 <= 0.0:
        return 0.0
    m4 = float(np.mean((values - mean) ** 4))
    return m4 / (m2 * m2)


def bandpass_filter(values: np.ndarray, fs: float, low_hz: float, high_hz: float) -> np.ndarray:
    """FFT 零相移理想带通：在 rFFT 频谱上保留 (low, high) 内谱线后逆变换。

    矩形频响在带沿会有振铃，但包络分析关心的是带内幅度调制而非时域波形，
    且无需引入 scipy，单频载波解调结果与物理预期一致。
    """
    n = values.size
    spectrum = np.fft.rfft(values)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    mask = (freqs > low_hz) & (freqs < high_hz)
    filtered = np.fft.irfft(spectrum * mask, n=n)
    return filtered


def hilbert_envelope(values: np.ndarray) -> np.ndarray:
    """Hilbert 包络：x + j·H(x) 的模；输入先去均值，避免直流泄漏到包络谱低频。"""
    centered = values - float(values.mean())
    analytic = _analytic_signal(centered)
    return np.abs(analytic)


def _analytic_signal(values: np.ndarray) -> np.ndarray:
    """构造解析信号（scipy.signal.hilbert 的 NumPy 等价实现）。"""
    n = values.size
    spectrum = np.fft.fft(values)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(spectrum * h)


def envelope_spectrum(envelope: np.ndarray, fs: float) -> dict:
    """去直流包络的单边幅值/功率谱。

    包络首先减去自身均值（去除载波能量泄漏的直流分量），不加窗
    （矩形窗，避免 Hann 窗把离散故障谱线能量摊宽到匹配窗外）。
    """
    x = envelope - float(envelope.mean())
    n = x.size
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    amplitude = 2.0 * np.abs(spectrum) / n
    amplitude[0] = np.abs(spectrum[0]) / n
    if n % 2 == 0:
        amplitude[-1] = np.abs(spectrum[-1]) / n
    power = np.abs(spectrum) ** 2 / (n * n)
    return {
        "n": n,
        "freqs": freqs,
        "amplitude": amplitude,
        "power": power,
        "resolution": fs / n,
        "nyquist": fs / 2.0,
    }


# ---------- 载波频带选择 ----------

def _check_auto_region(region_min: float, region_max: float, nyquist: float) -> None:
    if not (math.isfinite(region_min) and math.isfinite(region_max)):
        raise ApiError(400, "INVALID_AUTO_BAND_CONFIG",
                       "自动选带区域边界必须为有限数值",
                       {"region_min_hz": region_min, "region_max_hz": region_max})
    if region_min < 0 or region_max <= 0 or region_min >= region_max:
        raise ApiError(400, "INVALID_AUTO_BAND_CONFIG",
                       "自动选带区域必须满足 0 ≤ region_min < region_max",
                       {"region_min_hz": _r6(region_min), "region_max_hz": _r6(region_max)})
    if region_max > nyquist + 1e-12:
        raise ApiError(
            400,
            "BAND_OUT_OF_RANGE",
            f"自动选带区域上缘 {region_max:.4g} Hz 越过奈奎斯特频率 {nyquist:.4g} Hz",
            {"region_min_hz": _r6(region_min), "region_max_hz": _r6(region_max),
             "nyquist": _r6(nyquist)},
        )


def build_auto_candidates(
    region_min: float,
    region_max: float,
    band_width: float,
    nyquist: float,
) -> list[tuple[float, float]]:
    """在自动选带区域内按固定带宽、步长=带宽生成互不重叠的候选频带。

    整段完全落在区域内（且不越过奈奎斯特）的候选才保留；与区域上界不对齐的
    零头段不单独成带，避免出现过窄候选。
    """
    if band_width <= 0 or not math.isfinite(band_width):
        raise ApiError(400, "INVALID_AUTO_BAND_CONFIG",
                       "自动选带带宽必须为正的有限数值",
                       {"band_width_hz": band_width})
    if band_width > region_max - region_min + 1e-12:
        raise ApiError(
            400,
            "INVALID_AUTO_BAND_CONFIG",
            f"自动选带带宽 {band_width:.4g} Hz 大于候选区域宽度 "
            f"{region_max - region_min:.4g} Hz，无法形成候选频带",
            {"band_width_hz": _r6(band_width), "region_min_hz": _r6(region_min),
             "region_max_hz": _r6(region_max)},
        )
    candidates: list[tuple[float, float]] = []
    low = region_min
    while low + band_width <= region_max + 1e-12:
        high = low + band_width
        if low >= 0 and high <= nyquist + 1e-12 and high - low > 0:
            candidates.append((low, high))
        low += band_width
        if len(candidates) > MAX_AUTO_CANDIDATES:
            raise ApiError(
                400,
                "INVALID_AUTO_BAND_CONFIG",
                f"候选频带数量超过上限 {MAX_AUTO_CANDIDATES}，请增大带宽或收窄候选区域",
                {"max_candidates": MAX_AUTO_CANDIDATES},
            )
    if not candidates:
        raise ApiError(400, "INVALID_AUTO_BAND_CONFIG",
                       "自动选带区域内没有完整合法的候选频带，请扩大区域或减小带宽",
                       {"region_min_hz": _r6(region_min), "region_max_hz": _r6(region_max),
                        "band_width_hz": _r6(band_width), "nyquist": _r6(nyquist)})
    return candidates


def select_carrier_band(
    values: np.ndarray,
    fs: float,
    auto: dict | None,
) -> dict:
    """按滤波后信号峭度在合法候选频带中自动选带，返回选带依据与全部候选峭度。"""
    nyquist = fs / 2.0
    if auto is not None and auto.get("region_min_hz") is not None:
        region_min, region_max = float(auto["region_min_hz"]), float(auto["region_max_hz"])
        band_width = float(auto["band_width_hz"])
    else:
        region_min = DEFAULT_AUTO_REGION_RATIO[0] * nyquist
        region_max = DEFAULT_AUTO_REGION_RATIO[1] * nyquist
        bw_ratio = DEFAULT_AUTO_BAND_WIDTH_RATIO
        band_width = auto.get("band_width_hz") if auto and auto.get("band_width_hz") is not None \
            else bw_ratio * nyquist
    _check_auto_region(region_min, region_max, nyquist)

    candidates = build_auto_candidates(region_min, region_max, band_width, nyquist)
    evaluated = []
    for lo, hi in candidates:
        filtered = bandpass_filter(values, fs, lo, hi)
        evaluated.append({"band_min_hz": _r6(lo), "band_max_hz": _r6(hi),
                          "kurtosis": _r6(kurtosis(filtered))})

    # 峭度最大者胜出；并列时取中心频率更低（通常对应更早期共振带）的候选
    best_pos = max(
        range(len(evaluated)),
        key=lambda i: (evaluated[i]["kurtosis"], -0.5 * (evaluated[i]["band_min_hz"]
                                                         + evaluated[i]["band_max_hz"])),
    )
    best = evaluated[best_pos]
    return {
        "mode": "auto",
        "region_min_hz": _r6(region_min),
        "region_max_hz": _r6(region_max),
        "band_width_hz": _r6(band_width),
        "candidate_count": len(evaluated),
        "selected_index": best_pos,
        "selected_band_min_hz": best["band_min_hz"],
        "selected_band_max_hz": best["band_max_hz"],
        "selected_kurtosis": best["kurtosis"],
        "candidates": evaluated,
        "reason": (
            f"在 {region_min:.1f}~{region_max:.1f} Hz 候选区域内按固定带宽 "
            f"{band_width:.1f} Hz 生成 {len(evaluated)} 个互不重叠候选带，"
            f"对每个候选带做带通并计算滤波信号峭度；第 {best_pos + 1} 个候选带 "
            f"{best['band_min_hz']:.1f}~{best['band_max_hz']:.1f} Hz 峭度最大"
            f"（{best['kurtosis']:.3f}，正态信号约为 3），峭度越大说明冲击成分越突出，"
            "故选为载波解调频带"
        ),
    }


# ---------- 包络谱故障族匹配 ----------

def _targets_for_family(
    fault: str,
    fc: float,
    fr: float,
    max_harmonics: int,
    sideband_orders: list[int],
    nyquist: float,
) -> list[dict]:
    """生成一个故障族的全部匹配目标：h·fc（基频/谐波）及 h·fc±k·fr（1X 边带）。"""
    targets: list[dict] = []
    for harmonic in range(1, max_harmonics + 1):
        center = harmonic * fc
        if center - fr > nyquist + 1e-9:
            break
        kind = "fundamental" if harmonic == 1 else "harmonic"
        targets.append({"fault": fault, "kind": kind, "harmonic": harmonic,
                        "sideband_order": 0, "target_frequency_hz": center})
        for order in sideband_orders:
            for sign, label in ((-1, "lower"), (1, "upper")):
                f_side = center + sign * order * fr
                if f_side > 0 and f_side <= nyquist + 1e-9:
                    targets.append({"fault": fault, "kind": f"{label}_sideband",
                                    "harmonic": harmonic, "sideband_order": order,
                                    "target_frequency_hz": f_side})
    return targets


def match_all_targets(
    spec: dict,
    targets_by_fault: dict[str, list[dict]],
    tolerance_hz: float,
) -> tuple[dict[str, list[dict]], dict[str, np.ndarray]]:
    """在包络谱上联合匹配全部故障族目标。

    每个谱线若落在若干目标的 ±tolerance 窗内，只归属于频率距离最近的目标，
    避免 BPFO/BPFI/BSF 目标频率相近时同一条谱线被多个故障族重复计数
    （否则真实外圈冲击会把无关的 BSF 能量占比抬高）。返回各故障族的命中明细
    与其专属谱线布尔掩码。
    """
    freqs = spec["freqs"]
    amp = spec["amplitude"]
    noise_floor = float(np.median(amp[1:])) if amp.size > 1 else 0.0
    global_peak = float(amp[1:].max()) if amp.size > 1 else 0.0
    # 命中线必须显著高出噪声地板（≥6 倍中位数）且达到全局主峰的 5%，
    # 抑制纯噪声信号在大量匹配窗内的偶发高点
    prominence = max(6.0 * noise_floor, 0.05 * global_peak)

    n_bins = freqs.size
    owner_target = np.full(n_bins, -1, dtype=int)
    owner_distance = np.full(n_bins, np.inf)
    flat_targets: list[tuple[str, dict]] = []
    for fault, targets in targets_by_fault.items():
        for target in targets:
            flat_targets.append((fault, target))

    for ti, (_, target) in enumerate(flat_targets):
        f_target = target["target_frequency_hz"]
        idx_lo = max(int(np.searchsorted(freqs, f_target - tolerance_hz, side="left")), 1)
        idx_hi = int(np.searchsorted(freqs, f_target + tolerance_hz, side="right"))
        if idx_lo >= idx_hi:
            continue
        distance = np.abs(freqs[idx_lo:idx_hi] - f_target)
        closer = distance < owner_distance[idx_lo:idx_hi]
        owner_target[idx_lo:idx_hi] = np.where(closer, ti, owner_target[idx_lo:idx_hi])
        owner_distance[idx_lo:idx_hi] = np.minimum(owner_distance[idx_lo:idx_hi], distance)

    matches_by_fault: dict[str, list[dict]] = {fault: [] for fault in targets_by_fault}
    masks_by_fault: dict[str, np.ndarray] = {
        fault: np.zeros(n_bins, dtype=bool) for fault in targets_by_fault
    }
    owned_bins = {ti: np.where(owner_target == ti)[0] for ti in range(len(flat_targets))}

    for ti, (fault, target) in enumerate(flat_targets):
        bins = owned_bins[ti]
        if bins.size > 0:
            local_idx = int(bins[np.argmax(amp[bins])])
        else:
            # 窗内没有任何谱线（记录过短），退化为窗内最近谱线但不计能量归属
            f_target = target["target_frequency_hz"]
            idx_lo = max(int(np.searchsorted(
                freqs, f_target - tolerance_hz, side="left")), 1)
            idx_hi = int(np.searchsorted(freqs, f_target + tolerance_hz, side="right"))
            local_idx = idx_lo if idx_lo < idx_hi else 1
        peak_amp = float(amp[local_idx])
        deviation = float(freqs[local_idx] - target["target_frequency_hz"])
        hit = bins.size > 0 and peak_amp >= prominence and abs(deviation) <= tolerance_hz + 1e-12
        if hit:
            # 能量占比只统计显著命中的目标窗，避免大量噪声窗累积出虚假能量比例
            masks_by_fault[fault][bins] = True
        matches_by_fault[fault].append({
            "fault": fault,
            "kind": target["kind"],
            "harmonic": target["harmonic"],
            "sideband_order": target["sideband_order"],
            "target_frequency_hz": _r6(target["target_frequency_hz"]),
            "peak_frequency_hz": _r6(freqs[local_idx]),
            "peak_amplitude": _r6(peak_amp),
            "deviation_hz": _r6(deviation),
            "matched": bool(hit),
        })

    return matches_by_fault, masks_by_fault


def _mask_energy_ratio(spec: dict, mask: np.ndarray) -> float:
    """指定谱线集合功率占包络谱交流功率（直流除外）的比例。"""
    total = float(spec["power"][1:].sum())
    if total <= 0.0:
        return 0.0
    return float(spec["power"][mask].sum() / total)


def _classify_family(
    key: str,
    cn_name: str,
    fault_name: str,
    fc: float,
    matches: list[dict],
    energy_ratio: float,
    attention: float,
    critical: float,
    tolerance_hz: float,
    fr: float,
) -> dict:
    """汇总单个故障族的命中证据，给出能量占比等级、置信等级与中文判据。"""
    hit_matches = [m for m in matches if m["matched"]]
    fundamental = next((m for m in hit_matches if m["kind"] == "fundamental"), None)
    harmonics = [m for m in hit_matches if m["kind"] == "harmonic"]
    lower = [m for m in hit_matches if m["kind"] == "lower_sideband"]
    upper = [m for m in hit_matches if m["kind"] == "upper_sideband"]
    harmonic_orders = sorted({m["harmonic"] for m in harmonics})
    sideband_harmonics = sorted({m["harmonic"] for m in lower + upper})

    # 置信等级：高=基频 +（谐波或 1X 边带）形成证据链，且能量达标；
    # 中=基频命中且能量达标，或多谐波/边带但能量偏弱；低=仅有弱证据；无=无显著命中
    strong_chain = fundamental is not None and (harmonics or lower or upper)
    if strong_chain and energy_ratio >= attention:
        confidence = "high"
    elif (fundamental is not None and energy_ratio >= attention) or (
            len(harmonic_orders) >= 2 and energy_ratio >= attention) or (
            fundamental is not None and (lower or upper)):
        confidence = "medium"
    elif hit_matches:
        confidence = "low"
    else:
        confidence = "none"

    if energy_ratio >= critical:
        level = "critical"
    elif energy_ratio >= attention:
        level = "attention"
    else:
        level = "normal"

    parts = [f"{cn_name} 基频 {fc:.2f} Hz"]
    if fundamental is not None:
        parts.append(f"基频谱峰 {fundamental['peak_frequency_hz']:.2f} Hz"
                     f"（偏差 {fundamental['deviation_hz']:+.2f} Hz）命中")
    else:
        parts.append(f"基频 ±{tolerance_hz:g} Hz 窗内无显著谱峰")
    if harmonics:
        parts.append("存在第 " + "、".join(str(h) for h in harmonic_orders) + " 次谐波")
    if lower or upper:
        side_desc = []
        if lower:
            side_desc.append("下边带 h·f−" + f"{fr:.2f}")
        if upper:
            side_desc.append("上边带 h·f+" + f"{fr:.2f}")
        parts.append("命中 1X 边带（" + "、".join(side_desc) + "）")
    parts.append(f"故障族包络能量占比 {energy_ratio:.2%}")
    if level == "critical":
        parts.append(f"达到严重阈值 {critical:.0%}")
        if confidence == "high":
            parts.append(f"高度怀疑{fault_name}，建议停机检查")
        else:
            parts.append(f"疑似{fault_name}，建议尽快复检确认")
    elif level == "attention":
        parts.append(f"超过关注阈值 {attention:.0%}，建议安排复检")
    else:
        parts.append("低于关注阈值，未见明显异常")
    message = "；".join(parts) + "。"

    return {
        "fault": key,
        "name": cn_name,
        "characteristic_frequency_hz": _r6(fc),
        "energy_ratio": _r6(energy_ratio),
        "level": level,
        "confidence": confidence,
        "fundamental_matched": fundamental is not None,
        "matched_harmonic_orders": harmonic_orders,
        "matched_sideband_harmonics": sideband_harmonics,
        "lower_sideband_hits": lower,
        "upper_sideband_hits": upper,
        "peak_matches": matches,
        "message": message,
    }


def diagnose_envelope(
    values: np.ndarray,
    fs: float,
    geom: dict | None,
    rpm: float,
    carrier_band: dict | None,
    auto_band: dict | None,
    max_harmonics: int,
    sideband_orders: list[int],
    match_tolerance_hz: float,
    thresholds: dict,
) -> dict:
    """执行完整包络解调诊断，返回可直接持久化 / 序列化的结果字典。

    载波频带越界、采样不均匀等输入问题由调用方在落库前校验并抛出 ApiError。
    """
    nyquist = fs / 2.0
    fr = rpm / 60.0

    # ---------- 载波频带 ----------
    if carrier_band is not None:
        lo, hi = float(carrier_band["low_hz"]), float(carrier_band["high_hz"])
        check_frequency_band(lo, hi, nyquist, "载波频带")
        band_selection = {
            "mode": "manual",
            "region_min_hz": None,
            "region_max_hz": None,
            "band_width_hz": _r6(hi - lo),
            "candidate_count": 1,
            "selected_index": 0,
            "selected_band_min_hz": _r6(lo),
            "selected_band_max_hz": _r6(hi),
            "selected_kurtosis": None,
            "candidates": [],
            "reason": (f"按请求显式指定载波频带 {lo:.1f}~{hi:.1f} Hz 解调，"
                       "未执行峭度自动选带"),
        }
    else:
        band_selection = select_carrier_band(values, fs, auto_band)
        lo = band_selection["selected_band_min_hz"]
        hi = band_selection["selected_band_max_hz"]

    # ---------- 带通 → Hilbert 包络 → 去直流包络谱 ----------
    filtered = bandpass_filter(values, fs, lo, hi)
    band_kurtosis = kurtosis(filtered)
    envelope = hilbert_envelope(filtered)
    spec = envelope_spectrum(envelope, fs)
    # 匹配窗至少覆盖一个频率分辨率，避免短记录窗内无谱线
    effective_tolerance = max(float(match_tolerance_hz), float(spec["resolution"]))

    result = {
        "rpm": rpm,
        "shaft_frequency_hz": _r6(fr),
        "nyquist_frequency_hz": _r6(nyquist),
        "frequency_resolution_hz": _r6(spec["resolution"]),
        "sample_count": int(values.size),
        "carrier_band": {
            "low_hz": _r6(lo),
            "high_hz": _r6(hi),
            "band_kurtosis": _r6(band_kurtosis),
        },
        "band_selection": band_selection,
        "max_harmonics": int(max_harmonics),
        "sideband_orders": sorted(set(sideband_orders)),
        "match_tolerance_hz": _r6(effective_tolerance),
        "attention_ratio": _r6(thresholds["envelope_attention_ratio"]),
        "critical_ratio": _r6(thresholds["envelope_critical_ratio"]),
    }

    # ---------- 轴承几何缺失：解调照常给出，但无法匹配故障族 ----------
    missing = missing_geometry_fields(geom)
    if missing:
        result.update({
            "status": "unavailable",
            "confidence": "none",
            "dominant_fault": None,
            "missing_fields": missing,
            "reason": "缺少轴承几何参数，无法推导 BPFO/BPFI/BSF/FTF：" + "、".join(missing),
            "characteristic_frequencies_hz": {},
            "fault_families": [],
            "peak_matches": [],
            "conclusion": (
                f"已在 {lo:.1f}~{hi:.1f} Hz 载波频带上完成包络解调，但因缺少轴承几何参数"
                f"（{'、'.join(missing)}）无法匹配故障特征频率，包络谱仅供人工判读。"
            ),
        })
        return result

    char_freqs = bearing_characteristic_frequencies(geom, rpm)
    attention = thresholds["envelope_attention_ratio"]
    critical = thresholds["envelope_critical_ratio"]

    targets_by_fault = {
        key: _targets_for_family(
            key, char_freqs[key], fr, max_harmonics, sideband_orders, spec["nyquist"])
        for key, _, _ in BEARING_FAULTS
    }
    matches_by_fault, masks_by_fault = match_all_targets(spec, targets_by_fault, effective_tolerance)

    families: list[dict] = []
    all_matches: list[dict] = []
    for key, cn_name, fault_name in BEARING_FAULTS:
        matches = matches_by_fault[key]
        energy_ratio = _mask_energy_ratio(spec, masks_by_fault[key])
        family = _classify_family(
            key, cn_name, fault_name, char_freqs[key], matches, energy_ratio,
            attention, critical, effective_tolerance, fr)
        families.append(family)
        all_matches.extend(matches)

    # 主导故障：置信等级高者优先，同级按能量占比排序
    confidence_rank = {"high": 3, "medium": 2, "low": 1, "none": 0}
    ranked = sorted(
        families,
        key=lambda f: (confidence_rank[f["confidence"]], f["energy_ratio"]),
        reverse=True,
    )
    dominant = ranked[0]
    overall_confidence = dominant["confidence"] if dominant["energy_ratio"] > 0 else "none"
    if any(f["level"] == "critical" for f in families):
        level = "critical"
    elif any(f["level"] == "attention" for f in families):
        level = "attention"
    else:
        level = "normal"

    if level == "normal" or confidence_rank[overall_confidence] == 0:
        conclusion = (
            f"包络解调（载波 {lo:.0f}~{hi:.0f} Hz，带通峭度 {band_kurtosis:.2f}）在 "
            "BPFO/BPFI/BSF/FTF 的基频、谐波及 1X 边带处均未发现达到判据的谱峰证据，"
            "各故障族能量占比低于关注阈值，未见轴承早期冲击特征，判为正常。"
        )
    else:
        cn = next(name for k, name, _ in BEARING_FAULTS if k == dominant["fault"])
        fault_cn = next(fl for k, _, fl in BEARING_FAULTS if k == dominant["fault"])
        action = "建议停机检查" if level == "critical" else "建议安排复检并跟踪趋势"
        conclusion = (
            f"包络解调（载波 {lo:.0f}~{hi:.0f} Hz，带通峭度 {band_kurtosis:.2f}）显示 "
            f"{cn} 证据最突出：基频 {dominant['characteristic_frequency_hz']:.2f} Hz"
            + ("命中" if dominant["fundamental_matched"] else "未直接命中")
            + (f"，检出第 {'、'.join(str(h) for h in dominant['matched_harmonic_orders'])} 次谐波"
               if dominant["matched_harmonic_orders"] else "")
            + (f"，并伴随 1X 转频边带（{SIDEBAND_HINT.get(dominant['fault'], '周期性调制')}）"
               if dominant["matched_sideband_harmonics"] else "")
            + f"，故障族能量占比 {dominant['energy_ratio']:.2%}，"
            f"置信等级{CONFIDENCE_LABELS[overall_confidence]}，"
            f"综合判为{fault_cn}（{level}），{action}。"
        )

    result.update({
        "status": level,
        "confidence": overall_confidence,
        "dominant_fault": dominant["fault"],
        "missing_fields": [],
        "reason": None,
        "characteristic_frequencies_hz": {k: _r6(v) for k, v in char_freqs.items()},
        "fault_families": families,
        "peak_matches": all_matches,
        "conclusion": conclusion,
    })
    return result
