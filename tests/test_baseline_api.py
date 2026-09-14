"""振动基线对比测试：转速分组统计、中位数/离散范围、偏差分级、版本启停与审计留痕。"""
import math
import os

import pytest

# 使用独立测试数据库，需在导入 app 前设置
DB_PATH = "/tmp/test_vibration_baseline.db"
os.environ["VIBRATION_DB"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.baseline import (  # noqa: E402
    DEFAULT_DEVIATION_THRESHOLDS,
    build_rpm_groups,
    compare_diagnosis,
    diagnosis_feature_values,
    find_matching_group,
)
from app.main import app  # noqa: E402


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


def make_record(client, equipment, n=1000, fs=1000.0, freq=50.0, amp=1.0):
    """上传一条纯正弦等间隔采样记录；纯正弦 RMS ≈ amp/√2、峰值因子 ≈ √2。"""
    samples = [
        {"t": i / fs, "a": amp * math.sin(2 * math.pi * freq * i / fs)}
        for i in range(n)
    ]
    r = client.post("/api/v1/analysis", json={
        "equipment_id": equipment, "sampling_frequency": fs, "samples": samples,
    })
    assert r.status_code == 201, r.text
    return r.json()["record_id"]


def diagnose(client, rid, rpm=1500.0, freq=50.0):
    r = client.post("/api/v1/spectrum/diagnoses", json={
        "record_id": rid, "rpm": rpm,
        "order_bands": [{"name": "1X", "order_min": 0.5, "order_max": 1.5}],
    })
    assert r.status_code == 201, r.text
    return r.json()


def seed_identical(client, equipment, rpm, freq, amp=1.0, count=3):
    """在同一转速下生成 count 条指标近似一致的诊断。"""
    ids = []
    for _ in range(count):
        rid = make_record(client, equipment, freq=freq, amp=amp)
        ids.append(diagnose(client, rid, rpm=rpm, freq=freq)["diagnosis_id"])
    return ids


def build(client, equipment, **overrides):
    return client.post(f"/api/v1/baselines/{equipment}", json=overrides or {})


def rms_of(amp):
    return amp / math.sqrt(2)


# ---------- 基线构建：转速分组与统计量 ----------

def test_baseline_build_bins_grouping_median_and_dispersion(client):
    ids_lo = seed_identical(client, "PUMP-BL1", rpm=1500.0, freq=50.0)
    ids_hi = seed_identical(client, "PUMP-BL1", rpm=2000.0, freq=50.0)
    r = build(client, "PUMP-BL1", rpm_bins=[0, 1700, 3000],
              operator="li.si", note="检修后基线")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["status"] == "active"
    assert body["grouping_type"] == "bins"
    assert body["source_count"] == 6
    assert body["sample_count_total"] == 6
    assert body["created_by"] == "li.si"
    assert body["note"] == "检修后基线"
    assert body["effective_from"]
    assert set(body["source_diagnosis_ids"]) == set(ids_lo + ids_hi)
    assert body["skipped_groups"] == []

    groups = body["groups"]
    assert [g["group_index"] for g in groups] == [0, 1]
    g0, g1 = groups
    assert g0["rpm_min"] == 0 and g0["rpm_max"] == 1700
    assert g1["rpm_min"] == 1700 and g1["rpm_max"] == 3000
    assert g0["sample_count"] == 3 and g1["sample_count"] == 3
    assert set(g0["source_diagnosis_ids"]) == set(ids_lo)

    # 纯正弦：RMS ≈ amp/√2，三条一致 → 中位数=min=max
    rms_stat = g0["metrics"]["rms"]
    assert rms_stat["median"] == pytest.approx(rms_of(1.0), rel=1e-3)
    assert rms_stat["min"] == pytest.approx(rms_stat["median"], abs=1e-9)
    assert rms_stat["max"] == pytest.approx(rms_stat["median"], abs=1e-9)
    # 峰值因子 ≈ √2
    assert g0["metrics"]["crest_factor"]["median"] == pytest.approx(math.sqrt(2), rel=1e-3)
    # 1500rpm 转频 25Hz，主峰 50Hz → 阶次 2；2000rpm 转频 33.33Hz → 阶次 1.5
    assert g0["metrics"]["peak_order"]["median"] == pytest.approx(2.0, abs=0.02)
    assert g1["metrics"]["peak_order"]["median"] == pytest.approx(1.5, abs=0.02)
    assert g0["metrics"]["main_peak_frequency_hz"]["median"] == pytest.approx(50.0, abs=1.0)


def test_baseline_exact_grouping_when_no_bins(client):
    seed_identical(client, "PUMP-BL2", rpm=1500.0, freq=50.0)
    seed_identical(client, "PUMP-BL2", rpm=1501.0, freq=50.0)
    r = build(client, "PUMP-BL2")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grouping_type"] == "exact"
    assert body["rpm_bins"] is None
    groups = body["groups"]
    assert {g["rpm_min"] for g in groups} == {1500.0, 1501.0}
    assert all(g["rpm_min"] == g["rpm_max"] for g in groups)


def test_baseline_median_and_range_with_variation(client):
    # 同一分组内幅值 0.8 / 1.0 / 1.2 → 中位数 1.0，范围即最小最大
    for amp in (0.8, 1.0, 1.2):
        rid = make_record(client, "PUMP-BL3", freq=50.0, amp=amp)
        diagnose(client, rid, rpm=1500.0)
    body = build(client, "PUMP-BL3").json()
    stat = body["groups"][0]["metrics"]["rms"]
    assert stat["median"] == pytest.approx(rms_of(1.0), rel=1e-3)
    assert stat["min"] == pytest.approx(rms_of(0.8), rel=1e-3)
    assert stat["max"] == pytest.approx(rms_of(1.2), rel=1e-3)


def test_baseline_skipped_groups_reported(client):
    seed_identical(client, "PUMP-BL4", rpm=1500.0, freq=50.0)          # 3 条，达标
    seed_identical(client, "PUMP-BL4", rpm=2000.0, freq=50.0, count=2)  # 2 条，不足
    r = build(client, "PUMP-BL4", rpm_bins=[0, 1700, 3000])
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["sample_count"] == 3
    skipped = body["skipped_groups"]
    assert len(skipped) == 1
    assert skipped[0]["rpm_min"] == 1700 and skipped[0]["rpm_max"] == 3000
    assert skipped[0]["sample_count"] == 2 and skipped[0]["required"] == 3
    # 被跳过分组不计入总体样本数
    assert body["sample_count_total"] == 3


def test_baseline_insufficient_samples_rejected(client):
    seed_identical(client, "PUMP-BL5", rpm=1500.0, freq=50.0, count=2)
    r = build(client, "PUMP-BL5")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_BASELINE_SAMPLES"
    assert err["details"]["min_samples_per_group"] == 3
    assert err["details"]["skipped_groups"][0]["sample_count"] == 2


def test_baseline_rpm_out_of_bins_rejected(client):
    seed_identical(client, "PUMP-BL6", rpm=2500.0, freq=50.0)
    r = build(client, "PUMP-BL6", rpm_bins=[0, 1700, 2400])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "RPM_OUT_OF_BINS"
    assert err["details"]["rpm"] == 2500.0
    # 构建失败不产生基线
    assert client.get("/api/v1/baselines", params={"equipment_id": "PUMP-BL6"}).json()["total"] == 0


def test_baseline_no_diagnosis_rejected(client):
    r = build(client, "PUMP-BL7")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INSUFFICIENT_BASELINE_SAMPLES"


def test_baseline_unknown_source_id_404(client):
    seed_identical(client, "PUMP-BL8", rpm=1500.0, freq=50.0)
    r = build(client, "PUMP-BL8", source_diagnosis_ids=[1, 999])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"
    assert 999 in r.json()["error"]["details"]["unknown_diagnosis_ids"]


def test_baseline_source_id_from_other_equipment_404(client):
    seed_identical(client, "PUMP-BL9A", rpm=1500.0, freq=50.0)
    seed_identical(client, "PUMP-BL9B", rpm=1500.0, freq=50.0)
    other = client.get("/api/v1/spectrum/diagnoses",
                       params={"equipment_id": "PUMP-BL9B"}).json()["items"]
    other_id = other[0]["diagnosis_id"]
    r = build(client, "PUMP-BL9A",
              source_diagnosis_ids=[d["diagnosis_id"] for d in
                                    client.get("/api/v1/spectrum/diagnoses",
                                               params={"equipment_id": "PUMP-BL9A"}).json()["items"]] + [other_id])
    assert r.status_code == 404
    assert other_id in r.json()["error"]["details"]["unknown_diagnosis_ids"]


def test_baseline_effective_from_persisted(client):
    seed_identical(client, "PUMP-BL10", rpm=1500.0, freq=50.0)
    r = build(client, "PUMP-BL10", effective_from="2026-09-01T08:00:00+00:00")
    assert r.status_code == 201
    assert r.json()["effective_from"] == "2026-09-01 08:00:00"


def test_baseline_create_with_time_filter(client):
    ids = seed_identical(client, "PUMP-BL11", rpm=1500.0, freq=50.0)
    # 用未来时间窗过滤 → 无来源
    r = build(client, "PUMP-BL11", start_time="2099-01-01T00:00:00Z")
    assert r.status_code == 400
    # 宽时间窗覆盖全部
    r2 = build(client, "PUMP-BL11",
               start_time="2000-01-01T00:00:00Z", end_time="2099-01-01T00:00:00Z")
    assert r2.status_code == 201
    assert r2.json()["source_count"] == 3
    assert set(r2.json()["source_diagnosis_ids"]) == set(ids)


# ---------- 版本：新基线 supersede 旧基线 ----------

def test_baseline_versions_auto_increment_and_supersede(client):
    seed_identical(client, "PUMP-BL12", rpm=1500.0, freq=50.0)
    v1 = build(client, "PUMP-BL12").json()
    v2 = build(client, "PUMP-BL12", note="复检更新").json()
    assert v1["version"] == 1 and v2["version"] == 2
    detail1 = client.get(f"/api/v1/baselines/{v1['baseline_id']}").json()
    assert detail1["status"] == "superseded"
    assert v2["status"] == "active"
    active = client.get("/api/v1/baselines",
                        params={"equipment_id": "PUMP-BL12", "status": "active"}).json()
    assert active["total"] == 1 and active["items"][0]["version"] == 2


# ---------- 对比：偏差 / 变化率 / 等级 ----------

def build_steady_baseline(client, equipment="PUMP-CMP"):
    seed_identical(client, equipment, rpm=1500.0, freq=50.0, amp=1.0)
    return build(client, equipment).json()


def test_compare_critical_on_rms_increase(client):
    bl = build_steady_baseline(client)
    rid = make_record(client, "PUMP-CMP", freq=50.0, amp=2.0)  # RMS 翻倍 → +100%
    did = diagnose(client, rid, rpm=1500.0, freq=50.0)["diagnosis_id"]
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                    json={"diagnosis_id": did})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall_level"] == "critical"
    rms_cmp = next(m for m in body["metrics"] if m["metric"] == "rms")
    assert rms_cmp["level"] == "critical"
    assert rms_cmp["change_rate"] == pytest.approx(1.0, abs=0.01)
    assert rms_cmp["deviation"] > 0
    assert rms_cmp["within_dispersion"] is False
    assert "上升" in rms_cmp["message"] and "严重" in rms_cmp["message"]


