"""振动基线对比核心：按转速分组聚合历史频谱诊断指标，给出中位数 / 离散范围与偏差分级。

基线由同一设备多条已保存的频谱诊断（及其源分析记录）构建，按转速分组统计
RMS、峰值因子、主峰频率、峰值阶次四项指标；新诊断与基线对比时输出绝对偏差、
变化率与 normal / attention / critical 等级。
"""
from __future__ import annotations

import numpy as np

from .errors import ApiError

# 参与基线统计的四项指标
BASELINE_METRICS = ("rms", "crest_factor", "main_peak_frequency_hz", "peak_order")

METRIC_LABELS: dict[str, str] = {
    "rms": "RMS",
    "crest_factor": "峰值因子",
    "main_peak_frequency_hz": "主峰频率",
    "peak_order": "峰值阶次",
}
METRIC_UNITS: dict[str, str] = {
    "rms": "m/s²",
    "crest_factor": "",
    "main_peak_frequency_hz": "Hz",
    "peak_order": "阶",
}

# 各指标默认的相对偏差阈值（|新值-中位数| / |中位数|）
DEFAULT_DEVIATION_THRESHOLDS: dict[str, dict[str, float]] = {
    "rms": {"attention": 0.15, "critical": 0.30},
    "crest_factor": {"attention": 0.20, "critical": 0.40},
    "main_peak_frequency_hz": {"attention": 0.05, "critical": 0.10},
    "peak_order": {"attention": 0.05, "critical": 0.10},
}

LEVELS = ("normal", "attention", "critical")


def _r6(v: float) -> float:
    return round(float(v), 6)


def diagnosis_feature_values(diag: dict, record: dict) -> dict[str, float]:
    """从一条频谱诊断与其源分析记录中提取基线统计所需的四项指标。"""
    return {
        "rms": float(record["rms"]),
        "crest_factor": float(record["crest_factor"]),
        "main_peak_frequency_hz": float(diag["main_peak"]["frequency_hz"]),
        "peak_order": float(diag["order"]["peak_order"]),
    }


def assign_rpm_group(rpm: float, rpm_bins: list[float]) -> int:
    """按左闭右开区间把转速归入 rpm_bins（长度 N → 0..N-2 组）。"""
    lo, hi = rpm_bins[0], rpm_bins[-1]
    if rpm < lo or rpm >= hi:
        raise ApiError(
            400,
            "RPM_OUT_OF_BINS",
            f"诊断转速 {rpm:g} 不在转速分组区间 [{lo:g}, {hi:g}) 内",
            {"rpm": rpm, "min": lo, "max_exclusive": hi},
        )
    idx = int(np.searchsorted(rpm_bins, rpm, side="right")) - 1
    return max(0, idx)


def _same_rpm(a: float, b: float) -> bool:
    return a == b or abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def build_rpm_groups(
    entries: list[dict],
    rpm_bins: list[float] | None,
    min_samples: int,
) -> tuple[list[dict], list[dict]]:
    """把带 feature 指标的诊断条目按转速分组并计算中位数与离散范围。

    ``rpm_bins`` 给出时按左闭右开区间分组（区间边界在构建请求阶段已校验、
    转速越界由 :func:`assign_rpm_group` 抛错）；为 None 时按完全相同的 rpm 归组。
    返回 (groups, skipped)：样本数少于 ``min_samples`` 的组不进入基线、放入 skipped。
    """
    buckets: dict[float, list[dict]] = {}
    order: list[float] = []
    for e in entries:
        rpm = float(e["rpm"])
        if rpm_bins is not None:
            b = assign_rpm_group(rpm, rpm_bins)
            key = float(b)
            entry_group = b
        else:
            key = next((k for k in buckets if _same_rpm(k, rpm)), rpm)
            entry_group = None
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        e2 = dict(e)
        e2["group_index"] = entry_group
        buckets[key].append(e2)

    groups: list[dict] = []
    skipped: list[dict] = []
    for seq, key in enumerate(sorted(order, key=lambda k: (rpm_bins is None, k))):
        members = buckets[key]
        if rpm_bins is not None:
            b = int(key)
            rpm_min, rpm_max = float(rpm_bins[b]), float(rpm_bins[b + 1])
            label = f"{rpm_min:g}~{rpm_max:g} rpm"
        else:
            rpm_min = rpm_max = float(members[0]["rpm"])
            b = seq
            label = f"{rpm_min:g} rpm"

        if len(members) < min_samples:
            skipped.append({
                "group_index": b,
                "rpm_min": rpm_min,
                "rpm_max": rpm_max,
                "label": label,
                "sample_count": len(members),
                "required": min_samples,
            })
            continue

        metrics: dict[str, dict] = {}
        for name in BASELINE_METRICS:
            values = np.asarray([m["features"][name] for m in members], dtype=float)
            metrics[name] = {
                "median": _r6(np.median(values)),
                "min": _r6(np.min(values)),
                "max": _r6(np.max(values)),
            }
        groups.append({
            "group_index": b,
            "rpm_min": rpm_min,
            "rpm_max": rpm_max,
            "label": label,
            "sample_count": len(members),
            "metrics": metrics,
            "source_diagnosis_ids": sorted(int(m["diagnosis_id"]) for m in members),
        })

    if not groups:
        need = "每个转速分组" if rpm_bins is not None else "每个相同转速"
        raise ApiError(
            400,
            "INSUFFICIENT_BASELINE_SAMPLES",
            f"没有任何转速分组达到最少样本数 {min_samples}：{need} 至少需要 {min_samples} 条诊断",
            {"min_samples_per_group": min_samples, "skipped_groups": skipped},
        )
    return groups, skipped


