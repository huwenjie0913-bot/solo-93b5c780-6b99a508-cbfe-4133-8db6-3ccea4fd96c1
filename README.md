# 振动状态分析 API

面向旋转设备（泵、电机、风机）的振动采样分析服务：接收设备标识、采样频率与按时间排列的加速度样本，计算 **RMS / 峰峰值 / 峰值因子**，按设备阈值与**连续超限窗口**判定 `normal`（正常）/ `attention`（关注）/ `critical`（严重），并说明触发的规则，帮助区分偶发尖峰与持续超限。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
# 数据库路径可用环境变量覆盖：VIBRATION_DB=/path/to/vibration.db
```

交互式文档：`http://localhost:8000/docs`

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/analysis` | 提交采样并分析，持久化记录与原始样本 |
| GET | `/api/v1/analysis` | 记录查询（`equipment_id` / `level` / 分页） |
| GET | `/api/v1/analysis/{id}` | 单条记录详情 |
| GET | `/api/v1/analysis/{id}/samples` | 原始样本回放（`offset` / `limit` 分页） |
| PUT | `/api/v1/thresholds/{equipment_id}` | 创建/更新设备阈值 |
| GET | `/api/v1/thresholds/{equipment_id}` | 查询阈值（未配置时返回默认值） |
| GET | `/api/v1/thresholds` | 列出所有自定义阈值 |
| DELETE | `/api/v1/thresholds/{equipment_id}` | 删除自定义阈值 |

## 分析请求示例

```json
POST /api/v1/analysis
{
  "equipment_id": "PUMP-01",
  "sampling_frequency": 1000,
  "samples": [{"t": 0.0, "a": 0.12}, {"t": 0.001, "a": 0.46}, ...]
}
```

## 判定规则

1. **RMS**：整体振动能量，超过 `rms_attention` / `rms_critical` 触发关注/严重；
2. **峰峰值**：单次冲击幅度，按 `p2p_*` 阈值判定；
3. **峰值因子**（峰值/RMS）：冲击特征，按 `crest_*` 阈值判定；
4. **连续超限窗口**：`|a|` 连续超过 `peak_*` 阈值的时长 ≥ `window_seconds` 才触发——偶发尖峰（单次峰值）不会误判为持续超限。

最终等级取所有触发规则中的最高级，响应的 `triggered_rules` 逐条说明规则、指标值、阈值与建议。

## 错误响应

统一结构 `{"error": {"code", "message", "details"}}`：

| 场景 | 状态码 | code |
|---|---|---|
| 缺失字段 / 类型错误 / NaN | 422 | `VALIDATION_ERROR` |
| 样本时间未严格递增 | 400 | `TIME_OUT_OF_ORDER` |
| 样本数不足 `min_samples` | 400 | `INSUFFICIENT_SAMPLES` |
| 记录 / 阈值不存在 | 404 | `RECORD_NOT_FOUND` / `THRESHOLD_NOT_FOUND` |

## 测试

```bash
python3 -m pytest tests/ -q
```
