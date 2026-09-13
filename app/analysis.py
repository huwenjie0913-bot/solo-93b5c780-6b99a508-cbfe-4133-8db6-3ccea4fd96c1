"""振动信号分析核心：指标计算 + 规则判定。"""
from __future__ import annotations

import math

from .errors import ApiError

LEVELS = ("normal", "attention", "critical")

# 设备未配置阈值时使用的默认配置
DEFAULT_THRESHOLDS: dict = {
    "rms_attention": 2.8,
    "rms_critical": 7.1,
    "p2p_attention": 10.0,
    "p2p_critical": 25.0,
    "crest_attention": 4.0,
    "crest_critical": 6.0,
    "peak_attention": 5.0,
    "peak_critical": 12.0,
    "window_seconds": 0.5,
    "min_samples": 8,
}


def validate_samples(samples: list[dict], min_samples: int) -> None:
    """业务校验：样本数量与时间单调性，违反时抛出明确的 4xx。"""
    if len(samples) < min_samples:
        raise ApiError(
            400,
            "INSUFFICIENT_SAMPLES",
            f"样本数量不足：收到 {len(samples)} 个，至少需要 {min_samples} 个",
            {"received": len(samples), "required": min_samples},
        )
    for i in range(1, len(samples)):
        if samples[i]["t"] <= samples[i - 1]["t"]:
            raise ApiError(
                400,
                "TIME_OUT_OF_ORDER",
                f"样本时间未严格递增：第 {i} 个样本 t={samples[i]['t']} 不大于前一个 t={samples[i - 1]['t']}",
                {"index": i, "t": samples[i]["t"], "previous_t": samples[i - 1]["t"]},
            )


def compute_metrics(samples: list[dict]) -> dict:
    """计算 RMS、峰峰值、峰值因子、绝对峰值。"""
    values = [s["a"] for s in samples]
    n = len(values)
    rms = math.sqrt(sum(v * v for v in values) / n)
    peak_to_peak = max(values) - min(values)
    peak_abs = max(abs(v) for v in values)
    crest_factor = peak_abs / rms if rms > 0 else 0.0
    duration = samples[-1]["t"] - samples[0]["t"]
    return {
        "rms": rms,
        "peak_to_peak": peak_to_peak,
        "crest_factor": crest_factor,
        "peak_abs": peak_abs,
        "duration_seconds": duration,
    }


def longest_exceedance_seconds(samples: list[dict], threshold: float, fs: float) -> float:
    """|a| 连续超过 threshold 的最长持续时间（按采样间隔换算）。"""
    best = run = 0
    for s in samples:
        if abs(s["a"]) >= threshold:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best / fs


def evaluate(metrics: dict, samples: list[dict], fs: float, th: dict) -> tuple[str, list[dict]]:
    """按阈值与连续超限窗口判定等级，返回 (level, triggered_rules)。"""
    rules: list[dict] = []

    def add(rule: str, metric: str, value: float, threshold: float, level: str, message: str) -> None:
        rules.append({
            "rule": rule, "metric": metric, "value": round(value, 6),
            "threshold": threshold, "level": level, "message": message,
        })

    # 规则 1：整体能量 RMS
    if metrics["rms"] >= th["rms_critical"]:
        add("RMS_CRITICAL", "rms", metrics["rms"], th["rms_critical"], "critical",
            f"RMS {metrics['rms']:.3f} m/s² 达到严重阈值 {th['rms_critical']}，整体振动能量过高，建议停机检查")
    elif metrics["rms"] >= th["rms_attention"]:
        add("RMS_ATTENTION", "rms", metrics["rms"], th["rms_attention"], "attention",
            f"RMS {metrics['rms']:.3f} m/s² 超过关注阈值 {th['rms_attention']}，整体振动能量偏高")

    # 规则 2：峰峰值（单次冲击幅度）
    if metrics["peak_to_peak"] >= th["p2p_critical"]:
        add("P2P_CRITICAL", "peak_to_peak", metrics["peak_to_peak"], th["p2p_critical"], "critical",
            f"峰峰值 {metrics['peak_to_peak']:.3f} m/s² 达到严重阈值 {th['p2p_critical']}，存在剧烈冲击")
    elif metrics["peak_to_peak"] >= th["p2p_attention"]:
        add("P2P_ATTENTION", "peak_to_peak", metrics["peak_to_peak"], th["p2p_attention"], "attention",
            f"峰峰值 {metrics['peak_to_peak']:.3f} m/s² 超过关注阈值 {th['p2p_attention']}")

    # 规则 3：峰值因子（冲击性/调制特征）
    if metrics["crest_factor"] >= th["crest_critical"]:
        add("CREST_CRITICAL", "crest_factor", metrics["crest_factor"], th["crest_critical"], "critical",
            f"峰值因子 {metrics['crest_factor']:.3f} 达到严重阈值 {th['crest_critical']}，冲击特征显著")
    elif metrics["crest_factor"] >= th["crest_attention"]:
        add("CREST_ATTENTION", "crest_factor", metrics["crest_factor"], th["crest_attention"], "attention",
            f"峰值因子 {metrics['crest_factor']:.3f} 超过关注阈值 {th['crest_attention']}，出现冲击成分")

    # 规则 4：连续超限窗口（区分瞬时尖峰与持续超限）
    window = th["window_seconds"]
    exceed_c = longest_exceedance_seconds(samples, th["peak_critical"], fs)
    if exceed_c >= window:
        add("SUSTAINED_CRITICAL", "peak_abs", exceed_c, window, "critical",
            f"瞬时幅值连续 {exceed_c:.3f}s 超过严重阈值 {th['peak_critical']} m/s²"
            f"（窗口 {window}s），属于持续超限而非偶发尖峰，建议停机检查")
    else:
        exceed_a = longest_exceedance_seconds(samples, th["peak_attention"], fs)
        if exceed_a >= window:
            add("SUSTAINED_ATTENTION", "peak_abs", exceed_a, window, "attention",
                f"瞬时幅值连续 {exceed_a:.3f}s 超过关注阈值 {th['peak_attention']} m/s²（窗口 {window}s）")

    level = "normal"
    for r in rules:
        if LEVELS.index(r["level"]) > LEVELS.index(level):
            level = r["level"]
    return level, rules