def test_compare_attention_within_custom_threshold(client):
    bl = build_steady_baseline(client)
    rid = make_record(client, "PUMP-CMP", freq=50.0, amp=1.1)  # RMS +10%
    did = diagnose(client, rid, rpm=1500.0)["diagnosis_id"]
    # 默认关注线 15% → normal
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                    json={"diagnosis_id": did})
    body = r.json()
    assert next(m for m in body["metrics"] if m["metric"] == "rms")["level"] == "normal"
    # 收紧关注线到 5% → attention
    r2 = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare", json={
        "diagnosis_id": did,
        "deviation_thresholds": {"rms_attention": 0.05, "rms_critical": 0.5},
    })
    assert r2.status_code == 200, r2.text
    rms_cmp = next(m for m in r2.json()["metrics"] if m["metric"] == "rms")
    assert rms_cmp["level"] == "attention"
    assert rms_cmp["change_rate"] == pytest.approx(0.1, abs=0.01)


def test_compare_main_peak_frequency_shift(client):
    bl = build_steady_baseline(client)
    # 主峰从 50Hz 漂到 55Hz：频率 +10% → 默认严重线
    rid = make_record(client, "PUMP-CMP", freq=55.0, amp=1.0)
    did = diagnose(client, rid, rpm=1500.0, freq=55.0)["diagnosis_id"]
    body = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                       json={"diagnosis_id": did}).json()
    freq_cmp = next(m for m in body["metrics"] if m["metric"] == "main_peak_frequency_hz")
    assert freq_cmp["level"] == "critical"
    assert freq_cmp["change_rate"] == pytest.approx(0.1, abs=0.01)
    assert body["overall_level"] == "critical"


