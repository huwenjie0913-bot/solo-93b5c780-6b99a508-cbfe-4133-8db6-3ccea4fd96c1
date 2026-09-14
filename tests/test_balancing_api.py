"""影响系数法动平衡测试：单 / 双平面求解、超定最小二乘、离散孔位配重、
约束不达标原因、维度 / 小响应 / 病态拒绝与持久化筛选。"""
import math
import os

import numpy as np
import pytest

# 使用独立测试数据库，需在导入 app 前设置
DB_PATH = "/tmp/test_vibration_balancing.db"
os.environ["VIBRATION_DB"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.balancing import (  # noqa: E402
    DEFAULT_MAX_CONDITION_NUMBER,
    DEFAULT_MIN_RESPONSE_RATIO,
    polar_to_complex,
    solve_balancing,
)


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


HOLES_30 = [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330]


def polar(amplitude, phase_deg):
    return {"amplitude": float(amplitude), "phase_deg": float(phase_deg % 360)}


def from_complex(v):
    return polar(abs(v), np.angle(v, deg=True))


def single_plane_request(**over):
    """解析单平面模型：α=0.002∠60°，真实配平量 500 g·mm∠200°（=5g@r100）。"""
    alpha = polar_to_complex(0.002, 60.0)
    u_true = polar_to_complex(500.0, 200.0)
    v0 = -alpha * u_true
    trial = polar_to_complex(200.0 * 100.0, 90.0)
    req = {
        "equipment_id": "FAN-1",
        "speed_rpm": 1500.0,
        "planes": [{"name": "叶轮", "radius_mm": 100.0}],
        "measurement_points": [{"name": "DE-H", "unit": "mm/s"}],
        "initial_vibration": [from_complex(v0)],
        "trial_runs": [{
            "plane_index": 0, "trial_mass_g": 200.0, "radius_mm": 100.0,
            "angle_deg": 90.0, "response": [from_complex(v0 + alpha * trial)],
        }],
        "constraints": [],
    }
    req.update(over)
    return req


def two_plane_request(**over):
    """解析双平面模型：2×2 良态影响矩阵，真实配平量 400∠120° / 600∠250° g·mm。"""
    A = np.array([
        [polar_to_complex(0.003, 40), polar_to_complex(0.001, 100)],
        [polar_to_complex(0.0011, 150), polar_to_complex(0.0028, 300)],
    ])
    u = np.array([polar_to_complex(400.0, 120.0), polar_to_complex(600.0, 250.0)])
    v0 = -A @ u
    t1 = polar_to_complex(150.0 * 120.0, 30.0)
    t2 = polar_to_complex(180.0 * 90.0, 300.0)
    req = {
        "equipment_id": "FAN-2",
        "speed_rpm": 3000.0,
        "planes": [{"name": "DE", "radius_mm": 120.0}, {"name": "NDE", "radius_mm": 90.0}],
        "measurement_points": [{"name": "DE-H"}, {"name": "NDE-H"}],
        "initial_vibration": [from_complex(v0[0]), from_complex(v0[1])],
        "trial_runs": [
            {"plane_index": 0, "trial_mass_g": 150.0, "radius_mm": 120.0, "angle_deg": 30.0,
             "response": [from_complex(v0[0] + A[0, 0] * t1), from_complex(v0[1] + A[1, 0] * t1)]},
            {"plane_index": 1, "trial_mass_g": 180.0, "radius_mm": 90.0, "angle_deg": 300.0,
             "response": [from_complex(v0[0] + A[0, 1] * t2), from_complex(v0[1] + A[1, 1] * t2)]},
        ],
        "constraints": [],
    }
    req.update(over)
    return req


# ---------- 连续解 ----------

def test_single_plane_recovers_known_weight():
    res = solve_balancing(single_plane_request())
    c = res["continuous_solution"]
    assert c["solver"] == "exact"
    p = c["planes"][0]
    assert p["correction_mass_g"] == pytest.approx(5.0, abs=1e-6)
    assert p["correction_angle_deg"] == pytest.approx(200.0, abs=1e-6)
    assert c["max_residual_amplitude"] == pytest.approx(0.0, abs=1e-10)
    assert res["plane_count"] == 1 and res["point_count"] == 1
    assert res["diagnostics"]["condition_number"] == pytest.approx(1.0)
    # 影响系数应还原为 0.002∠60°
    alpha = res["diagnostics"]["influence_matrix"]["rows"][0]["coefficients"][0]
    assert alpha["amplitude"] == pytest.approx(0.002, abs=1e-9)
    assert alpha["phase_deg"] == pytest.approx(60.0, abs=1e-6)


def test_two_plane_recovers_known_weights():
    res = solve_balancing(two_plane_request())
    c = res["continuous_solution"]
    de, nde = c["planes"]
    assert de["correction_mass_g"] == pytest.approx(400.0 / 120.0, abs=1e-6)
    assert de["correction_angle_deg"] == pytest.approx(120.0, abs=1e-6)
    assert nde["correction_mass_g"] == pytest.approx(600.0 / 90.0, abs=1e-6)
    assert nde["correction_angle_deg"] == pytest.approx(250.0, abs=1e-6)
    assert c["max_residual_amplitude"] == pytest.approx(0.0, abs=1e-10)
    assert res["plane_count"] == 2
    cond = res["diagnostics"]["condition_number"]
    assert 1.0 < cond < DEFAULT_MAX_CONDITION_NUMBER


def test_overdetermined_single_plane_uses_least_squares():
    """单平面 3 个一致测点 → 超定最小二乘，残振为 0 且 solver 标记 least_squares。"""
    alpha = polar_to_complex(0.002, 60.0)
    u = polar_to_complex(400.0, 150.0)
    v0 = -alpha * u
    trial = polar_to_complex(150.0 * 100.0, 30.0)
    scales = [1.0, 0.9, 1.1]
    req = {
        "equipment_id": "FAN-3",
        "planes": [{"radius_mm": 100.0}],
        "measurement_points": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        "initial_vibration": [from_complex(v0 * s) for s in scales],
        "trial_runs": [{
            "plane_index": 0, "trial_mass_g": 150.0, "radius_mm": 100.0, "angle_deg": 30.0,
            "response": [from_complex(v0 * s + alpha * s * trial) for s in scales],
        }],
        "constraints": [],
    }
    res = solve_balancing(req)
    c = res["continuous_solution"]
    assert c["solver"] == "least_squares"
    assert c["planes"][0]["correction_mass_g"] == pytest.approx(4.0, abs=1e-6)
    assert c["planes"][0]["correction_angle_deg"] == pytest.approx(150.0, abs=1e-6)
    assert c["max_residual_amplitude"] == pytest.approx(0.0, abs=1e-10)


def test_target_residual_flag():
    req = single_plane_request(target_residual_amplitude=0.05)
    res = solve_balancing(req)
    assert res["continuous_solution"]["target_achieved"] is True
    assert res["continuous_solution"]["predicted_residual"][0]["within_target"] is True

    # 解析可完全配平的模型残振≈0，极小目标也能达到
    req = single_plane_request(target_residual_amplitude=1e-9)
    res = solve_balancing(req)
    assert res["continuous_solution"]["max_residual_amplitude"] == pytest.approx(0.0, abs=1e-12)
    assert res["continuous_solution"]["target_achieved"] is True

    # 超定不一致系统：两测点影响系数与初始振动不成比例，最小二乘最优仍有非零残振
    a0, a1 = polar_to_complex(0.002, 60.0), polar_to_complex(0.0011, 300.0)
    v0 = np.array([polar_to_complex(1.0, 80.0), polar_to_complex(0.8, 200.0)])
    t = polar_to_complex(200.0 * 100.0, 90.0)
    inconsistent = {
        "equipment_id": "FAN-LS",
        "planes": [{"radius_mm": 100.0}],
        "measurement_points": [{"name": "A"}, {"name": "B"}],
        "initial_vibration": [from_complex(v0[0]), from_complex(v0[1])],
        "trial_runs": [{
            "plane_index": 0, "trial_mass_g": 200.0, "radius_mm": 100.0, "angle_deg": 90.0,
            "response": [from_complex(v0[0] + a0 * t), from_complex(v0[1] + a1 * t)],
        }],
        "constraints": [],
    }
    res = solve_balancing(inconsistent)
    floor = res["continuous_solution"]["max_residual_amplitude"]
    assert floor > 1e-6

    res = solve_balancing({**inconsistent, "target_residual_amplitude": floor * 0.5})
    assert res["continuous_solution"]["target_achieved"] is False
    assert any(p["within_target"] is False
               for p in res["continuous_solution"]["predicted_residual"])

    res = solve_balancing({**inconsistent, "target_residual_amplitude": floor * 2.0})
    assert res["continuous_solution"]["target_achieved"] is True
    assert all(p["within_target"] is True
               for p in res["continuous_solution"]["predicted_residual"])


# ---------- 离散配重 ----------

def test_discrete_plan_single_plane_meets_target():
    req = single_plane_request(
        target_residual_amplitude=0.05,
        constraints=[{
            "plane_index": 0, "max_correction_mass_g": 20.0,
            "hole_angles_deg": HOLES_30, "unit_weight_mass_g": 1.0,
            "install_radius_mm": 100.0,
        }],
    )
    d = solve_balancing(req)["discrete_solution"]
    assert d["status"] == "available"
    assert d["target_achieved"] is True
    assert d["max_residual_amplitude"] <= 0.05
    # 每块 1g，总块数与总质量一致，且不超最大配重
    plane = d["planes"][0]
    assert plane["total_pieces"] * 1.0 == plane["total_mass_g"]
    assert plane["total_mass_g"] <= 20.0
    assert all(p["count"] >= 1 for p in plane["pieces"])
    assert d["combinations_evaluated"] >= 1


def test_discrete_granularity_failure_marked():
    """孔位只有 90/270、单块 10g：无法合成 200° 方向配重，目标不可达要明确标记。"""
    req = single_plane_request(
        target_residual_amplitude=0.01,
        constraints=[{
            "plane_index": 0, "max_correction_mass_g": 20.0,
            "hole_angles_deg": [90, 270], "unit_weight_mass_g": 10.0,
            "install_radius_mm": 100.0,
        }],
    )
    d = solve_balancing(req)["discrete_solution"]
    assert d["status"] == "available"
    assert d["target_achieved"] is False
    assert "DISCRETE_GRANULARITY_INSUFFICIENT" in d["reasons"]
    # 连续解本身可达，故不是连续解的问题
    assert "CONTINUOUS_TARGET_UNREACHABLE" not in d["reasons"]


def test_discrete_exceeds_max_mass_marked():
    """连续解需 5g 但限制 2g：离散方案封顶 2g 并标记 CONTINUOUS_EXCEEDS_MAX_MASS。"""
    req = single_plane_request(
        target_residual_amplitude=0.01,
        constraints=[{
            "plane_index": 0, "max_correction_mass_g": 2.0,
            "hole_angles_deg": HOLES_30, "unit_weight_mass_g": 0.5,
            "install_radius_mm": 100.0,
        }],
    )
    res = solve_balancing(req)
    c, d = res["continuous_solution"], res["discrete_solution"]
    assert c["planes"][0]["within_max_mass"] is False
    assert d["target_achieved"] is False
    assert "CONTINUOUS_EXCEEDS_MAX_MASS" in d["reasons"]
    assert d["reason_details"]["continuous_exceeds_max_mass_planes"][0]["plane_index"] == 0
    assert d["planes"][0]["total_mass_g"] <= 2.0 + 1e-9


def test_discrete_unavailable_without_hole_or_unit_spec():
    req = single_plane_request(
        target_residual_amplitude=0.01,
        constraints=[{"plane_index": 0, "max_correction_mass_g": 20.0}],
    )
    d = solve_balancing(req)["discrete_solution"]
    assert d["status"] == "unavailable"
    assert d["target_achieved"] is None
    assert d["reasons"] == ["DISCRETE_UNAVAILABLE"]
    missing = d["reason_details"]["planes_missing_constraints"][0]
    assert missing["plane_index"] == 0
    assert set(missing["missing_constraints"]) == {"hole_angles_deg", "unit_weight_mass_g"}
    # 缺约束平面在分节中说明
    assert d["planes"][0]["status"] == "unavailable"


# ---------- 拒绝计算 ----------

def test_reject_dimension_mismatch_response_count():
    req = single_plane_request()
    req["trial_runs"][0]["response"] = []
    with pytest.raises(Exception) as ei:
        solve_balancing(req)
    assert ei.value.code == "DIMENSION_MISMATCH"
    assert ei.value.details["field"] == "trial_runs[0].response"
    assert ei.value.details["expected"] == 1 and ei.value.details["actual"] == 0


def test_reject_dimension_mismatch_initial_count():
    req = single_plane_request()
    req["initial_vibration"] = []
    with pytest.raises(Exception) as ei:
        solve_balancing(req)
    assert ei.value.code == "DIMENSION_MISMATCH"
    assert ei.value.details["field"] == "initial_vibration"


def test_reject_trial_run_count_mismatch():
    req = two_plane_request()
    req["trial_runs"] = [req["trial_runs"][0]]
    with pytest.raises(Exception) as ei:
        solve_balancing(req)
    assert ei.value.code == "DIMENSION_MISMATCH"
    assert ei.value.details["field"] == "trial_runs"
    assert ei.value.details["expected"] == 2


def test_reject_response_change_too_small():
    req = single_plane_request()
    # 试重后响应与初始几乎一致（变化 0.5% < 默认 2%），同相位
    v0 = req["initial_vibration"][0]
    req["trial_runs"][0]["response"] = [{
        "amplitude": v0["amplitude"] * 1.005, "phase_deg": v0["phase_deg"],
    }]
    with pytest.raises(Exception) as ei:
        solve_balancing(req)
    assert ei.value.code == "RESPONSE_CHANGE_TOO_SMALL"
    detail = ei.value.details["insufficient_changes"][0]
    assert detail["change_ratio"] < DEFAULT_MIN_RESPONSE_RATIO
    assert "field" in detail and detail["point_index"] == 0


def test_reject_ill_conditioned_matrix():
    a1, a2 = polar_to_complex(0.003, 50), polar_to_complex(0.0030001, 50.0001)
    A = np.array([
        [a1, a2],
        [polar_to_complex(0.002, 200), polar_to_complex(0.0020001, 200.0001)],
    ])
    v0 = np.array([10 + 0j, 8 + 2j])
    t1 = polar_to_complex(100.0 * 100.0, 0.0)
    t2 = polar_to_complex(100.0 * 90.0, 90.0)
    req = {
        "equipment_id": "F",
        "planes": [{"radius_mm": 100.0}, {"radius_mm": 90.0}],
        "measurement_points": [{"name": "A"}, {"name": "B"}],
        "initial_vibration": [from_complex(v0[0]), from_complex(v0[1])],
        "trial_runs": [
            {"plane_index": 0, "trial_mass_g": 100.0, "radius_mm": 100.0, "angle_deg": 0.0,
             "response": [from_complex(v0[0] + A[0, 0] * t1), from_complex(v0[1] + A[1, 0] * t1)]},
            {"plane_index": 1, "trial_mass_g": 100.0, "radius_mm": 90.0, "angle_deg": 90.0,
             "response": [from_complex(v0[0] + A[0, 1] * t2), from_complex(v0[1] + A[1, 1] * t2)]},
        ],
        "constraints": [],
    }
    with pytest.raises(Exception) as ei:
        solve_balancing(req)
    assert ei.value.code == "ILL_CONDITIONED_MATRIX"
    assert ei.value.details["condition_number"] > DEFAULT_MAX_CONDITION_NUMBER
    assert ei.value.details["condition_number_limit"] == DEFAULT_MAX_CONDITION_NUMBER


def test_reject_missing_trial_radius():
    req = single_plane_request()
    req["planes"] = [{"name": "无半径平面"}]
    req["trial_runs"][0]["radius_mm"] = None
    with pytest.raises(Exception) as ei:
        solve_balancing(req)
    assert ei.value.code == "MISSING_TRIAL_RADIUS"
    assert ei.value.details["plane_index"] == 0


# ---------- HTTP 接口与持久化 ----------

def test_api_create_single_plane_persists(client):
    body = single_plane_request(
        target_residual_amplitude=0.05,
        constraints=[{
            "plane_index": 0, "max_correction_mass_g": 20.0,
            "hole_angles_deg": HOLES_30, "unit_weight_mass_g": 1.0,
            "install_radius_mm": 100.0,
        }],
        operator="zhang.san", note="停机试重一次",
    )
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["balance_id"] >= 1
    assert data["equipment_id"] == "FAN-1"
    assert data["continuous_solution"]["planes"][0]["correction_mass_g"] == pytest.approx(5.0, abs=1e-5)
    assert data["discrete_solution"]["target_achieved"] is True
    # 保留原始输入
    assert data["raw_input"]["initial_vibration"][0]["phase_deg"] == pytest.approx(
        body["initial_vibration"][0]["phase_deg"])
    assert data["raw_input"]["trial_runs"][0]["trial_mass_g"] == 200.0
    assert data["diagnostics"]["condition_number"] == 1.0
    assert "influence_coefficient" in data["method"]
    assert data["created_by"] == "zhang.san"
    assert data["created_at"]

    detail = client.get(f"/api/v1/balancing/jobs/{data['balance_id']}").json()
    assert detail["raw_input"]["planes"][0]["radius_mm"] == 100.0
    assert detail["continuous_solution"] == data["continuous_solution"]
    assert detail["discrete_solution"]["planes"][0]["pieces"]


def test_api_create_two_plane_persists(client):
    r = client.post("/api/v1/balancing/jobs", json=two_plane_request())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["plane_count"] == 2 and data["point_count"] == 2
    assert len(data["method"]["continuous_solution"]) > 0


def test_api_list_filter_by_equipment_and_plane_count(client):
    client.post("/api/v1/balancing/jobs", json=single_plane_request())
    client.post("/api/v1/balancing/jobs", json=two_plane_request())
    req_b = single_plane_request(equipment_id="OTHER")
    client.post("/api/v1/balancing/jobs", json=req_b)

    r = client.get("/api/v1/balancing/jobs")
    assert r.status_code == 200
    assert r.json()["total"] == 3
    # 列表不含原始输入大字段
    assert "raw_input" not in r.json()["items"][0]
    assert "condition_number" in r.json()["items"][0]

    r = client.get("/api/v1/balancing/jobs", params={"plane_count": 2})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["plane_count"] == 2

    r = client.get("/api/v1/balancing/jobs", params={"plane_count": 1})
    assert r.json()["total"] == 2

    r = client.get("/api/v1/balancing/jobs", params={"equipment_id": "FAN-1"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["equipment_id"] == "FAN-1"

    r = client.get("/api/v1/balancing/jobs", params={"equipment_id": "FAN-1", "plane_count": 2})
    assert r.json()["total"] == 0


def test_api_detail_not_found(client):
    r = client.get("/api/v1/balancing/jobs/9999")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "BALANCING_JOB_NOT_FOUND"


def test_api_rejects_without_persisting(client):
    body = single_plane_request()
    body["initial_vibration"] = []
    r = client.post("/api/v1/balancing/jobs", json=body)
    # min_length=1 由 Pydantic 拦截为 422
    assert r.status_code == 422
    fields = [f["field"] for f in r.json()["error"]["details"]["fields"]]
    assert any("initial_vibration" in f for f in fields)

    # 业务校验（响应变化过小）→ 400，且不落库
    body = single_plane_request()
    v0 = body["initial_vibration"][0]
    body["trial_runs"][0]["response"] = [dict(v0)]  # 响应完全不变
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RESPONSE_CHANGE_TOO_SMALL"
    listing = client.get("/api/v1/balancing/jobs").json()
    assert listing["total"] == 0


def test_api_pydantic_rejects_bad_values(client):
    # 试重质量必须 > 0；NaN / Inf 拒绝；平面数最多 2
    body = single_plane_request()
    body["trial_runs"][0]["trial_mass_g"] = 0
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 422

    # NaN 不是合法 JSON 文本，直接发送字符串以验证服务端 Pydantic 拦截
    import json as _json
    body = single_plane_request()
    body["trial_runs"][0]["angle_deg"] = float("nan")
    r = client.post(
        "/api/v1/balancing/jobs",
        content=_json.dumps(body, allow_nan=True),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422

    body = two_plane_request()
    body["planes"].append({"name": "X", "radius_mm": 50.0})
    body["measurement_points"].append({"name": "C"})
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 422


def test_api_duplicate_hole_angles_rejected(client):
    body = single_plane_request(constraints=[{
        "plane_index": 0, "hole_angles_deg": [10.0, 370.0],
        "unit_weight_mass_g": 1.0,
    }])
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 422
    assert "孔位" in r.text


def test_api_unit_weight_over_max_rejected(client):
    body = single_plane_request(constraints=[{
        "plane_index": 0, "max_correction_mass_g": 5.0,
        "hole_angles_deg": HOLES_30, "unit_weight_mass_g": 10.0,
    }])
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 422


def test_api_points_fewer_than_planes_rejected(client):
    body = two_plane_request()
    body["measurement_points"] = [{"name": "ONLY"}]
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 422
    assert "测点" in r.text


def test_api_failed_job_not_listed_and_error_details_locatable(client):
    body = two_plane_request()
    body["trial_runs"] = [body["trial_runs"][0]]  # 缺一轮试重
    r = client.post("/api/v1/balancing/jobs", json=body)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "DIMENSION_MISMATCH"
    assert err["details"]["field"] == "trial_runs"
    assert err["details"]["expected"] == 2 and err["details"]["actual"] == 1
    assert client.get("/api/v1/balancing/jobs").json()["total"] == 0
