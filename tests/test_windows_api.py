"""时间窗 × 转速工况聚合、窗口阈值判定与告警管理测试。"""
import os

import pytest

# 使用独立测试数据库，需在导入 app 前设置
os.environ["VIBRATION_DB"] = "/tmp/test_vibration_windows.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.analysis import (  # noqa: E402
    aggregate_windows,
    compute_kurtosis,
    compute_window_metrics,
    evaluate_window,
)
from app.errors import ApiError  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    if os.path.exists("/tmp/test_vibration_windows.db"):
        os.remove("/tmp/test_vibration_windows.db")
    with TestClient(app) as c:
        yield c


def window_payload(samples, equipment="PUMP-W", window_seconds=1.0,
                   rpm_bins=(0, 1500, 3000), reference_time=None):
    body = {
        "equipment_id": equipment,
        "window_seconds": window_seconds,
        "rpm_bins": list(rpm_bins),
        "samples": samples,
    }
    if reference_time is not None:
        body["reference_time"] = reference_time
    return body


def post_windows(client, samples, **kwargs):
    return client.post("/api/v1/windows/analysis", json=window_payload(samples, **kwargs))


def const_samples(n, t0=0.0, fs=100.0, a=1.0, rpm=1000.0):
    return [{"t": t0 + i / fs, "a": a, "rpm": rpm} for i in range(n)]


# ---------- 聚合计算（单元测试） ----------

def test_kurtosis_constant_signal_is_zero():
    # 方差为 0 的恒值信号没有四阶矩，定义为 0（不应除零）
    assert compute_kurtosis([2.0, 2.0, 2.0]) == 0.0


def test_kurtosis_known_values():
    # ±1 方波：均值 0，m2=m4=1 → 峭度 1
    assert compute_kurtosis([-1.0, 1.0, -1.0, 1.0]) == pytest.approx(1.0)
    # 单个强冲击拉高峭度
    values = [0.0] * 7 + [8.0]  # 均值 1，m2=7，m4=301
    assert compute_kurtosis(values) == pytest.approx(301.0 / 49.0)


def test_window_metrics_rms_peak_p2p():
    samples = const_samples(4, a=3.0)
    m = compute_window_metrics(samples)
    assert m["sample_count"] == 4
    assert m["rms"] == pytest.approx(3.0)
    assert m["peak_abs"] == pytest.approx(3.0)
    assert m["peak_to_peak"] == pytest.approx(0.0)
    assert m["kurtosis"] == pytest.approx(0.0)


def test_aggregate_splits_time_windows_and_rpm_bands():
    # 第 1 秒低速 1000rpm，第 2 秒高速 2000rpm → 两个时间窗、各一个转速区间
    samples = const_samples(8, t0=0.0, rpm=1000.0, a=1.0) + \
        const_samples(8, t0=1.0, rpm=2000.0, a=2.0)
    windows = aggregate_windows(samples, 1.0, [0, 1500, 3000])
    assert [w["window_index"] for w in windows] == [0, 1]
    assert windows[0]["rpm_min"] == 0 and windows[0]["rpm_max"] == 1500
    assert windows[1]["rpm_min"] == 1500 and windows[1]["rpm_max"] == 3000
    assert windows[0]["rms"] == pytest.approx(1.0)
    assert windows[1]["rms"] == pytest.approx(2.0)
    # 同一时间窗内两种转速工况 → 同窗不同区间，按区间顺序返回
    interleaved = []
    for i in range(16):
        interleaved.append({"t": i * 0.05, "a": 1.0, "rpm": 1000.0 if i % 2 == 0 else 2000.0})
    windows2 = aggregate_windows(interleaved, 1.0, [0, 1500, 3000])
    assert len(windows2) == 2
    assert {w["rpm_bin_index"] for w in windows2} == {0, 1}
    assert all(w["sample_count"] == 8 for w in windows2)