def test_compare_rpm_group_selection(client):
    seed_identical(client, "PUMP-CMP2", rpm=1500.0, freq=50.0)
    seed_identical(client, "PUMP-CMP2", rpm=2000.0, freq=50.0)
    bl = build(client, "PUMP-CMP2", rpm_bins=[0, 1700, 3000]).json()
    rid = make_record(client, "PUMP-CMP2", freq=50.0, amp=1.5)
    did = diagnose(client, rid, rpm=2000.0)["diagnosis_id"]
    body = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                       json={"diagnosis_id": did}).json()
    assert body["group_index"] == 1
    assert body["rpm_min"] == 1700 and body["rpm_max"] == 3000
    assert body["rpm"] == 2000.0


def test_compare_rpm_not_covered_400(client):
    bl = build_steady_baseline(client, "PUMP-CMP3")
    rid = make_record(client, "PUMP-CMP3", freq=50.0)
    did = diagnose(client, rid, rpm=1900.0)["diagnosis_id"]
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                    json={"diagnosis_id": did})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BASELINE_RPM_NOT_COVERED"


def test_compare_equipment_mismatch_400(client):
    bl = build_steady_baseline(client, "PUMP-CMP4A")
    rid = make_record(client, "PUMP-CMP4B", freq=50.0)
    did = diagnose(client, rid, rpm=1500.0)["diagnosis_id"]
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                    json={"diagnosis_id": did})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BASELINE_EQUIPMENT_MISMATCH"