def find_matching_group(rpm: float, groups: list[dict]) -> dict:
    """为待对比诊断的转速找基线分组：优先左闭右开区间，精确组按相同转速匹配。"""
    for g in groups:
        if g["rpm_min"] != g["rpm_max"]:
            if g["rpm_min"] <= rpm < g["rpm_max"]:
                return g
        elif _same_rpm(g["rpm_min"], rpm):
            return g
    raise ApiError(
        400,
        "BASELINE_RPM_NOT_COVERED",
        f"基线没有覆盖转速 {rpm:g} 的分组，无法进行同工况对比",
        {"rpm": rpm,
         "groups": [{"group_index": g["group_index"], "rpm_min": g["rpm_min"],
                     "rpm_max": g["rpm_max"]} for g in groups]},
    )


def _classify(value: float, median: float, lo: float, hi: float) -> tuple[str, float | None, float]:
    """返回 (level, change_rate, deviation)。中位数为 0（量纲指标不可除）时无法按变化率分级。"""
    deviation = value - median
    if median == 0.0:
        return "normal", None, deviation
    rate = deviation / abs(median)
    if abs(rate) >= hi:
        level = "critical"
    elif abs(rate) >= lo:
        level = "attention"
    else:
        level = "normal"
    return level, rate, deviation


def compare_diagnosis(
    diag: dict,
    record: dict,
    group: dict,
    thresholds: dict[str, dict[str, float]],
) -> dict:
    """将一条新诊断与基线的对应转速分组逐项对比，生成偏差、变化率与等级。"""
    features = diagnosis_feature_values(diag, record)
    metric_results: list[dict] = []
    overall = "normal"
    for name in BASELINE_METRICS:
        value = features[name]
        stat = group["metrics"][name]
        median = float(stat["median"])
        lo = float(thresholds[name]["attention"])
        hi = float(thresholds[name]["critical"])
        level, rate, deviation = _classify(value, median, lo, hi)
        in_range = float(stat["min"]) <= value <= float(stat["max"])
        label, unit = METRIC_LABELS[name], METRIC_UNITS[name]
        if rate is None:
            message = f"{label} 基线中位数为 0，无法按变化率判定，记录实际值 {value:.4g}{unit}"
        else:
            direction = "上升" if deviation > 0 else ("下降" if deviation < 0 else "持平")
            if level == "normal":
                message = (f"{label} {value:.4g}{unit} 相对基线中位数 {median:.4g}{unit} "
                           f"{direction} {abs(rate):.1%}，处于关注阈值 {lo:.0%} 以内")
            else:
                verb = "达到严重阈值" if level == "critical" else "超过关注阈值"
                advice = "建议停机检查" if level == "critical" else "建议安排复检"
                message = (f"{label} {value:.4g}{unit} 相对基线中位数 {median:.4g}{unit} "
                           f"{direction} {abs(rate):.1%}，{verb}（{lo:.0%}/{hi:.0%}），{advice}")
        metric_results.append({
            "metric": name,
            "label": label,
            "unit": unit,
            "value": _r6(value),
            "baseline_median": median,
            "baseline_min": float(stat["min"]),
            "baseline_max": float(stat["max"]),
            "deviation": _r6(deviation),
            "change_rate": _r6(rate) if rate is not None else None,
            "within_dispersion": in_range,
            "attention_threshold": lo,
            "critical_threshold": hi,
            "level": level,
            "message": message,
        })
        if LEVELS.index(level) > LEVELS.index(overall):
            overall = level
    return {"overall_level": overall, "metrics": metric_results}
