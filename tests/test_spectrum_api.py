"""频谱诊断测试：单边 FFT、阶次带能量、轴承特征频率判定、持久化与无效输入拒绝。"""
import math
import os

import pytest

# 使用独立测试数据库，需在导入 app 前设置
os.environ["VIBRATION_DB"] = "/tmp/test_vibration_spectrum.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.spectrum import bearing_characteristic_frequencies  # noqa: E402


@pytest.fixture()
def client():
    if os.path.exists("/tmp/test_vibration_spectrum.db"):
        os.remove("/tmp/test_vibration_spectrum.db")
    with TestClient(app) as c:
        yield c


# 典型深沟球轴承几何
GEOM = {
    "ball_count": 8,
    "ball_diameter": 6.746,
    "pitch_diameter": 28.5,
    "contact_angle": 0.0,
}


def make_record(client, equipment, n=1000, fs=1000.0, components=((50.0, 1.0),), dc=0.0):
    """上传一条等间隔采样记录，components 为 (频率 Hz, 幅值) 的正弦分量。"""
    samples = []
    for i in range(n):
        a = dc + sum(amp * math.sin(2 * math.pi * f * i / fs) for f, amp in components)
        samples.append({"t": i / fs, "a": a})
    r = client.post("/api/v1/analysis", json={
        "equipment_id": equipment, "sampling_frequency": fs, "samples": samples,
    })
    assert r.status_code == 201, r.text
    return r.json()["record_id"]


def diagnose(client, record_id, rpm=1500.0, order_bands=None, geometry=True, **overrides):
    body = {"record_id": record_id, "rpm": rpm}
    if order_bands is not None:
        body["order_bands"] = order_bands
    if geometry:
        body["bearing_geometry"] = GEOM if geometry is True else geometry
    body.update(overrides)
    return client.post("/api/v1/spectrum/diagnoses", json=body)


# ---------- 基础谱：去直流 / Hann 窗 / 主峰 / 分辨率 ----------

def test_basic_spectrum_main_peak_and_resolution(client):
    rid = make_record(client, "PUMP-S1", n=1000, fs=1000.0, components=((50.0, 2.0),), dc=3.0)
    r = diagnose(client, rid, geometry=False)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["frequency_resolution_hz"] == pytest.approx(1.0, abs=1e-6)
    assert body["nyquist_frequency_hz"] == pytest.approx(500.0)
    assert body["sample_count"] == 1000
    assert body["window"] == "hann"
    peak = body["main_peak"]
    assert peak["frequency_hz"] == pytest.approx(50.0, abs=0.5)
    assert peak["bin_index"] == 50
    # Hann coherent gain 0.5 → 幅值恢复后 ≈ 2.0（去直流不影响交流幅值）
    assert peak["amplitude"] == pytest.approx(2.0, rel=0.01)


def test_dc_removed_does_not_dominate_spectrum(client):
    # 强直流偏置被去除：主峰应落在交流分量上，0 Hz 线不参与主峰搜索
    rid = make_record(client, "PUMP-DC", components=((120.0, 0.5),), dc=10.0)
    body = diagnose(client, rid, geometry=False).json()
    assert body["main_peak"]["frequency_hz"] == pytest.approx(120.0, abs=0.5)


# ---------- 阶次换算与阶次带能量 ----------

def test_order_analysis_band_energy(client):
    # rpm=1500 → 转频 25 Hz；2X=50 Hz 集中了全部信号能量
    rid = make_record(client, "PUMP-O1", components=((50.0, 1.0),))
    r = diagnose(client, rid, rpm=1500.0, geometry=False, order_bands=[
        {"name": "1X", "order_min": 0.9, "order_max": 1.1},
        {"name": "2X", "order_min": 1.8, "order_max": 2.2},
    ])
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert order["shaft_frequency_hz"] == pytest.approx(25.0)
    assert order["peak_order"] == pytest.approx(2.0, abs=0.02)
    bands = {b["name"]: b for b in order["bands"]}
    assert bands["2X"]["energy_ratio"] == pytest.approx(1.0, abs=0.02)
    assert bands["1X"]["energy_ratio"] == pytest.approx(0.0, abs=0.02)
    # 阶次带换算回频率
    assert bands["2X"]["frequency_min_hz"] == pytest.approx(45.0)
    assert bands["2X"]["frequency_max_hz"] == pytest.approx(55.0)