def test_compare_unknown_diagnosis_404(client):
    bl = build_steady_baseline(client)
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                    json={"diagnosis_id": 9999})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"


# ---------- 启用 / 停用 ----------

def test_deactivate_and_activate_with_audit(client):
    bl = build_steady_baseline(client, "PUMP-SW1")
    bid = bl["baseline_id"]
    r = client.post(f"/api/v1/baselines/{bid}/deactivate",
                    json={"operator": "wang.wu", "note": "大修停用"})
    assert r.status_code == 200
    assert r.json()["status"] == "inactive"

    dup = client.post(f"/api/v1/baselines/{bid}/deactivate", json={"operator": "wang.wu"})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "BASELINE_ALREADY_INACTIVE"

    r2 = client.post(f"/api/v1/baselines/{bid}/activate",
                     json={"operator": "wang.wu", "note": "复检后重新启用"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "active"

    dup2 = client.post(f"/api/v1/baselines/{bid}/activate", json={"operator": "wang.wu"})
    assert dup2.status_code == 409
    assert dup2.json()["error"]["code"] == "BASELINE_ALREADY_ACTIVE"

    detail = client.get(f"/api/v1/baselines/{bid}").json()
    types = [e["event_type"] for e in detail["events"]]
    assert types == ["created", "deactivated", "activated"]
    created = detail["events"][0]
    assert created["operator"] == "system"
    assert created["details"]["group_count"] == 1
    assert created["details"]["source_count"] == 3
    deact = detail["events"][1]
    assert deact["operator"] == "wang.wu" and deact["note"] == "大修停用"


def test_activate_supersedes_other_active_baseline(client):
    seed_identical(client, "PUMP-SW2", rpm=1500.0, freq=50.0)
    v1 = build(client, "PUMP-SW2").json()
    v2 = build(client, "PUMP-SW2").json()
    assert client.get(f"/api/v1/baselines/{v1['baseline_id']}").json()["status"] == "superseded"
    # 重新启用 v1 → v2 被置为 superseded
    r = client.post(f"/api/v1/baselines/{v1['baseline_id']}/activate",
                    json={"operator": "zhao.liu"})
    assert r.status_code == 200
    assert client.get(f"/api/v1/baselines/{v1['baseline_id']}").json()["status"] == "active"
    assert client.get(f"/api/v1/baselines/{v2['baseline_id']}").json()["status"] == "superseded"


def test_status_change_unknown_baseline_404(client):
    r = client.post("/api/v1/baselines/999/deactivate", json={"operator": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BASELINE_NOT_FOUND"


def test_compare_inactive_baseline_still_allowed_directly(client):
    bl = build_steady_baseline(client, "PUMP-SW3")
    client.post(f"/api/v1/baselines/{bl['baseline_id']}/deactivate", json={"operator": "x"})
    rid = make_record(client, "PUMP-SW3", freq=50.0, amp=1.0)
    did = diagnose(client, rid, rpm=1500.0)["diagnosis_id"]
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare",
                    json={"diagnosis_id": did})
    assert r.status_code == 200
    assert r.json()["baseline_status"] == "inactive"


def test_active_baseline_convenience_endpoint(client):
    bl = build_steady_baseline(client, "PUMP-SW4")
    rid = make_record(client, "PUMP-SW4", freq=50.0, amp=2.0)
    did = diagnose(client, rid, rpm=1500.0)["diagnosis_id"]
    r = client.post("/api/v1/equipment/PUMP-SW4/baseline-compare",
                    json={"diagnosis_id": did})
    assert r.status_code == 200
    assert r.json()["baseline_id"] == bl["baseline_id"]
    assert r.json()["overall_level"] == "critical"

    client.post(f"/api/v1/baselines/{bl['baseline_id']}/deactivate", json={"operator": "x"})
    r2 = client.post("/api/v1/equipment/PUMP-SW4/baseline-compare",
                     json={"diagnosis_id": did})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "NO_ACTIVE_BASELINE"


# ---------- 查询与持久化 ----------

def test_list_baselines_pagination_and_filter(client):
    seed_identical(client, "PUMP-LS1", rpm=1500.0, freq=50.0)
    seed_identical(client, "PUMP-LS2", rpm=1500.0, freq=50.0)
    build(client, "PUMP-LS1")
    build(client, "PUMP-LS1")
    build(client, "PUMP-LS2")
    page = client.get("/api/v1/baselines", params={"equipment_id": "PUMP-LS1"})
    assert page.json()["total"] == 2
    assert [b["version"] for b in page.json()["items"]] == [2, 1]
    all_page = client.get("/api/v1/baselines", params={"limit": 1, "offset": 1})
    assert all_page.json()["total"] == 3
    assert len(all_page.json()["items"]) == 1
    # 列表项包含统计分组
    assert all_page.json()["items"][0]["groups"]


def test_baseline_detail_not_found_404(client):
    r = client.get("/api/v1/baselines/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BASELINE_NOT_FOUND"


def test_baseline_audit_persisted_in_create(client):
    bl = build_steady_baseline(client, "PUMP-AU1")
    detail = client.get(f"/api/v1/baselines/{bl['baseline_id']}").json()
    events = detail["events"]
    assert len(events) == 1 and events[0]["event_type"] == "created"
    assert events[0]["details"]["grouping_type"] == "exact"
    assert events[0]["created_at"]


# ---------- 请求校验（422 / 400） ----------

def test_invalid_deviation_thresholds_422(client):
    seed_identical(client, "PUMP-V1", rpm=1500.0, freq=50.0)
    r = build(client, "PUMP-V1", deviation_thresholds={
        "rms_attention": 0.5, "rms_critical": 0.2})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_non_increasing_rpm_bins_422(client):
    seed_identical(client, "PUMP-V2", rpm=1500.0, freq=50.0)
    r = build(client, "PUMP-V2", rpm_bins=[3000, 1700, 0])
    assert r.status_code == 422


def test_baseline_time_range_swapped_422(client):
    seed_identical(client, "PUMP-V3", rpm=1500.0, freq=50.0)
    r = build(client, "PUMP-V3",
              start_time="2026-09-10T00:00:00Z", end_time="2026-09-01T00:00:00Z")
    assert r.status_code == 422


def test_baseline_compare_bad_thresholds_422(client):
    bl = build_steady_baseline(client, "PUMP-V4")
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/compare", json={
        "diagnosis_id": 1,
        "deviation_thresholds": {"crest_factor_attention": 0.9,
                                 "crest_factor_critical": 0.2},
    })
    assert r.status_code == 422


def test_status_request_requires_operator_422(client):
    bl = build_steady_baseline(client, "PUMP-V5")
    r = client.post(f"/api/v1/baselines/{bl['baseline_id']}/deactivate", json={})
    assert r.status_code == 422


# ---------- 核心计算单元测试 ----------

def test_compare_core_zero_median_non_crash():
    group = {"group_index": 0, "rpm_min": 1500.0, "rpm_max": 1500.0,
             "metrics": {
                 "rms": {"median": 0.0, "min": 0.0, "max": 0.0},
                 "crest_factor": {"median": 1.4, "min": 1.4, "max": 1.4},
                 "main_peak_frequency_hz": {"median": 50.0, "min": 50.0, "max": 50.0},
                 "peak_order": {"median": 2.0, "min": 2.0, "max": 2.0},
             }}
    diag = {"main_peak": {"frequency_hz": 50.0}, "order": {"peak_order": 2.0}}
    record = {"rms": 0.5, "crest_factor": 1.4}
    out = compare_diagnosis(diag, record, group, DEFAULT_DEVIATION_THRESHOLDS)
    rms_cmp = next(m for m in out["metrics"] if m["metric"] == "rms")
    assert rms_cmp["change_rate"] is None
    assert rms_cmp["level"] == "normal"
    assert "无法" in rms_cmp["message"]


def test_find_matching_group_exact_and_range():
    groups = [
        {"group_index": 0, "rpm_min": 0.0, "rpm_max": 1700.0},
        {"group_index": 1, "rpm_min": 1700.0, "rpm_max": 3000.0},
    ]
    assert find_matching_group(1699.9, groups)["group_index"] == 0
    assert find_matching_group(1700.0, groups)["group_index"] == 1


def test_build_groups_exact_sorts_by_rpm():
    entries = [
        {"diagnosis_id": i, "rpm": rpm,
         "features": dict.fromkeys(
             ("rms", "crest_factor", "main_peak_frequency_hz", "peak_order"), 1.0)}
        for i, rpm in enumerate([2000.0, 1500.0, 1500.0, 2000.0, 1500.0, 2000.0], start=1)
    ]
    groups, skipped = build_rpm_groups(entries, None, 3)
    assert skipped == []
    assert [g["rpm_min"] for g in groups] == [1500.0, 2000.0]
    assert groups[0]["group_index"] == 0 and groups[1]["group_index"] == 1


def test_diagnosis_feature_values_extraction():
    diag = {"main_peak": {"frequency_hz": 51.5}, "order": {"peak_order": 2.06}}
    record = {"rms": 0.8, "crest_factor": 1.42}
    fv = diagnosis_feature_values(diag, record)
    assert fv == {"rms": 0.8, "crest_factor": 1.42,
                  "main_peak_frequency_hz": 51.5, "peak_order": 2.06}
