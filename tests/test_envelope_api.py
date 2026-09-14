"""包络解调诊断接口回归：手动/峭度自动选带、故障识别、正常样本不误报、错误不落库、持久化筛选。"""
import math
import os

import numpy as np
import pytest

# 使用独立测试数据库，需在导入 app 前设置
DB_PATH = "/tmp/test_vibration_envelope.db"
os.environ["VIBRATION_DB"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.spectrum import bearing_characteristic_frequencies  # noqa: E402


@pytest.fixture()
def client():
    # 同一会话内其他测试模块可能覆盖 VIBRATION_DB，夹具内重新固定到本文件的库，
    # 结束后恢复原值，避免污染后续模块（各模块库路径在模块导入期设置）。
    saved = os.environ.get("VIBRATION_DB")
    os.environ["VIBRATION_DB"] = DB_PATH
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        if saved is None:
            os.environ.pop("VIBRATION_DB", None)
        else:
            os.environ["VIBRATION_DB"] = saved


# 典型深沟球轴承几何，与频谱诊断测试一致
GEOM = {
    "ball_count": 8,
    "ball_diameter": 6.746,
    "pitch_diameter": 28.5,
    "contact_angle": 0.0,
}

FS = 10000.0
N = 20000
RPM = 1500.0
FR = RPM / 60.0
CARRIER = 1000.0
CHAR = bearing_characteristic_frequencies(GEOM, RPM)


def _ring_impulses(fault_freq, jitter=0.0, amplitude=1.0, rng=None):
    """以 fault_freq 为周期、在载波 CARRIER 上产生衰减振荡冲击序列（轴承缺陷的简化模型）。"""
    rng = rng or np.random.default_rng(0)
    impulses = np.zeros(N)
    period = FS / fault_freq
    k = 0
    while k * period < N:
        idx = int(round(k * period + (rng.normal(0, jitter) if jitter else 0.0)))
        if 0 <= idx < N:
            impulses[idx] = amplitude
        k += 1
    tau = np.arange(64) / FS
    pulse = np.exp(-tau * 400.0) * np.sin(2 * math.pi * CARRIER * tau)
    return np.convolve(impulses, pulse)[:N]


def make_record(client, equipment, values, fs=FS):
    samples = [{"t": i / fs, "a": float(v)} for i, v in enumerate(values)]
    r = client.post("/api/v1/analysis", json={
        "equipment_id": equipment, "sampling_frequency": fs, "samples": samples,
    })
    assert r.status_code == 201, r.text
    return r.json()["record_id"]


def envelope_diagnose(client, record_id, **overrides):
    body = {"record_id": record_id, "rpm": RPM, "bearing_geometry": GEOM}
    body.update(overrides)
    return client.post("/api/v1/envelope/diagnoses", json=body)


def fault_signal(fault, rng_seed=0, amplitude=1.0, noise=0.3, am=False):
    rng = np.random.default_rng(rng_seed)
    t = np.arange(N) / FS
    jitter = {"bpfi": 2.0, "bsf": 1.5}.get(fault, 0.0)
    ring = _ring_impulses(CHAR[fault], jitter=jitter, amplitude=amplitude, rng=rng)
    if am:  # 内圈缺陷随载荷区受 1X 幅值调制
        ring = ring * (0.5 + 0.5 * np.sin(2 * math.pi * FR * t))
    return ring + 0.5 * np.sin(2 * math.pi * FR * t) + noise * rng.standard_normal(N)


def clean_signal(rng_seed=1):
    rng = np.random.default_rng(rng_seed)
    t = np.arange(N) / FS
    return 0.5 * np.sin(2 * math.pi * FR * t) + 0.3 * rng.standard_normal(N)


# ---------- 手动载波频带：BPFO/BPFI/BSF 识别 ----------

@pytest.mark.parametrize("fault,am", [("bpfo", False), ("bpfi", True), ("bsf", False)])
def test_manual_band_detects_fault_families(client, fault, am):
    rid = make_record(client, f"BEARING-{fault.upper()}", fault_signal(fault, am=am))
    r = envelope_diagnose(client, rid, carrier_band={"low_hz": 700.0, "high_hz": 1400.0})
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["level"] in ("attention", "critical")
    assert body["status"] == body["level"]
    assert body["dominant_fault"] == fault
    assert body["confidence"] in ("high", "medium")

    selection = body["band_selection"]
    assert selection["mode"] == "manual"
    assert selection["selected_band_min_hz"] == 700.0
    assert selection["selected_band_max_hz"] == 1400.0
    assert selection["candidates"] == []
    assert "显式指定载波频带" in selection["reason"]
    assert body["carrier_band"]["band_kurtosis"] > 3.0

    families = {f["fault"]: f for f in body["fault_families"]}
    dominant = families[fault]
    assert dominant["fundamental_matched"] is True
    assert dominant["matched_harmonic_orders"]  # 至少一次谐波
    assert dominant["energy_ratio"] >= body["attention_ratio"]
    # 真实故障能量远高于其余故障族（谱线按最近目标唯一归属，不重复计数）
    others = [f["energy_ratio"] for k, f in families.items() if k != fault]
    assert dominant["energy_ratio"] > 3.0 * max(others)

    # 谱峰匹配明细：目标频率、偏差与基频/谐波/边带分类
    matches = [m for m in body["peak_matches"] if m["fault"] == fault]
    assert any(m["kind"] == "fundamental" and m["matched"] for m in matches)
    assert any(m["kind"] in ("harmonic", "lower_sideband", "upper_sideband")
               and m["matched"] for m in matches)
    for m in matches:
        assert m["target_frequency_hz"] > 0
        if m["matched"]:
            assert abs(m["deviation_hz"]) <= body["match_tolerance_hz"] + 1e-6

    # 特征频率与中文判据
    assert body["characteristic_frequencies_hz"][fault] == pytest.approx(CHAR[fault], rel=1e-4)
    assert f"{CHAR[fault]:.2f}" in body["conclusion"]
    fault_cn = {"bpfo": "外圈", "bpfi": "内圈", "bsf": "滚动体", "ftf": "保持架"}[fault]
    assert fault_cn in body["conclusion"]
    assert "包络解调" in body["conclusion"]


def test_manual_band_bpfi_has_1x_sidebands(client):
    rid = make_record(client, "BEARING-BPFI-SB", fault_signal("bpfi", am=True))
    body = envelope_diagnose(client, rid, carrier_band={"low_hz": 700.0, "high_hz": 1400.0}).json()
    bpfi = next(f for f in body["fault_families"] if f["fault"] == "bpfi")
    # 受转频调制的内圈缺陷应在谐波两侧出现 1X 边带
    assert bpfi["matched_sideband_harmonics"]
    assert bpfi["lower_sideband_hits"] or bpfi["upper_sideband_hits"]
    hit = (bpfi["lower_sideband_hits"] + bpfi["upper_sideband_hits"])[0]
    assert hit["sideband_order"] == 1
    assert abs(abs(hit["target_frequency_hz"]
                   - hit["harmonic"] * CHAR["bpfi"]) - FR) < body["match_tolerance_hz"] + 1.0


# ---------- 峭度自动选带 ----------

def test_auto_band_selection_by_kurtosis(client):
    values = fault_signal("bpfo")
    rid = make_record(client, "BEARING-AUTO", values)
    r = envelope_diagnose(client, rid)  # 不传任何频带参数 → 默认区域峭度自动选带
    assert r.status_code == 201, r.text
    body = r.json()

    selection = body["band_selection"]
    assert selection["mode"] == "auto"
    assert selection["candidate_count"] >= 2
    assert len(selection["candidates"]) == selection["candidate_count"]
    # 胜出候选带的峭度必须不小于任何其他候选
    kurt_list = [c["kurtosis"] for c in selection["candidates"]]
    assert selection["selected_kurtosis"] == max(kurt_list)
    chosen = selection["candidates"][selection["selected_index"]]
    assert chosen["band_min_hz"] == selection["selected_band_min_hz"]
    # 冲击载波在 1000 Hz 附近，选中带应覆盖该共振区
    assert chosen["band_min_hz"] <= CARRIER <= chosen["band_max_hz"]
    assert "峭度最大" in selection["reason"]
    # 自动选带同样识别 BPFO
    assert body["dominant_fault"] == "bpfo"
    assert body["confidence"] == "high"


def test_auto_band_with_explicit_region(client):
    rid = make_record(client, "BEARING-AUTO2", fault_signal("bpfo"))
    r = envelope_diagnose(client, rid, auto_band={
        "region_min_hz": 500.0, "region_max_hz": 2000.0, "band_width_hz": 500.0})
    assert r.status_code == 201, r.text
    selection = r.json()["band_selection"]
    assert selection["mode"] == "auto"
    assert selection["region_min_hz"] == 500.0
    assert selection["candidate_count"] == 3  # 500-1000 / 1000-1500 / 1500-2000
    assert all(c["band_max_hz"] - c["band_min_hz"] == 500.0 for c in selection["candidates"])


# ---------- 正常样本不误报 ----------

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_clean_signal_not_flagged(client, seed):
    rid = make_record(client, f"BEARING-OK-{seed}", clean_signal(seed))
    r = envelope_diagnose(client, rid, carrier_band={"low_hz": 700.0, "high_hz": 1400.0})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["level"] == "normal"
    assert body["status"] == "normal"
    assert body["confidence"] == "none"
    assert body["dominant_fault"] is None
    for family in body["fault_families"]:
        assert family["level"] == "normal"
        assert family["confidence"] == "none"
        assert family["energy_ratio"] < body["attention_ratio"]
        assert family["fundamental_matched"] is False
        assert family["matched_harmonic_orders"] == []
    assert "正常" in body["conclusion"]


def test_weak_early_impulse_is_low_confidence(client):
    # 极弱早期冲击：可检出基频线索，但不足以给出高置信异常
    values = fault_signal("bpfo", amplitude=0.15, noise=0.3)
    rid = make_record(client, "BEARING-WEAK", values)
    body = envelope_diagnose(
        client, rid, carrier_band={"low_hz": 700.0, "high_hz": 1400.0}).json()
    assert body["confidence"] in ("low", "none", "medium")
    assert body["confidence"] != "high" or body["level"] != "critical"


# ---------- 缺少轴承几何 ----------

def test_missing_geometry_returns_unavailable(client):
    rid = make_record(client, "BEARING-NOGEOM", fault_signal("bpfo"))
    r = client.post("/api/v1/envelope/diagnoses", json={
        "record_id": rid, "rpm": RPM,
        "carrier_band": {"low_hz": 700.0, "high_hz": 1400.0}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["level"] == "unavailable"
    assert body["confidence"] == "none"
    assert set(body["missing_fields"]) == {
        "ball_count", "ball_diameter", "pitch_diameter", "contact_angle"}
    assert body["fault_families"] == []
    # 解调本身仍完成
    assert body["band_selection"]["mode"] == "manual"
    assert "人工判读" in body["conclusion"]


# ---------- 错误输入：统一错误结构且不落库 ----------

def test_carrier_band_beyond_nyquist_rejected_400(client):
    rid = make_record(client, "BEARING-ERR1", clean_signal())
    r = envelope_diagnose(client, rid, carrier_band={"low_hz": 4000.0, "high_hz": 6000.0})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "BAND_OUT_OF_RANGE"
    assert err["details"]["nyquist"] == pytest.approx(FS / 2)
    # 拒绝后不落库
    assert client.get("/api/v1/envelope/diagnoses",
                      params={"equipment_id": "BEARING-ERR1"}).json()["total"] == 0


def test_auto_region_beyond_nyquist_rejected_400(client):
    rid = make_record(client, "BEARING-ERR2", clean_signal())
    r = envelope_diagnose(client, rid, auto_band={
        "region_min_hz": 4000.0, "region_max_hz": 6000.0, "band_width_hz": 500.0})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAND_OUT_OF_RANGE"
    assert client.get("/api/v1/envelope/diagnoses",
                      params={"equipment_id": "BEARING-ERR2"}).json()["total"] == 0


def test_auto_band_too_wide_rejected_400(client):
    rid = make_record(client, "BEARING-ERR3", clean_signal())
    r = envelope_diagnose(client, rid, auto_band={
        "region_min_hz": 1000.0, "region_max_hz": 1500.0, "band_width_hz": 1000.0})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_AUTO_BAND_CONFIG"


def test_record_not_found_404(client):
    r = envelope_diagnose(client, 99999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RECORD_NOT_FOUND"


def test_non_uniform_sampling_rejected_400_and_not_persisted(client):
    fs = FS
    n = 256
    # 累积时间戳：对一个间隔加大扰动（0.5ms，远大于频谱诊断的 1µs 容差），
    # 该量级扰动不触发 /analysis 的波形异常规则，但必须被包络解调拒绝
    times = np.arange(n) / fs
    times[100:] += 0.0005
    samples = [{"t": float(times[i]),
                "a": math.sin(2 * math.pi * 50 * times[i])} for i in range(n)]
    r = client.post("/api/v1/analysis", json={
        "equipment_id": "BEARING-ERR4", "sampling_frequency": fs, "samples": samples})
    assert r.status_code == 201, r.text
    rid = r.json()["record_id"]
    diag = envelope_diagnose(client, rid, carrier_band={"low_hz": 700.0, "high_hz": 1400.0})
    assert diag.status_code == 400
    assert diag.json()["error"]["code"] == "NON_UNIFORM_SAMPLING"
    assert client.get("/api/v1/envelope/diagnoses",
                      params={"equipment_id": "BEARING-ERR4"}).json()["total"] == 0


def test_manual_and_auto_band_together_422(client):
    rid = make_record(client, "BEARING-ERR5", clean_signal())
    r = envelope_diagnose(client, rid,
                          carrier_band={"low_hz": 700.0, "high_hz": 1400.0},
                          auto_band={"band_width_hz": 500.0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_fault_type_filter_422(client):
    r = client.get("/api/v1/envelope/diagnoses", params={"fault_type": "unknown"})
    assert r.status_code == 422


# ---------- 持久化：详情 / 筛选 / 分页 ----------

def test_persistence_detail_and_filters(client):
    rid_ok = make_record(client, "BEARING-P1", clean_signal())
    ok = envelope_diagnose(client, rid_ok,
                           carrier_band={"low_hz": 700.0, "high_hz": 1400.0}).json()
    rid_bpfo = make_record(client, "BEARING-P1", fault_signal("bpfo"))
    bpfo = envelope_diagnose(client, rid_bpfo,
                             carrier_band={"low_hz": 700.0, "high_hz": 1400.0}).json()
    rid_bpfi = make_record(client, "BEARING-P2", fault_signal("bpfi", am=True))
    bpfi = envelope_diagnose(client, rid_bpfi,
                             carrier_band={"low_hz": 700.0, "high_hz": 1400.0}).json()

    # 详情接口回显完整结果（选带依据、谱峰匹配、几何参数）
    detail = client.get(f"/api/v1/envelope/diagnoses/{bpfo['envelope_id']}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["envelope_id"] == bpfo["envelope_id"]
    assert d["equipment_id"] == "BEARING-P1"
    assert d["source_record_id"] == rid_bpfo
    assert d["band_selection"]["mode"] == "manual"
    assert d["bearing_geometry"] == GEOM
    assert len(d["peak_matches"]) == len(bpfo["peak_matches"])
    assert d["fault_families"][0]["peak_matches"]
    assert d["conclusion"] == bpfo["conclusion"]

    # 按设备筛选
    listing = client.get("/api/v1/envelope/diagnoses", params={"equipment_id": "BEARING-P1"})
    assert listing.json()["total"] == 2
    ids_p1 = {it["envelope_id"] for it in listing.json()["items"]}
    assert ids_p1 == {ok["envelope_id"], bpfo["envelope_id"]}

    # 按主导故障类型筛选
    only_bpfi = client.get("/api/v1/envelope/diagnoses", params={"fault_type": "bpfi"})
    assert only_bpfi.json()["total"] == 1
    assert only_bpfi.json()["items"][0]["envelope_id"] == bpfi["envelope_id"]

    # 按置信等级筛选
    high = client.get("/api/v1/envelope/diagnoses", params={"confidence": "high"})
    high_ids = {it["envelope_id"] for it in high.json()["items"]}
    assert bpfo["envelope_id"] in high_ids and bpfi["envelope_id"] in high_ids
    assert ok["envelope_id"] not in high_ids
    none_conf = client.get("/api/v1/envelope/diagnoses",
                           params={"equipment_id": "BEARING-P1", "confidence": "none"})
    assert none_conf.json()["total"] == 1
    assert none_conf.json()["items"][0]["envelope_id"] == ok["envelope_id"]

    # 组合筛选 + 分页
    page1 = client.get("/api/v1/envelope/diagnoses",
                       params={"equipment_id": "BEARING-P1", "limit": 1, "offset": 0})
    page2 = client.get("/api/v1/envelope/diagnoses",
                       params={"equipment_id": "BEARING-P1", "limit": 1, "offset": 1})
    assert page1.json()["total"] == 2
    assert len(page1.json()["items"]) == 1 and len(page2.json()["items"]) == 1
    assert page1.json()["items"][0]["envelope_id"] != page2.json()["items"][0]["envelope_id"]


def test_time_range_filter(client):
    rid = make_record(client, "BEARING-TIME", fault_signal("bpfo"))
    envelope_diagnose(client, rid, carrier_band={"low_hz": 700.0, "high_hz": 1400.0})
    past = client.get("/api/v1/envelope/diagnoses", params={
        "equipment_id": "BEARING-TIME",
        "start_time": "2000-01-01T00:00:00Z",
        "end_time": "2000-01-02T00:00:00Z",
    })
    assert past.status_code == 200
    assert past.json()["total"] == 0
    now = client.get("/api/v1/envelope/diagnoses", params={
        "start_time": "2000-01-01T00:00:00Z"})
    assert now.json()["total"] >= 1
    bad = client.get("/api/v1/envelope/diagnoses", params={"start_time": "nonsense"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_TIME_RANGE"


def test_detail_not_found_404(client):
    r = client.get("/api/v1/envelope/diagnoses/99999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ENVELOPE_DIAGNOSIS_NOT_FOUND"