def test_aggregate_rejects_rpm_outside_bins():
    samples = const_samples(8, rpm=3000.0)  # 区间左闭右开，3000 不属于任何区间
    with pytest.raises(ApiError) as exc:
        aggregate_windows(samples, 1.0, [0, 1500, 3000])
    assert exc.value.code == "RPM_OUT_OF_BINS"
    assert exc.value.status_code == 400


# ---------- 阈值判定与告警持久化（接口测试） ----------

def test_window_analysis_normal(client):
    r = post_windows(client, const_samples(16, a=0.1, rpm=1000.0))
    assert r.status_code == 201
    body = r.json()
    assert body["overall_level"] == "normal"
    assert body["alarm_count"] == 0
    assert body["window_count"] == 1  # t=0~0.15s 全部落在第 0 窗
    win = body["windows"][0]
    assert win["evaluated"] is True
    assert win["level"] == "normal"
    assert win["rms"] == pytest.approx(0.1)
    assert win["alarm_id"] is None


def test_window_analysis_critical_only_in_high_rpm_band(client):
    # 同一时间窗内：低速工况正常，高速工况 RMS 严重越界
    samples = []
    for i in range(16):
        rpm = 1000.0 if i % 2 == 0 else 2000.0
        samples.append({"t": i * 0.05, "a": 0.1 if rpm < 1500 else 10.0, "rpm": rpm})
    r = post_windows(
        client, samples, reference_time="2026-09-13T08:00:00+00:00"
    )
    assert r.status_code == 201
    body = r.json()
    assert body["overall_level"] == "critical"
    assert body["alarm_count"] == 1

    levels = {(w["rpm_min"], w["level"]) for w in body["windows"]}
    assert (0.0, "normal") in levels
    assert (1500.0, "critical") in levels

    bad = next(w for w in body["windows"] if w["level"] == "critical")
    rules = {rule["rule"] for rule in bad["triggered_rules"]}
    assert "WINDOW_RMS_CRITICAL" in rules
    assert bad["alarm_id"] is not None
    # 告警窗口时间戳 = 参考时间 + 窗口起点
    assert bad["window_start"] == 0.0

    detail = client.get(f"/api/v1/alarms/{bad['alarm_id']}").json()
    assert detail["severity"] == "critical"
    assert detail["status"] == "open"
    assert detail["equipment_id"] == "PUMP-W"
    assert detail["rpm_min"] == 1500.0 and detail["rpm_max"] == 3000.0
    assert detail["window_time"] == "2026-09-13T08:00:00+00:00"
    assert detail["reference_time"] == "2026-09-13T08:00:00+00:00"
    assert "2000" not in detail["message"]  # 消息中描述的是工况区间，非单个转速
    assert "1500" in detail["message"] and "3000" in detail["message"]


def test_window_timestamp_offsets_by_window_start(client):
    # t 从 2.0s 开始 → 落在第 2 个时间窗，告警时间戳 = 参考时间 + 2s
    samples = const_samples(8, t0=2.0, rpm=2000.0, a=10.0)
    r = post_windows(client, samples, reference_time="2026-09-13T08:00:00+00:00")
    body = r.json()
    assert body["windows"][0]["window_index"] == 2
    alarm_id = body["windows"][0]["alarm_id"]
    detail = client.get(f"/api/v1/alarms/{alarm_id}").json()
    assert detail["window_time"] == "2026-09-13T08:00:02+00:00"


def test_kurtosis_threshold_triggers_attention(client):
    client.put("/api/v1/thresholds/GEAR-7", json={
        "rms_attention": 100.0, "rms_critical": 200.0,
        "p2p_attention": 100.0, "p2p_critical": 200.0,
        "crest_attention": 100.0, "crest_critical": 200.0,
        "peak_attention": 100.0, "peak_critical": 200.0,
        "kurtosis_attention": 4.0, "kurtosis_critical": 8.0,
        "window_seconds": 1.0, "window_min_samples": 4, "min_samples": 8,
    })
    values = [0.0] * 7 + [8.0]  # 峭度 ≈ 6.14，处于关注线与严重线之间
    samples = [{"t": i * 0.05, "a": v, "rpm": 1200.0} for i, v in enumerate(values)]
    r = post_windows(client, samples, equipment="GEAR-7")
    win = r.json()["windows"][0]
    assert win["level"] == "attention"
    assert win["kurtosis"] == pytest.approx(301.0 / 49.0)
    assert any(rule["rule"] == "WINDOW_KURTOSIS_ATTENTION" for rule in win["triggered_rules"])
    assert win["alarm_id"] is not None


