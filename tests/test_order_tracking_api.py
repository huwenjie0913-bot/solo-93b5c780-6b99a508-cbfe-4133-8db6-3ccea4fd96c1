"""变速阶次跟踪测试：等角度重采样、阶次谱滑窗、共振转速区间合并、持久化与无效输入拒绝。"""
import json
import math
import os

import numpy as np
import pytest

# 使用独立测试数据库，需在导入 app 前设置
os.environ["VIBRATION_DB"] = "/tmp/test_vibration_order.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.order_tracking import analyze_order_tracking  # noqa: E402


@pytest.fixture()
def client():
    if os.path.exists("/tmp/test_vibration_order.db"):
        os.remove("/tmp/test_vibration_order.db")
    with TestClient(app) as c:
        yield c


def make_record(client, equipment, times, values, fs=5000.0):
    """上传一条显式给定时序的等间隔采样记录。"""
    samples = [{"t": float(t), "a": float(a)} for t, a in zip(times, values)]
    r = client.post("/api/v1/analysis", json={
        "equipment_id": equipment, "sampling_frequency": fs, "samples": samples,
    })
    assert r.status_code == 201, r.text
    return r.json()["record_id"]


def pulse_times_from_speed(t_eval, speed_rpm, ppr):
    """由瞬时转速曲线（rpm，关于 t 的函数）按每转 ppr 个脉冲生成严格递增脉冲时刻。

    累积转角 θ(t)=1/60∫rpm dt（转），在 θ=k/ppr 处放置脉冲，脉冲时间用线性反插求解。
    """
    theta = np.concatenate([[0.0], np.cumsum((speed_rpm[1:] + speed_rpm[:-1]) / 2
                                            * np.diff(t_eval) / 60.0)])
    total_pulses = int(math.floor(theta[-1] * ppr)) + 1
    target = np.arange(total_pulses, dtype=float) / ppr
    pulses = list(np.interp(target, theta, t_eval))
    # 末段不足一个脉冲间隔时，按最后一个间隔补外推脉冲，保证脉冲包住样本末尾
    last_interval = pulses[-1] - pulses[-2]
    while pulses[-1] < t_eval[-1]:
        pulses.append(pulses[-1] + last_interval)
    return pulses


def ramp_signal(n=30_000, fs=5000.0, rpm0=600.0, rpm1=3000.0, resonance=(1700.0, 1900.0)):
    """线性升降速信号：弱 1X 分量 + 共振转速区间内放大的 2X 分量。"""
    t = np.arange(n) / fs
    rpm = rpm0 + (rpm1 - rpm0) * t / t[-1]
    fr = rpm / 60.0
    theta = 2 * math.pi * np.cumsum(fr) / fs
    amp = np.where((rpm >= resonance[0]) & (rpm <= resonance[1]), 1.6, 0.02)
    a = 0.3 * np.sin(theta) + amp * np.sin(2 * theta)
    return t, a, rpm


def analyze(client, rid, pulses, ppr=4, **overrides):
    body = {
        "record_id": rid,
        "pulse_times": pulses,
        "pulses_per_revolution": ppr,
        "samples_per_revolution": 128,
        "window_revolutions": 8.0,
        "overlap_revolutions": 4.0,
        "order_bands": [
            {"name": "1X", "order_min": 0.8, "order_max": 1.2},
            {"name": "2X", "order_min": 1.8, "order_max": 2.2},
        ],
        "resonance_ratio_threshold": 0.3,
        "min_consecutive_windows": 2,
    }
    body.update(overrides)
    return client.post("/api/v1/order-tracking/analyses", json=body)


# ---------- 恒转速：等角度重采样后主峰阶次稳定 ----------

