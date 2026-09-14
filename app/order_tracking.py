"""变速阶次跟踪核心：转速脉冲插值转角、等角度重采样、按转数滑窗的阶次谱与共振转速区间合并。

恒转速 FFT 在启停机 / 升降速过程中会把 1X、2X 等阶次成分拉宽（频谱涂抹），
无法定位共振转速。这里改用键相 / 转速脉冲把时域信号重采样到等角度网格，
使各阶次成分在角域重新变为离散谱线，再按固定转数滑窗计算阶次谱。
"""
from __future__ import annotations

import math

import numpy as np

from .errors import ApiError
from .spectrum import label_for_band


def _r6(v: float) -> float:
    """统一保留 6 位小数，便于结果稳定展示。"""
    return round(float(v), 6)


def validate_pulse_train(pulse_times: list[float], pulses_per_revolution: int) -> None:
    """脉冲时间业务校验：必须严格递增（有限性由 Pydantic 拦截为 422）。"""
    for i in range(1, len(pulse_times)):
        if pulse_times[i] <= pulse_times[i - 1]:
            raise ApiError(
                400,
                "PULSE_TIME_OUT_OF_ORDER",
                f"转速脉冲时间未严格递增：第 {i} 个脉冲 t={pulse_times[i]} 不大于前一个 "
                f"t={pulse_times[i - 1]}",
                {"index": i, "t": pulse_times[i], "previous_t": pulse_times[i - 1]},
            )
    if pulses_per_revolution < 1:
        raise ApiError(
            400,
            "INVALID_PULSE_CONFIG",
            f"每转脉冲数必须为正整数，收到 {pulses_per_revolution}",
            {"pulses_per_revolution": pulses_per_revolution},
        )


def _check_pulse_coverage(
    pulse_times: np.ndarray,
    sample_times: np.ndarray,
    pulse_angles: np.ndarray,
    window_revolutions: float,
) -> tuple[float, float]:
    """脉冲必须包住全部样本时间，且转角覆盖至少一个完整分析窗，返回样本起止转角。"""
    t0, t1 = float(sample_times[0]), float(sample_times[-1])
    p0, p1 = float(pulse_times[0]), float(pulse_times[-1])
    tol = 1e-9 * max(1.0, abs(t0), abs(t1))
    if p0 > t0 + tol or p1 < t1 - tol:
        raise ApiError(
            400,
            "INSUFFICIENT_PULSE_COVERAGE",
            f"转速脉冲覆盖不足：脉冲区间 [{p0:.6g}, {p1:.6g}]s 未包住样本区间 "
            f"[{t0:.6g}, {t1:.6g}]s，无法对整段记录做等角度重采样",
            {
                "first_pulse_time": _r6(p0),
                "last_pulse_time": _r6(p1),
                "first_sample_time": _r6(t0),
                "last_sample_time": _r6(t1),
                "missing_lead_seconds": _r6(max(0.0, p0 - t0)),
                "missing_trail_seconds": _r6(max(0.0, t1 - p1)),
            },
        )
    theta0 = float(np.interp(t0, pulse_times, pulse_angles))
    theta1 = float(np.interp(t1, pulse_times, pulse_angles))
    if theta1 - theta0 < window_revolutions - 1e-12:
        raise ApiError(
            400,
            "INSUFFICIENT_PULSE_COVERAGE",
            f"转速脉冲覆盖的转角不足一个分析窗：样本区间内仅 {theta1 - theta0:.3f} 转，"
            f"窗长需要 {window_revolutions:g} 转",
            {
                "covered_revolutions": _r6(theta1 - theta0),
                "required_revolutions": _r6(window_revolutions),
            },
        )
    return theta0, theta1