# ---------- 轴承几何 → BPFO/BPFI/BSF/FTF 与命中判定 ----------

def test_characteristic_frequency_formulas():
    f = bearing_characteristic_frequencies(GEOM, rpm=1500.0)
    fr = 25.0
    ratio = 6.746 / 28.5
    assert f["bpfo"] == pytest.approx(fr * 4 * (1 - ratio))
    assert f["bpfi"] == pytest.approx(fr * 4 * (1 + ratio))
    assert f["bsf"] == pytest.approx(fr * 28.5 / 6.746 * (1 - ratio ** 2))
    assert f["ftf"] == pytest.approx(fr * 0.5 * (1 - ratio))
    # 量纲合理性：FTF < 转频 < BPFO/BPFI
    assert f["ftf"] < fr < f["bpfo"] < f["bpfi"]


def test_bearing_bpfo_critical_hit(client):
    fr = 25.0
    ratio = GEOM["ball_diameter"] / GEOM["pitch_diameter"]
    bpfo = fr * GEOM["ball_count"] / 2 * (1 - ratio)  # ≈ 76.23 Hz
    rid = make_record(client, "PUMP-B1", n=2048, fs=2000.0,
                      components=((bpfo, 1.0), (25.0, 0.2)))
    r = diagnose(client, rid, rpm=1500.0)
    assert r.status_code == 201, r.text
    bearing = r.json()["bearing"]
    assert bearing["status"] == "critical"
    bpfo_band = next(b for b in bearing["bands"] if b["fault"] == "bpfo")
    assert bpfo_band["level"] == "critical"
    assert bpfo_band["energy_ratio"] >= bearing["critical_ratio"]
    assert bearing["characteristic_frequencies_hz"]["bpfo"] == pytest.approx(bpfo, rel=1e-4)
    hits = bearing["hits"]
    assert any(h["fault"] == "bpfo" and h["level"] == "critical" for h in hits)
    assert "BPFO" in hits[0]["message"]
    # 顶层等级跟随轴承结论
    assert r.json()["level"] == "critical"


def test_bearing_attention_level(client):
    fr = 25.0
    ratio = GEOM["ball_diameter"] / GEOM["pitch_diameter"]
    bpfi = fr * GEOM["ball_count"] / 2 * (1 + ratio)  # ≈ 123.77 Hz
    rid = make_record(client, "PUMP-B2", n=2048, fs=2000.0,
                      components=((bpfi, 0.3), (53.0, 0.95)))  # 部分能量落在 BPFI 带外
    body = diagnose(client, rid, rpm=1500.0).json()
    bearing = body["bearing"]
    bpfi_band = next(b for b in bearing["bands"] if b["fault"] == "bpfi")
    assert bpfi_band["level"] == "attention"
    assert bearing["status"] == "attention"
    assert all(h["fault"] != "bpfi" or h["level"] == "attention" for h in bearing["hits"])


def test_bearing_normal_when_no_fault_energy(client):
    rid = make_record(client, "PUMP-B3", n=2048, fs=2000.0, components=((25.0, 1.0),))
    bearing = diagnose(client, rid, rpm=1500.0).json()["bearing"]
    assert bearing["status"] == "normal"
    assert bearing["hits"] == []
    assert len(bearing["bands"]) == 4
    assert {b["fault"] for b in bearing["bands"]} == {"bpfo", "bpfi", "bsf", "ftf"}


def test_threshold_overrides_from_request(client):
    fr = 25.0
    ratio = GEOM["ball_diameter"] / GEOM["pitch_diameter"]
    bpfo = fr * GEOM["ball_count"] / 2 * (1 - ratio)
    rid = make_record(client, "PUMP-B4", n=2048, fs=2000.0, components=((bpfo, 1.0),))
    # 把阈值抬到 BPFO 实际占比之上 → normal，且回显配置
    body = diagnose(client, rid, rpm=1500.0,
                    bearing_attention_ratio=0.999, bearing_critical_ratio=0.9995).json()
    bearing = body["bearing"]
    assert bearing["attention_ratio"] == 0.999
    assert bearing["critical_ratio"] == 0.9995
    assert next(b for b in bearing["bands"] if b["fault"] == "bpfo")["level"] == "normal"
    assert bearing["status"] == "normal"