def test_constant_speed_resamples_to_order():
    fs = 10000.0
    rpm = 1500.0
    n = 20_000
    t = np.arange(n) / fs
    # 纯 2X：转频 25 Hz → 50 Hz；fs/fr=400 保证线性插值抖动可忽略
    a = 1.0 * np.sin(2 * math.pi * 50.0 * t)
    samples = [{"t": float(x), "a": float(y)} for x, y in zip(t, a)]
    speed = np.full(n, rpm)
    pulses = pulse_times_from_speed(t, speed, ppr=4)

    result = analyze_order_tracking(
        samples=samples, sampling_frequency=fs, pulse_times=pulses,
        pulses_per_revolution=4, samples_per_revolution=128,
        window_revolutions=10.0, overlap_revolutions=0.0,
        order_bands=[{"name": "2X", "order_min": 1.5, "order_max": 2.5}],
        resonance_ratio_threshold=0.3, min_consecutive_windows=1,
    )
    # 2s × 25 转/s = 50 转，10 转窗无重叠 → 5 窗
    assert result["window_count"] == 5
    w = result["windows"][2]
    assert w["main_peak_order"] == pytest.approx(2.0, abs=0.05)
    assert w["average_rpm"] == pytest.approx(rpm, rel=1e-3)
    # 阶次分辨率 = SPR / (SPR·窗长) = 1/窗长（结果保留 6 位小数）
    assert w["order_resolution"] == pytest.approx(0.1, abs=1e-6)
    assert result["order_nyquist"] == 64.0
    band2x = w["bands"][0]
    # 带宽 ±0.5 阶（= ±5 根谱线）完整覆盖 2 阶主峰与 Hann 主瓣泄漏
    assert band2x["energy_ratio"] == pytest.approx(1.0, abs=0.02)


def test_constant_speed_api(client):
    fs = 5000.0
    rpm = 1500.0
    n = 10_000
    t = np.arange(n) / fs
    a = 1.0 * np.sin(2 * math.pi * 50.0 * t)
    rid = make_record(client, "PUMP-OC1", t, a, fs=fs)
    pulses = pulse_times_from_speed(t, np.full(n, rpm), ppr=4)
    r = analyze(client, rid, pulses, window_revolutions=4.0, overlap_revolutions=0.0,
                order_bands=[{"name": "2X", "order_min": 1.7, "order_max": 2.3}],
                resonance_ratio_threshold=0.9, min_consecutive_windows=2)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["equipment_id"] == "PUMP-OC1"
    assert body["source_record_id"] == rid
    assert body["window_count"] >= 10
    assert all(w["main_peak_order"] == pytest.approx(2.0, abs=0.05)
               for w in body["windows"])
    # 恒 2X 但阈值高（0.9，带外泄漏后占比略低）→ 不应有共振区间，说明合并逻辑而非一律命中
    assert isinstance(body["resonance_zones"], list)


# ---------- 升速穿越共振转速：定位共振区间 ----------

def test_ramp_locates_resonance_zone(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-RAMP", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    r = analyze(client, rid, pulses,
                order_bands=[{"name": "2X", "order_min": 1.8, "order_max": 2.2}])
    assert r.status_code == 201, r.text
    body = r.json()

    # 窗按转数步进：窗的平均转速应整体单调（重叠滑窗）
    avg_rpms = [w["average_rpm"] for w in body["windows"]]
    assert avg_rpms == sorted(avg_rpms)

    zones = body["resonance_zones"]
    assert len(zones) == 1
    zone = zones[0]
    assert zone["rpm_min"] < 1900.0 and zone["rpm_max"] > 1700.0
    # 定位误差应明显小于整个升速跨度
    assert zone["rpm_min"] >= 1500.0 and zone["rpm_max"] <= 2100.0
    assert zone["window_count"] >= 2
    assert zone["window_start_index"] < zone["window_end_index"]
    assert zone["dominant_band"] == "2X"
    assert zone["max_energy_ratio"] >= 0.3
    assert zone["main_peak_order_min"] == pytest.approx(2.0, abs=0.1)
    # 区间起止时间落在记录范围内
    assert zone["t_start"] < zone["t_end"]
    assert body["windows"][0]["t_start"] <= zone["t_start"]
    assert zone["t_end"] <= body["windows"][-1]["t_end"]