def test_insufficient_window_samples_not_evaluated(client):
    client.put("/api/v1/thresholds/SPARSE-1", json={
        "rms_attention": 1.0, "rms_critical": 2.0,
        "p2p_attention": 3.0, "p2p_critical": 8.0,
        "crest_attention": 3.0, "crest_critical": 5.0,
        "peak_attention": 2.0, "peak_critical": 4.0,
        "window_seconds": 1.0, "window_min_samples": 8, "min_samples": 8,
    })
    # 同一窗内：工况 0 有 8 个样本参与判定，工况 1 仅 4 个样本不判定
    samples = [{"t": i * 0.05, "a": 0.1, "rpm": 1000.0 if i < 8 else 2000.0} for i in range(12)]
    r = post_windows(client, samples, equipment="SPARSE-1")
    assert r.status_code == 201
    windows = r.json()["windows"]
    sparse = next(w for w in windows if w["rpm_min"] == 1500.0)
    dense = next(w for w in windows if w["rpm_min"] == 0.0)
    assert sparse["evaluated"] is False
    assert sparse["level"] == "insufficient"
    assert sparse["alarm_id"] is None
    assert dense["evaluated"] is True


# ---------- 请求校验 ----------

def test_rpm_out_of_bins_400(client):
    r = post_windows(client, const_samples(8, rpm=9999.0))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RPM_OUT_OF_BINS"


def test_window_time_out_of_order_400(client):
    samples = const_samples(8)
    samples[3]["t"] = samples[0]["t"]
    r = post_windows(client, samples)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TIME_OUT_OF_ORDER"


def test_rpm_bins_must_increase_422(client):
    r = client.post("/api/v1/windows/analysis", json={
        "equipment_id": "PUMP-W", "window_seconds": 1.0,
        "rpm_bins": [0, 2000, 1500], "samples": const_samples(8),
    })
    assert r.status_code == 422


def test_negative_rpm_422(client):
    samples = const_samples(8)
    samples[0]["rpm"] = -10
    assert post_windows(client, samples).status_code == 422


def test_threshold_without_window_fields_uses_defaults(client):
    # 老调用方不传 kurtosis/window_min_samples，应使用默认值
    r = client.put("/api/v1/thresholds/LEGACY-1", json={
        "rms_attention": 0.5, "rms_critical": 1.0,
        "p2p_attention": 3.0, "p2p_critical": 8.0,
        "crest_attention": 3.0, "crest_critical": 5.0,
        "peak_attention": 2.0, "peak_critical": 4.0,
        "window_seconds": 0.01, "min_samples": 8,
    })
    assert r.status_code == 200
    cfg = client.get("/api/v1/thresholds/LEGACY-1").json()
    assert cfg["kurtosis_attention"] == 4.0
    assert cfg["kurtosis_critical"] == 8.0
    assert cfg["window_min_samples"] == 4


# ---------- 告警查询 / 确认 / 备注 / 留痕 ----------

def _create_alarm(client, equipment="PUMP-W", rpm=2000.0, a=10.0):
    samples = const_samples(8, t0=2.0, rpm=rpm, a=a)
    body = post_windows(client, samples, equipment=equipment,
                        reference_time="2026-09-13T08:00:00+00:00").json()
    return body["windows"][0]["alarm_id"]