# ---------- 无几何参数：unavailable ----------

def test_bearing_unavailable_without_geometry(client):
    rid = make_record(client, "PUMP-U1")
    body = diagnose(client, rid, geometry=False).json()
    bearing = body["bearing"]
    assert bearing["status"] == "unavailable"
    assert body["level"] == "unavailable"
    assert set(bearing["missing_fields"]) == {
        "ball_count", "ball_diameter", "pitch_diameter", "contact_angle"}
    assert "ball_count" in bearing["reason"]
    assert bearing["bands"] == [] and bearing["hits"] == []
    # 基础谱与阶次结果仍然保留
    assert body["main_peak"]["frequency_hz"] > 0
    assert body["order"]["rpm"] == 1500.0


def test_bearing_unavailable_with_partial_geometry(client):
    rid = make_record(client, "PUMP-U2")
    partial = {"ball_count": 8, "ball_diameter": 6.746}  # 缺节径、接触角
    r = client.post("/api/v1/spectrum/diagnoses", json={
        "record_id": rid, "rpm": 1500.0, "bearing_geometry": partial,
    })
    assert r.status_code == 201, r.text
    bearing = r.json()["bearing"]
    assert bearing["status"] == "unavailable"
    assert set(bearing["missing_fields"]) == {"pitch_diameter", "contact_angle"}


# ---------- 持久化：查询 / 时间范围 / 详情 ----------

def test_diagnosis_persistence_query_and_detail(client):
    rid = make_record(client, "PUMP-Q1", components=((50.0, 1.0),))
    created = diagnose(client, rid, geometry=False).json()
    diag_id = created["diagnosis_id"]
    assert created["created_at"]

    detail = client.get(f"/api/v1/spectrum/diagnoses/{diag_id}")
    assert detail.status_code == 200
    assert detail.json()["main_peak"] == created["main_peak"]
    assert detail.json()["source_record_id"] == rid
    assert detail.json()["equipment_id"] == "PUMP-Q1"

    make_record(client, "PUMP-Q2")
    diagnose(client, make_record(client, "PUMP-Q2"), geometry=False)
    listing = client.get("/api/v1/spectrum/diagnoses", params={"equipment_id": "PUMP-Q1"})
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["diagnosis_id"] == diag_id
    assert "main_peak" in body["items"][0]


def test_diagnosis_time_range_filter(client):
    rid = make_record(client, "PUMP-T1")
    diagnose(client, rid, geometry=False)
    listing = client.get("/api/v1/spectrum/diagnoses", params={
        "equipment_id": "PUMP-T1",
        "start_time": "2000-01-01T00:00:00Z",
        "end_time": "2000-01-02T00:00:00Z",
    })
    assert listing.status_code == 200
    assert listing.json()["total"] == 0

    future = client.get("/api/v1/spectrum/diagnoses", params={
        "start_time": "2000-01-01T00:00:00Z",
    })
    assert future.json()["total"] >= 1