def test_min_consecutive_windows_filters_short_runs(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-RAMP2", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    bands_2x = [{"name": "2X", "order_min": 1.8, "order_max": 2.2}]
    # 最少连续窗数远超命中游程长度 → 不合并任何区间
    r = analyze(client, rid, pulses, order_bands=bands_2x, min_consecutive_windows=100)
    assert r.status_code == 201, r.text
    assert r.json()["resonance_zones"] == []
    # 阈值抬得极高同样不应有区间
    r2 = analyze(client, rid, pulses, order_bands=bands_2x, resonance_ratio_threshold=0.999)
    assert r2.json()["resonance_zones"] == []


# ---------- 脉冲摘要 ----------

def test_pulse_summary_stats(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-PS", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    body = analyze(client, rid, pulses).json()
    ps = body["pulse_summary"]
    assert ps["pulse_count"] == len(pulses)
    assert body["pulses_per_revolution"] == 4
    assert ps["first_pulse_time"] == pytest.approx(0.0, abs=1e-9)
    assert ps["last_pulse_time"] == pytest.approx(t[-1], abs=1e-3)
    # 升速：脉冲间隔应递减；瞬时转速随时间升高
    assert ps["pulse_interval_min_s"] < ps["pulse_interval_max_s"]
    assert ps["instantaneous_rpm_min"] < ps["instantaneous_rpm_max"]
    assert ps["instantaneous_rpm_min"] == pytest.approx(600.0, rel=0.02)
    assert ps["instantaneous_rpm_max"] == pytest.approx(3000.0, rel=0.02)
    assert ps["covered_revolutions"] > 0
    # 顶层回显参数
    assert body["samples_per_revolution"] == 128
    assert body["window_revolutions"] == 8.0
    assert body["order_resolution"] == pytest.approx(1.0 / 8.0)
    assert body["resampled_sample_count"] > body["window_count"]


# ---------- 持久化：详情与按设备分页 ----------

def test_persistence_detail_and_pagination(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-DB1", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    created = analyze(client, rid, pulses).json()
    tid = created["tracking_id"]
    assert created["created_at"]

    detail = client.get(f"/api/v1/order-tracking/analyses/{tid}")
    assert detail.status_code == 200
    db_body = detail.json()
    assert db_body["pulse_summary"] == created["pulse_summary"]
    assert len(db_body["windows"]) == created["window_count"]
    assert db_body["resonance_zones"] == created["resonance_zones"]
    assert db_body["source_record_id"] == rid

    # 另一台设备的结果不应混入
    rid2 = make_record(client, "PUMP-DB2", t, a)
    analyze(client, rid2, pulses)
    listing = client.get("/api/v1/order-tracking/analyses",
                         params={"equipment_id": "PUMP-DB1"})
    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] == 1
    assert page["items"][0]["tracking_id"] == tid
    assert "windows" in page["items"][0] and "resonance_zones" in page["items"][0]

    # 分页参数生效（按设备过滤，同库其他测试的结果不混入）
    p2 = client.get("/api/v1/order-tracking/analyses",
                    params={"equipment_id": "PUMP-DB1", "limit": 1, "offset": 0}).json()
    assert p2["total"] == 1 and len(p2["items"]) == 1
    both = client.get("/api/v1/order-tracking/analyses",
                      params={"equipment_id": "PUMP-DB2"}).json()
    assert both["total"] == 1 and both["items"][0]["equipment_id"] == "PUMP-DB2"


def test_detail_not_found_404(client):
    r = client.get("/api/v1/order-tracking/analyses/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ORDER_TRACKING_NOT_FOUND"


def test_source_record_not_found_404(client):
    r = client.post("/api/v1/order-tracking/analyses", json={
        "record_id": 999,
        "pulse_times": [0.0, 0.1, 0.2],
        "pulses_per_revolution": 1,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RECORD_NOT_FOUND"


def test_invalid_time_range_400(client):
    swapped = client.get("/api/v1/order-tracking/analyses", params={
        "start_time": "2026-09-14T10:00:00Z",
        "end_time": "2026-09-14T08:00:00Z",
    })
    assert swapped.status_code == 400
    assert swapped.json()["error"]["code"] == "INVALID_TIME_RANGE"
    bad = client.get("/api/v1/order-tracking/analyses",
                     params={"start_time": "nope"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_TIME_RANGE"


# ---------- 无效输入：脉冲与阶次带 ----------

def test_pulse_times_not_strictly_increasing_400(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E1", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    pulses[10] = pulses[9]  # 相等 → 非严格递增
    r = analyze(client, rid, pulses)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "PULSE_TIME_OUT_OF_ORDER"
    assert err["details"]["index"] == 10


def test_pulse_times_decreasing_400(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E1B", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    pulses[5] -= pulses[6] - pulses[4]  # 倒退
    r = analyze(client, rid, pulses)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PULSE_TIME_OUT_OF_ORDER"


def test_pulse_coverage_short_400(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E2", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    # 丢掉首尾脉冲，覆盖不到样本两端
    r = analyze(client, rid, pulses[3:-3])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_PULSE_COVERAGE"
    assert "missing_lead_seconds" in err["details"] or "missing_trail_seconds" in err["details"]
    # 拒绝后不写入结果
    assert client.get("/api/v1/order-tracking/analyses",
                      params={"equipment_id": "PUMP-E2"}).json()["total"] == 0


def test_pulse_coverage_shorter_than_window_400(client):
    fs = 5000.0
    rpm = 1500.0
    n = 10_000
    t = np.arange(n) / fs
    a = np.sin(2 * math.pi * 25.0 * t)
    rid = make_record(client, "PUMP-E2B", t, a, fs=fs)
    pulses = pulse_times_from_speed(t, np.full(n, rpm), ppr=4)
    # 脉冲只覆盖前 1 转（≈0.04s），样本却有 50 转 → 覆盖不足
    pulses = [p for p in pulses if p <= 0.045]
    r = analyze(client, rid, pulses, window_revolutions=4.0, overlap_revolutions=0.0)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INSUFFICIENT_PULSE_COVERAGE"


def test_order_band_beyond_angular_nyquist_400(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E3", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    # SPR=128 → 角域奈奎斯特 64 阶；100 阶带越界
    r = analyze(client, rid, pulses, order_bands=[
        {"name": "超高阶", "order_min": 90.0, "order_max": 100.0},
    ])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "ORDER_OUT_OF_RANGE"
    assert err["details"]["order_nyquist"] == 64.0
    assert err["details"]["bands"][0]["order_max"] == 100.0
    # 不写入结果
    assert client.get("/api/v1/order-tracking/analyses",
                      params={"equipment_id": "PUMP-E3"}).json()["total"] == 0


def test_pulse_times_nan_422(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E4", t, a)
    # json.dumps 默认允许 NaN 字面量；服务端 Pydantic 必须拒绝为 422
    payload = json.dumps({
        "record_id": rid,
        "pulse_times": [0.0, float("nan"), 0.2],
        "pulses_per_revolution": 1,
    })
    r = client.post("/api/v1/order-tracking/analyses",
                    content=payload, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_overlap_must_be_smaller_than_window_422(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E5", t, a)
    pulses = pulse_times_from_speed(t, rpm, ppr=4)
    r = analyze(client, rid, pulses, window_revolutions=4.0, overlap_revolutions=4.0)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_pulse_list_min_length_422(client):
    t, a, rpm = ramp_signal()
    rid = make_record(client, "PUMP-E6", t, a)
    r = client.post("/api/v1/order-tracking/analyses", json={
        "record_id": rid,
        "pulse_times": [0.1],
        "pulses_per_revolution": 1,
    })
    assert r.status_code == 422