def _check_order_bands(order_bands: list[dict], order_nyquist: float) -> None:
    """所有请求阶次带上缘不得越过角域奈奎斯特上限（每转采样数 / 2，单位：阶）。"""
    out_of_range = []
    for ob in order_bands:
        if ob["order_max"] > order_nyquist + 1e-12 or ob["order_min"] > order_nyquist + 1e-12:
            out_of_range.append({
                "name": ob.get("name") or label_for_band(ob),
                "order_min": _r6(ob["order_min"]),
                "order_max": _r6(ob["order_max"]),
            })
    if out_of_range:
        names = "、".join(
            f"{b['name']}({b['order_min']:g}~{b['order_max']:g} 阶)" for b in out_of_range
        )
        raise ApiError(
            400,
            "ORDER_OUT_OF_RANGE",
            f"请求阶次带 {names} 越过角域奈奎斯特上限 {order_nyquist:g} 阶"
            "（= 每转重采样样本数 / 2），请提高 samples_per_revolution 或收窄阶次带",
            {"order_nyquist": _r6(order_nyquist), "bands": out_of_range},
        )


def equal_angle_resample(
    sample_times: np.ndarray,
    sample_values: np.ndarray,
    pulse_times: np.ndarray,
    pulse_angles: np.ndarray,
    samples_per_revolution: int,
    theta0: float,
    theta1: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """线性插值转角 → 等角度重采样。

    脉冲序列给出分段线性的 t→θ 映射；先在等角度网格上反插得到各角点时间，
    再在原始均匀采样上线性插值取幅值。返回 (转角网格, 角点时间, 角域幅值)。
    """
    n_grid = int(math.floor((theta1 - theta0) * samples_per_revolution)) + 1
    theta_grid = theta0 + np.arange(n_grid, dtype=float) / samples_per_revolution
    # 覆盖校验保证 theta_grid 落在脉冲转角范围内，np.interp 不做外推
    grid_times = np.interp(theta_grid, pulse_angles, pulse_times)
    grid_values = np.interp(grid_times, sample_times, sample_values)
    return theta_grid, grid_times, grid_values


def _order_spectrum(values: np.ndarray, samples_per_revolution: int) -> dict:
    """单窗角域信号：去直流 + Hann 窗的单边阶次谱（rFFT，频率轴单位为“阶”）。"""
    n = values.size
    window = np.hanning(n)
    windowed = (values - values.mean()) * window

    spectrum = np.fft.rfft(windowed)
    orders = np.fft.rfftfreq(n, d=1.0 / samples_per_revolution)

    amplitude = 2.0 * np.abs(spectrum) / window.sum()
    amplitude[0] = np.abs(spectrum[0]) / window.sum()
    if n % 2 == 0:
        amplitude[-1] = np.abs(spectrum[-1]) / window.sum()

    power = (np.abs(spectrum) ** 2) / (window ** 2).sum()
    return {
        "n": n,
        "orders": orders,
        "amplitude": amplitude,
        "power": power,
        "resolution": samples_per_revolution / n,
        "nyquist": samples_per_revolution / 2.0,
    }


def _order_band_ratio(spec: dict, order_min: float, order_max: float) -> float:
    """阶次带 [order_min, order_max] 内功率占全谱（0 阶直流线除外）功率的比例。"""
    mask = (spec["orders"] >= order_min) & (spec["orders"] <= order_max)
    total = spec["power"][1:].sum()
    if total <= 0:
        return 0.0
    return float(spec["power"][mask].sum() / total)


def build_pulse_summary(
    pulse_times: np.ndarray,
    pulses_per_revolution: int,
    pulse_angles: np.ndarray,
) -> dict:
    """脉冲摘要：数量、时间范围、间隔统计与由相邻脉冲推算的瞬时转速。"""
    intervals = np.diff(pulse_times)
    # 每个脉冲间隔对应 1/PPR 转 → rpm = 60 / (PPR·dt)
    inst_rpm = 60.0 / (pulses_per_revolution * intervals)
    return {
        "pulse_count": int(pulse_times.size),
        "first_pulse_time": _r6(pulse_times[0]),
        "last_pulse_time": _r6(pulse_times[-1]),
        "covered_revolutions": _r6(pulse_angles[-1] - pulse_angles[0]),
        "pulse_interval_min_s": _r6(intervals.min()),
        "pulse_interval_max_s": _r6(intervals.max()),
        "pulse_interval_mean_s": _r6(intervals.mean()),
        "instantaneous_rpm_min": _r6(inst_rpm.min()),
        "instantaneous_rpm_max": _r6(inst_rpm.max()),
        "instantaneous_rpm_mean": _r6(inst_rpm.mean()),
    }


def analyze_order_tracking(
    samples: list[dict],
    sampling_frequency: float,
    pulse_times: list[float],
    pulses_per_revolution: int,
    samples_per_revolution: int,
    window_revolutions: float,
    overlap_revolutions: float,
    order_bands: list[dict],
    resonance_ratio_threshold: float,
    min_consecutive_windows: int,
) -> dict:
    """执行完整的变速阶次跟踪分析，返回可直接持久化 / 序列化的结果字典。

    任何输入校验失败都抛出 ApiError（400），由路由层保证不写入结果。
    """
    validate_pulse_train(pulse_times, pulses_per_revolution)
    order_nyquist = samples_per_revolution / 2.0
    _check_order_bands(order_bands, order_nyquist)

    sample_times = np.asarray([s["t"] for s in samples], dtype=float)
    sample_values = np.asarray([s["a"] for s in samples], dtype=float)
    pt = np.asarray(pulse_times, dtype=float)
    # 第 j 个脉冲对应转角 j/PPR（转，起点取 0）
    pa = np.arange(pt.size, dtype=float) / pulses_per_revolution

    theta0, theta1 = _check_pulse_coverage(pt, sample_times, pa, window_revolutions)
    theta_grid, grid_times, grid_values = equal_angle_resample(
        sample_times, sample_values, pt, pa, samples_per_revolution, theta0, theta1
    )

    window_len = int(round(window_revolutions * samples_per_revolution))
    stride = int(round((window_revolutions - overlap_revolutions) * samples_per_revolution))
    stride = max(stride, 1)

    windows: list[dict] = []
    start = 0
    while start + window_len <= grid_values.size:
        index = len(windows)
        seg_theta = theta_grid[start:start + window_len]
        seg_t = grid_times[start:start + window_len]
        seg_a = grid_values[start:start + window_len]

        spec = _order_spectrum(seg_a, samples_per_revolution)
        peak_idx = int(np.argmax(spec["amplitude"][1:])) + 1
        angle_span = (window_len - 1) / samples_per_revolution
        duration = float(seg_t[-1] - seg_t[0])
        average_rpm = angle_span * 60.0 / duration if duration > 0 else 0.0

        bands = []
        for ob in order_bands:
            lo, hi = ob["order_min"], ob["order_max"]
            bands.append({
                "name": ob.get("name") or label_for_band(ob),
                "order_min": _r6(lo),
                "order_max": _r6(hi),
                "energy_ratio": _r6(_order_band_ratio(spec, lo, hi)),
            })

        windows.append({
            "window_index": index,
            "t_start": _r6(seg_t[0]),
            "t_end": _r6(seg_t[-1]),
            "duration_seconds": _r6(duration),
            "revolution_start": _r6(seg_theta[0]),
            "revolution_end": _r6(seg_theta[-1]),
            "average_rpm": _r6(average_rpm),
            "order_resolution": _r6(spec["resolution"]),
            "sample_count": int(window_len),
            "main_peak_order": _r6(spec["orders"][peak_idx]),
            "main_peak_amplitude": _r6(spec["amplitude"][peak_idx]),
            "bands": bands,
        })
        start += stride

    if not windows:
        # 理论上被转角覆盖校验拦截，保留防御性兜底
        raise ApiError(
            400,
            "INSUFFICIENT_PULSE_COVERAGE",
            "脉冲与样本覆盖范围内无法切出完整的阶次分析窗",
            {"window_revolutions": window_revolutions},
        )

    zones = merge_resonance_zones(
        windows, resonance_ratio_threshold, min_consecutive_windows
    )
    summary = build_pulse_summary(pt, pulses_per_revolution, pa)

    return {
        "sampling_frequency": _r6(sampling_frequency),
        "sample_count": int(sample_times.size),
        "pulses_per_revolution": int(pulses_per_revolution),
        "pulse_count": int(pt.size),
        "samples_per_revolution": int(samples_per_revolution),
        "window_revolutions": _r6(window_revolutions),
        "overlap_revolutions": _r6(overlap_revolutions),
        "order_resolution": windows[0]["order_resolution"],
        "order_nyquist": _r6(order_nyquist),
        "resampled_sample_count": int(grid_values.size),
        "resonance_ratio_threshold": _r6(resonance_ratio_threshold),
        "min_consecutive_windows": int(min_consecutive_windows),
        "order_bands": [
            {
                "name": ob.get("name") or label_for_band(ob),
                "order_min": _r6(ob["order_min"]),
                "order_max": _r6(ob["order_max"]),
            }
            for ob in order_bands
        ],
        "pulse_summary": summary,
        "window_count": len(windows),
        "windows": windows,
        "resonance_zones": zones,
    }


def merge_resonance_zones(
    windows: list[dict],
    threshold: float,
    min_consecutive_windows: int,
) -> list[dict]:
    """按“任一指定阶次带能量占比 ≥ 阈值”标记窗口，连续命中不少于最少窗数则合并为共振转速区间。"""
    def hit_band(w: dict) -> dict | None:
        best = None
        for b in w["bands"]:
            if b["energy_ratio"] >= threshold and (best is None or b["energy_ratio"] > best["energy_ratio"]):
                best = b
        return best

    zones: list[dict] = []
    run_start: int | None = None
    for i, w in enumerate(windows):
        if hit_band(w) is not None:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                _maybe_append_zone(zones, windows, run_start, i - 1, min_consecutive_windows)
                run_start = None
    if run_start is not None:
        _maybe_append_zone(zones, windows, run_start, len(windows) - 1, min_consecutive_windows)
    return zones


def _maybe_append_zone(
    zones: list[dict],
    windows: list[dict],
    start: int,
    end: int,
    min_consecutive_windows: int,
) -> None:
    """把一段连续命中窗收成区间；短于最少连续窗数的游程忽略。"""
    run = windows[start:end + 1]
    if len(run) < min_consecutive_windows:
        return
    duration_total = sum(w["duration_seconds"] for w in run)
    weighted_rpm = (
        sum(w["average_rpm"] * w["duration_seconds"] for w in run) / duration_total
        if duration_total > 0
        else sum(w["average_rpm"] for w in run) / len(run)
    )
    dominant = max(
        (b for w in run for b in w["bands"]),
        key=lambda b: b["energy_ratio"],
        default=None,
    )
    zones.append({
        "zone_index": len(zones),
        "window_start_index": start,
        "window_end_index": end,
        "window_count": len(run),
        "t_start": _r6(run[0]["t_start"]),
        "t_end": _r6(run[-1]["t_end"]),
        "rpm_min": _r6(min(w["average_rpm"] for w in run)),
        "rpm_max": _r6(max(w["average_rpm"] for w in run)),
        "average_rpm": _r6(weighted_rpm),
        "main_peak_order_min": _r6(min(w["main_peak_order"] for w in run)),
        "main_peak_order_max": _r6(max(w["main_peak_order"] for w in run)),
        "dominant_band": dominant["name"] if dominant else None,
        "max_energy_ratio": _r6(max(b["energy_ratio"] for w in run for b in w["bands"])),
    })