def test_diagnosis_detail_not_found_404(client):
    r = client.get("/api/v1/spectrum/diagnoses/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"


def test_source_record_not_found_404(client):
    r = client.post("/api/v1/spectrum/diagnoses", json={"record_id": 999, "rpm": 1500.0})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RECORD_NOT_FOUND"


# ---------- 无效输入 ----------

def test_non_uniform_sampling_rejected_400(client):
    fs = 1000.0
    samples = [{"t": i / fs, "a": math.sin(2 * math.pi * 50 * i / fs)} for i in range(64)]
    samples[30]["t"] -= 0.0001  # 压缩一个采样间隔（保持时间严格递增，但间隔不一致）
    r = client.post("/api/v1/analysis", json={
        "equipment_id": "PUMP-E1", "sampling_frequency": fs, "samples": samples,
    })
    assert r.status_code == 201
    rid = r.json()["record_id"]
    diag = diagnose(client, rid, geometry=False)
    assert diag.status_code == 400
    assert diag.json()["error"]["code"] == "NON_UNIFORM_SAMPLING"


def test_sampling_interval_mismatch_rejected_400(client):
    # 实际间隔 0.002s，但记录声明 fs=1000（0.001s）
    samples = [{"t": i / 500.0, "a": 0.1 * i} for i in range(32)]
    r = client.post("/api/v1/analysis", json={
        "equipment_id": "PUMP-E2", "sampling_frequency": 1000.0, "samples": samples,
    })
    assert r.status_code == 201
    diag = diagnose(client, r.json()["record_id"], geometry=False)
    assert diag.status_code == 400
    assert diag.json()["error"]["code"] == "NON_UNIFORM_SAMPLING"


def test_order_band_beyond_nyquist_rejected_400(client):
    # fs=1000 → Nyquist=500Hz；rpm=1500 → 转频 25Hz；100 阶 = 2500Hz 越界
    rid = make_record(client, "PUMP-E3", fs=1000.0)
    r = diagnose(client, rid, geometry=False, order_bands=[
        {"name": "超高阶", "order_min": 90.0, "order_max": 100.0},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAND_OUT_OF_RANGE"


def test_invalid_order_band_422(client):
    rid = make_record(client, "PUMP-E4")
    r = diagnose(client, rid, geometry=False, order_bands=[
        {"order_min": 3.0, "order_max": 2.0},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_bearing_geometry_422(client):
    rid = make_record(client, "PUMP-E5")
    bad = {**GEOM, "ball_diameter": 50.0}  # d >= D，物理上不成立
    r = diagnose(client, rid, geometry=bad)
    assert r.status_code == 422


def test_bad_rpm_and_missing_record_id_422(client):
    r1 = client.post("/api/v1/spectrum/diagnoses", json={"record_id": 1, "rpm": 0})
    assert r1.status_code == 422
    r2 = client.post("/api/v1/spectrum/diagnoses", json={"rpm": 1500.0})
    assert r2.status_code == 422


def test_invalid_time_range_400(client):
    bad_iso = client.get("/api/v1/spectrum/diagnoses", params={"start_time": "not-a-time"})
    assert bad_iso.status_code == 400
    assert bad_iso.json()["error"]["code"] == "INVALID_TIME_RANGE"
    swapped = client.get("/api/v1/spectrum/diagnoses", params={
        "start_time": "2026-09-13T10:00:00Z",
        "end_time": "2026-09-13T08:00:00Z",
    })
    assert swapped.status_code == 400
    assert swapped.json()["error"]["code"] == "INVALID_TIME_RANGE"


# ---------- 设备级阈值配置联动 ----------

def test_equipment_bearing_thresholds_used(client):
    client.put("/api/v1/thresholds/PUMP-CFG", json={
        "rms_attention": 100.0, "rms_critical": 200.0,
        "p2p_attention": 100.0, "p2p_critical": 200.0,
        "crest_attention": 100.0, "crest_critical": 200.0,
        "peak_attention": 100.0, "peak_critical": 200.0,
        "window_seconds": 1.0, "min_samples": 8,
        "bearing_attention_ratio": 0.9,
        "bearing_critical_ratio": 0.95,
        "bearing_band_tolerance": 0.03,
    })
    rid = make_record(client, "PUMP-CFG", n=2048, fs=2000.0,
                      components=((50.0, 1.0),))
    body = diagnose(client, rid, rpm=1500.0).json()
    bearing = body["bearing"]
    assert bearing["attention_ratio"] == 0.9
    assert bearing["band_tolerance"] == 0.03
    assert bearing["status"] == "normal"


def test_threshold_config_rejects_bad_bearing_ratio(client):
    r = client.put("/api/v1/thresholds/PUMP-CFG2", json={
        "rms_attention": 1.0, "rms_critical": 2.0,
        "p2p_attention": 3.0, "p2p_critical": 8.0,
        "crest_attention": 3.0, "crest_critical": 5.0,
        "peak_attention": 2.0, "peak_critical": 4.0,
        "window_seconds": 1.0, "min_samples": 8,
        "bearing_attention_ratio": 0.8,
        "bearing_critical_ratio": 0.5,  # attention >= critical
    })
    assert r.status_code == 422