def test_alarm_list_filters(client):
    _create_alarm(client, equipment="PUMP-A")
    _create_alarm(client, equipment="PUMP-B")

    r = client.get("/api/v1/alarms", params={"equipment_id": "PUMP-A", "status": "open"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["equipment_id"] == "PUMP-A"
    assert body["items"][0]["severity"] == "critical"

    assert client.get("/api/v1/alarms", params={"severity": "attention"}).json()["total"] == 0
    assert client.get("/api/v1/alarms", params={"status": "acknowledged"}).json()["total"] == 0


def test_alarm_acknowledge_and_audit_trail(client):
    alarm_id = _create_alarm(client)

    # 初始状态：open，含 created 系统事件
    detail = client.get(f"/api/v1/alarms/{alarm_id}").json()
    assert detail["status"] == "open"
    assert detail["acknowledged_by"] is None
    assert [e["event_type"] for e in detail["events"]] == ["created"]
    assert detail["events"][0]["operator"] == "system"

    # 交班确认
    ack = client.post(f"/api/v1/alarms/{alarm_id}/acknowledge", json={
        "operator": "zhang.san", "note": "交班给夜班继续排查",
    })
    assert ack.status_code == 200
    ack_body = ack.json()
    assert ack_body["status"] == "acknowledged"
    assert ack_body["acknowledged_by"] == "zhang.san"
    assert ack_body["acknowledged_at"]
    assert ack_body["note"] == "交班给夜班继续排查"

    # 重复确认 → 409
    dup = client.post(f"/api/v1/alarms/{alarm_id}/acknowledge", json={"operator": "li.si"})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "ALARM_ALREADY_ACKNOWLEDGED"

    # 追加备注（已确认告警仍可备注）
    noted = client.post(f"/api/v1/alarms/{alarm_id}/notes", json={
        "operator": "li.si", "note": "复测已恢复正常",
    })
    assert noted.status_code == 201
    event = noted.json()
    assert event["event_type"] == "noted"
    assert event["operator"] == "li.si"
    assert event["note"] == "复测已恢复正常"

    # 完整事件流留痕，按时间排序
    final = client.get(f"/api/v1/alarms/{alarm_id}").json()
    assert [e["event_type"] for e in final["events"]] == ["created", "acknowledged", "noted"]
    assert [e["operator"] for e in final["events"]] == ["system", "zhang.san", "li.si"]
    assert final["note"] == "复测已恢复正常"

    # 列表中状态同步
    assert client.get("/api/v1/alarms", params={"status": "acknowledged"}).json()["total"] == 1
    assert client.get("/api/v1/alarms", params={"status": "open"}).json()["total"] == 0


def test_alarm_endpoints_404(client):
    assert client.get("/api/v1/alarms/999").status_code == 404
    assert client.post("/api/v1/alarms/999/acknowledge",
                       json={"operator": "x"}).status_code == 404
    assert client.post("/api/v1/alarms/999/notes",
                       json={"operator": "x", "note": "n"}).status_code == 404


def test_acknowledge_requires_operator(client):
    alarm_id = _create_alarm(client)
    r = client.post(f"/api/v1/alarms/{alarm_id}/acknowledge", json={"note": "缺操作者"})
    assert r.status_code == 422
    assert client.get(f"/api/v1/alarms/{alarm_id}").json()["status"] == "open"


def test_evaluate_window_helper_levels():
    from app.analysis import DEFAULT_THRESHOLDS
    th = {**DEFAULT_THRESHOLDS,
          "rms_attention": 2.0, "rms_critical": 5.0,
          "peak_attention": 3.0, "peak_critical": 6.0,
          "kurtosis_attention": 4.0, "kurtosis_critical": 8.0}
    normal, rules_n = evaluate_window(
        compute_window_metrics(const_samples(8, a=0.1)), th, 1000.0)
    assert normal == "normal" and rules_n == []
    critical, rules_c = evaluate_window(
        compute_window_metrics(const_samples(8, a=10.0)), th, 2000.0)
    assert critical == "critical"
    assert any(r["metric"] == "rms" for r in rules_c)
    # 消息中带工况描述，便于维护人员定位
    assert "rpm 工况" in rules_c[0]["message"]
