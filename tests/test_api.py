import math
import os

import pytest

# 使用独立测试数据库，需在导入 app 前设置
os.environ["VIBRATION_DB"] = "/tmp/test_vibration.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    if os.path.exists("/tmp/test_vibration.db"):
        os.remove("/tmp/test_vibration.db")
    with TestClient(app) as c:
        yield c


def make_samples(n=64, fs=1000.0, amp=1.0, spike_at=None, spike_amp=0.0, spike_len=1):
    samples = []
    for i in range(n):
        a = amp * math.sin(2 * math.pi * 50 * i / fs)
        if spike_at is not None and spike_at <= i < spike_at + spike_len:
            a = spike_amp
        samples.append({"t": i / fs, "a": a})
    return samples


def post(client, samples, equipment="PUMP-01", fs=1000.0):
    return client.post("/api/v1/analysis", json={
        "equipment_id": equipment,
        "sampling_frequency": fs,
        "samples": samples,
    })


# ---------- 正常分析 ----------

def test_normal_analysis(client):
    r = post(client, make_samples(n=200, amp=1.0))  # 10 个完整周期
    assert r.status_code == 201
    body = r.json()
    assert body["level"] == "normal"
    assert body["triggered_rules"] == []
    # 正弦波 RMS ≈ amp/√2，峰值因子 ≈ √2
    assert body["rms"] == pytest.approx(1 / math.sqrt(2), rel=1e-3)
    assert body["crest_factor"] == pytest.approx(math.sqrt(2), rel=1e-2)
    assert body["peak_to_peak"] == pytest.approx(2.0, rel=1e-2)


def test_critical_by_custom_threshold(client):
    client.put("/api/v1/thresholds/MOTOR-9", json={
        "rms_attention": 0.5, "rms_critical": 1.0,
        "p2p_attention": 3.0, "p2p_critical": 8.0,
        "crest_attention": 3.0, "crest_critical": 5.0,
        "peak_attention": 2.0, "peak_critical": 4.0,
        "window_seconds": 0.01, "min_samples": 8,
    })
    r = post(client, make_samples(amp=3.0), equipment="MOTOR-9")
    body = r.json()
    assert body["level"] == "critical"
    rules = {rule["rule"] for rule in body["triggered_rules"]}
    assert "RMS_CRITICAL" in rules
    assert any("达到严重阈值" in rule["message"] for rule in body["triggered_rules"])


def test_sustained_exceedance_vs_single_spike(client):
    # 单次尖峰：峰值因子/峰峰值可能触发，但连续超限规则不应触发
    client.put("/api/v1/thresholds/FAN-1", json={
        "rms_attention": 100.0, "rms_critical": 200.0,
        "p2p_attention": 100.0, "p2p_critical": 200.0,
        "crest_attention": 100.0, "crest_critical": 200.0,
        "peak_attention": 5.0, "peak_critical": 10.0,
        "window_seconds": 0.01, "min_samples": 8,
    })
    single = post(client, make_samples(n=100, amp=0.5, spike_at=50, spike_amp=15.0, spike_len=1), equipment="FAN-1")
    assert single.json()["level"] == "normal"

    # 持续超限：连续 20 个样本（0.02s > 窗口 0.01s）超过严重阈值
    sustained = post(client, make_samples(n=100, amp=0.5, spike_at=40, spike_amp=15.0, spike_len=20), equipment="FAN-1")
    body = sustained.json()
    assert body["level"] == "critical"
    assert any(rule["rule"] == "SUSTAINED_CRITICAL" for rule in body["triggered_rules"])


# ---------- 4xx 错误 ----------

def test_missing_fields_422(client):
    r = client.post("/api/v1/analysis", json={"equipment_id": "PUMP-01"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    fields = {f["field"] for f in body["error"]["details"]["fields"]}
    assert any("sampling_frequency" in f for f in fields)
    assert any("samples" in f for f in fields)


def test_time_out_of_order_400(client):
    samples = make_samples(n=16)
    samples[5]["t"] = samples[2]["t"]  # 制造乱序
    r = post(client, samples)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TIME_OUT_OF_ORDER"


def test_insufficient_samples_400(client):
    r = post(client, make_samples(n=4))
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "INSUFFICIENT_SAMPLES"
    assert body["error"]["details"]["required"] == 8


def test_invalid_threshold_config_422(client):
    r = client.put("/api/v1/thresholds/X-1", json={
        "rms_attention": 10.0, "rms_critical": 5.0,  # attention >= critical
        "p2p_attention": 3.0, "p2p_critical": 8.0,
        "crest_attention": 3.0, "crest_critical": 5.0,
        "peak_attention": 2.0, "peak_critical": 4.0,
        "window_seconds": 0.01, "min_samples": 8,
    })
    assert r.status_code == 422


# ---------- 记录查询与回放 ----------

def test_record_query_and_replay(client):
    post(client, make_samples(amp=1.0), equipment="PUMP-A")
    post(client, make_samples(amp=1.0), equipment="PUMP-B")

    r = client.get("/api/v1/analysis", params={"equipment_id": "PUMP-A"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    record_id = r.json()["items"][0]["id"]

    detail = client.get(f"/api/v1/analysis/{record_id}")
    assert detail.status_code == 200
    assert detail.json()["equipment_id"] == "PUMP-A"

    replay = client.get(f"/api/v1/analysis/{record_id}/samples", params={"limit": 10})
    assert replay.status_code == 200
    body = replay.json()
    assert body["total"] == 64
    assert len(body["samples"]) == 10
    ts = [s["t"] for s in body["samples"]]
    assert ts == sorted(ts)

    page2 = client.get(f"/api/v1/analysis/{record_id}/samples", params={"offset": 10, "limit": 100})
    assert page2.json()["samples"][0]["idx"] == 10


def test_record_not_found_404(client):
    assert client.get("/api/v1/analysis/999").status_code == 404
    assert client.get("/api/v1/analysis/999/samples").status_code == 404


def test_threshold_default_and_delete(client):
    r = client.get("/api/v1/thresholds/UNKNOWN-1")
    assert r.status_code == 200
    assert r.json()["source"] == "default"

    assert client.delete("/api/v1/thresholds/UNKNOWN-1").status_code == 404
