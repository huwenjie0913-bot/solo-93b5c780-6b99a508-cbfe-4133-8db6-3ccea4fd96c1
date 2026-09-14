# 振动状态分析 API

面向旋转设备（泵、电机、风机）的振动采样分析服务：接收设备标识、采样频率与按时间排列的加速度样本，计算 **RMS / 峰峰值 / 峰值因子**，按设备阈值与**连续超限窗口**判定 `normal`（正常）/ `attention`（关注）/ `critical`（严重），并说明触发的规则，帮助区分偶发尖峰与持续超限。

此外提供**时间窗 × 转速工况分析**：对带转速的样本按时间窗与转速区间两级聚合，计算各工况下的 RMS / 峰值 / 峭度；越界窗口自动生成带时间戳与严重级别的**告警**，支持查询、交班确认、备注与操作者留痕。

还提供**频谱诊断**：以已有采样记录为输入，去直流并加 Hann 窗后做单边 FFT，给出频率分辨率、主峰频率与幅值；按录入的恒定转速把频谱换算为阶次并汇总用户指定阶次带的能量占比；提供轴承几何参数时推导 BPFO / BPFI / BSF / FTF，按特征频带能量占比阈值判定 `normal` / `attention` / `critical` 并给出命中依据；时域指标正常但特定频率存在稳定能量峰的场景由此可见线索。

最后提供**基线对比**：检修后的泵重新上线时，用同一设备多条已保存频谱诊断及其源分析记录，按转速分组统计 RMS、峰值因子、主峰频率、峰值阶次的中位数与离散范围，生成带版本、来源 ID 与生效时间的基线；新诊断相对基线给出各指标偏差、变化率与关注/严重等级，帮助维护人员区分“负载差异导致的同工况正常波动”与“设备状态漂移”。

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
| POST | `/api/v1/windows/analysis` | 时间窗 × 转速区间聚合分析，越界窗口生成告警 |
| POST | `/api/v1/spectrum/diagnoses` | 以已有记录为输入做频谱/阶次/轴承诊断并保存 |
| GET | `/api/v1/spectrum/diagnoses` | 频谱诊断查询（`equipment_id` / `start_time` / `end_time` / 分页） |
| GET | `/api/v1/spectrum/diagnoses/{id}` | 频谱诊断详情（谱指标、阶次带、轴承特征频带与命中依据） |
| POST | `/api/v1/baselines/{equipment_id}` | 按转速分组构建设备振动基线（版本自增） |
| GET | `/api/v1/baselines` | 基线查询（`equipment_id` / `status` / 分页） |
| GET | `/api/v1/baselines/{id}` | 基线详情（分组统计、来源 ID、审计事件流） |
| POST | `/api/v1/baselines/{id}/activate` | 启用基线（同设备旧启用基线自动变为 superseded） |
| POST | `/api/v1/baselines/{id}/deactivate` | 停用基线 |
| POST | `/api/v1/baselines/{id}/compare` | 指定基线对新诊断做偏差/变化率/等级对比 |
| POST | `/api/v1/equipment/{equipment_id}/baseline-compare` | 便捷入口：用设备当前启用基线对比 |
| GET | `/api/v1/alarms` | 告警查询（`equipment_id` / `status` / `severity` / 分页） |
| GET | `/api/v1/alarms/{id}` | 告警详情（含操作事件流） |
| POST | `/api/v1/alarms/{id}/acknowledge` | 确认告警（交班，记录操作者） |
| POST | `/api/v1/alarms/{id}/notes` | 追加备注（记录操作者） |
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

## 时间窗 × 转速工况分析

```json
POST /api/v1/windows/analysis
{
  "equipment_id": "PUMP-01",
  "window_seconds": 1.0,
  "rpm_bins": [0, 1500, 3000],
  "reference_time": "2026-09-13T08:00:00+00:00",
  "samples": [{"t": 0.0, "a": 0.12, "rpm": 980}, {"t": 0.01, "a": 0.46, "rpm": 2100}, ...]
}
```

- 样本先按 `floor(t / window_seconds)` 切时间窗，再按 `rpm_bins` 的**左闭右开**区间归入转速工况；样本转速不在 `[首边界, 末边界)` 内返回 `RPM_OUT_OF_BINS`。
- 每个“时间窗 × 转速区间”计算 `rms`（均方根）、`peak_abs`（峰值）、`kurtosis`（峭度，正态信号≈3）、`peak_to_peak`，样本数少于 `window_min_samples` 的窗口标记 `level=insufficient`，不参与判定、不产生告警。
- 越界窗口各生成一条告警，`window_time = reference_time + 窗口起点`（未传 `reference_time` 时取当前 UTC 时间），消息中携带窗口编号与转速区间，便于定位“异常振动发生在哪个运行工况”。
- 响应的 `overall_level` 为所有窗口的最高等级，`alarm_count` 为本次新建告警数，各窗口的 `alarm_id` 关联到持久化告警。

## 频谱诊断

```json
POST /api/v1/spectrum/diagnoses
{
  "record_id": 12,
  "rpm": 1500,
  "order_bands": [
    {"name": "1X", "order_min": 0.9, "order_max": 1.1},
    {"name": "2X", "order_min": 1.8, "order_max": 2.2}
  ],
  "bearing_geometry": {
    "ball_count": 8,
    "ball_diameter": 6.746,
    "pitch_diameter": 28.5,
    "contact_angle": 0
  }
}
```

- **基础谱**：从 `record_id` 对应记录取原始样本，先去直流（减均值）再加 Hann 窗，执行单边 FFT；响应含 `frequency_resolution_hz`（fs/N）、`nyquist_frequency_hz`、`main_peak`（主峰频率、经 Hann coherent gain 修正的幅值、谱线索引）。
- **阶次分析**：按恒定转速 `rpm` 计算转频 `fr = rpm/60`，阶次 `order = f / fr`；`order_bands` 以转频倍数给出（下界必须 >0，上界不得越过奈奎斯特频率），逐带返回换算后的频率区间与能量占比（带内功率 / 全谱功率，直流除外），并给出主峰对应阶次 `peak_order`。
- **轴承诊断**：几何参数齐全时推导特征频率（`d` 为滚动体直径，`D` 为节径，`α` 为接触角，`n` 为滚动体数）：
  - BPFO = `fr·n/2·(1 − d/D·cosα)`，BPFI = `fr·n/2·(1 + d/D·cosα)`
  - BSF = `fr·D/d·(1 − (d/D·cosα)²)`，FTF = `fr/2·(1 − d/D·cosα)`

  每个特征频率以 ±`bearing_band_tolerance`（默认 2%）取频带，频带能量占比 ≥ `bearing_critical_ratio`（默认 15%）判 `critical`，≥ `bearing_attention_ratio`（默认 5%）判 `attention`，否则 `normal`；`hits` 汇总命中频带及中文依据。任一派生频带（含容差半宽）越过奈奎斯特频率时，整个请求以 `400 BAND_OUT_OF_RANGE` 拒绝（响应 details 列出越界频带），不会把无法评估的结果保存为 normal。
- **无几何参数**（或仅提供部分字段）：基础谱与阶次结果照常返回，轴承诊断 `status=unavailable`，`missing_fields` 与 `reason` 说明缺少 `ball_count` / `ball_diameter` / `pitch_diameter` / `contact_angle` 中的哪些字段。
- 三个阈值可随设备阈值配置（`bearing_*` 字段，对老调用方为可选），也可在请求中用 `bearing_attention_ratio` / `bearing_critical_ratio` / `bearing_band_tolerance` 临时覆盖。
- 诊断结果持久化，`GET /api/v1/spectrum/diagnoses` 支持按 `equipment_id` 与创建时间范围 `start_time` / `end_time`（ISO 8601）筛选、分页；`GET .../{id}` 查看详情。

## 振动基线与偏差对比

```json
POST /api/v1/baselines/PUMP-01
{
  "rpm_bins": [0, 1700, 3000],
  "min_samples_per_group": 3,
  "deviation_thresholds": {
    "rms_attention": 0.15, "rms_critical": 0.30,
    "main_peak_frequency_attention": 0.05, "main_peak_frequency_critical": 0.10
  },
  "effective_from": "2026-09-14T08:00:00+00:00",
  "operator": "li.si",
  "note": "大修后稳定运行一周构建"
}
```

- **来源**：默认取该设备全部已保存频谱诊断（`spectrum_diagnoses` 与其源分析记录 `analysis_records` JOIN 得到指标），可用 `source_diagnosis_ids` 显式指定（任一 ID 不存在或属于其他设备返回 `404 DIAGNOSIS_NOT_FOUND`），或用 `start_time` / `end_time` 按诊断创建时间过滤。
- **转速分组**：传 `rpm_bins` 时按左闭右开区间归组（与时间窗分析一致），诊断转速超出覆盖范围返回 `400 RPM_OUT_OF_BINS`；不传时按完全相同的 `rpm` 精确归组。每组分别统计 **RMS、峰值因子（取自源分析记录）、主峰频率（Hz）、峰值阶次（取自诊断 order.peak_order）** 的 `median`（中位数）与 `min` / `max`（离散范围），并记录每组的 `source_diagnosis_ids`。
- **样本数门槛**：每组诊断条数少于 `min_samples_per_group`（默认 3）时跳过该组并在响应 `skipped_groups` 中说明；没有任何组达标返回 `400 INSUFFICIENT_BASELINE_SAMPLES`（details 含被跳过的分组）。
- **版本与状态**：同一设备版本号从 1 自增；新建基线默认 `active`，设备上原有 active 基线自动变为 `superseded`。可通过 activate / deactivate 接口切换（重复操作返回 409），启用旧版本时当前 active 版本同样被置为 superseded。基线带 `effective_from`（缺省取当前 UTC）、`note`、`created_by`，所有创建/启用/停用操作连同操作者、备注与详情写入 `baseline_audit_events`，随基线详情返回完整事件流。
- **偏差阈值**：按指标配置相对偏差 `|新值 − 中位数| / |中位数|` 的关注线 / 严重线，默认 RMS 15%/30%、峰值因子 20%/40%、主峰频率与峰值阶次 5%/10%，attention 必须小于 critical（否则 422）。对比请求中可用 `deviation_thresholds` 临时覆盖，不修改已保存基线。

```json
POST /api/v1/baselines/3/compare
{"diagnosis_id": 42}

POST /api/v1/equipment/PUMP-01/baseline-compare
{"diagnosis_id": 42}
```

- 对比前按新诊断的转速匹配基线转速分组（精确组按相同转速，区间组按左闭右开）；基线未覆盖该转速返回 `400 BASELINE_RPM_NOT_COVERED`，跨设备对比返回 `400 BASELINE_EQUIPMENT_MISMATCH`，设备没有启用基线而使用便捷入口时返回 `409 NO_ACTIVE_BASELINE`。
- 逐指标返回 `value` / `baseline_median` / `baseline_min` / `baseline_max`、带符号的 `deviation`（绝对偏差）与 `change_rate`（相对变化率，上升为正）、`within_dispersion`（是否落在历史离散范围内）、命中的阈值与中文 `message`；`overall_level` 取四项指标的最高等级。中位数为 0 时变化率无法计算（返回 `null`），该项按 normal 处理并在 message 中说明。
- 直接对停用 / 被取代基线调用 compare 仍然可用，响应中以 `baseline_status` 标明其当前状态；对比结果不落库（基线本身及其审计留痕持久化）。

## 告警交班与留痕

- 告警初始 `status=open`，确认后变为 `acknowledged`，记录 `acknowledged_by` / `acknowledged_at`；重复确认返回 `409 ALARM_ALREADY_ACKNOWLEDGED`，已确认告警仍可追加备注。
- 所有操作（系统创建、确认、备注）写入 `alarm_events` 事件流，GET 告警详情可查看完整的操作者与时间留痕。

```json
POST /api/v1/alarms/12/acknowledge
{"operator": "zhang.san", "note": "交班给夜班继续排查"}
```

## 判定规则

1. **RMS**：整体振动能量，超过 `rms_attention` / `rms_critical` 触发关注/严重；
2. **峰峰值**：单次冲击幅度，按 `p2p_*` 阈值判定；
3. **峰值因子**（峰值/RMS）：冲击特征，按 `crest_*` 阈值判定；
4. **连续超限窗口**：`|a|` 连续超过 `peak_*` 阈值的时长 ≥ `window_seconds` 才触发——偶发尖峰（单次峰值）不会误判为持续超限。

最终等级取所有触发规则中的最高级，响应的 `triggered_rules` 逐条说明规则、指标值、阈值与建议。

**窗口指标阈值**（时间窗分析使用，可与上述阈值一起按设备配置）：`rms_*` / `peak_*` 复用于窗口 RMS 与峰值判定，另增 `kurtosis_attention=4.0` / `kurtosis_critical=8.0`（窗口峭度）与 `window_min_samples=4`（窗口参与判定的最少样本数）。这些字段对老调用方为可选，缺省取默认值；旧版数据库在启动时自动增量迁移补列。

**轴承频带阈值**（频谱诊断使用）：`bearing_attention_ratio=0.05` / `bearing_critical_ratio=0.15`（特征频带能量占比关注/严重线）与 `bearing_band_tolerance=0.02`（特征频率频带相对半宽），同为设备阈值的可选字段，旧库启动时自动迁移。

## 错误响应

统一结构 `{"error": {"code", "message", "details"}}`：

| 场景 | 状态码 | code |
|---|---|---|
| 缺失字段 / 类型错误 / NaN / 转速边界非递增 | 422 | `VALIDATION_ERROR` |
| 样本时间未严格递增 | 400 | `TIME_OUT_OF_ORDER` |
| 样本数不足 `min_samples` | 400 | `INSUFFICIENT_SAMPLES` |
| 窗口样本转速超出 `rpm_bins` 覆盖范围 | 400 | `RPM_OUT_OF_BINS` |
| 采样间隔不一致（含与记录声明采样频率不符） | 400 | `NON_UNIFORM_SAMPLING` |
| 阶次带/特征频带越过奈奎斯特频率或边界非法 | 400 | `BAND_OUT_OF_RANGE` / `INVALID_FREQUENCY_BAND` |
| 查询时间范围格式非法或起止颠倒 | 400 | `INVALID_TIME_RANGE` |
| 告警重复确认 | 409 | `ALARM_ALREADY_ACKNOWLEDGED` |
| 基线诊断转速超出 `rpm_bins` 覆盖范围 | 400 | `RPM_OUT_OF_BINS` |
| 基线无分组 / 无任何分组达到最少诊断条数 | 400 | `INSUFFICIENT_BASELINE_SAMPLES` |
| 基线未覆盖待对比诊断的转速 | 400 | `BASELINE_RPM_NOT_COVERED` |
| 诊断与基线不属于同一设备 | 400 | `BASELINE_EQUIPMENT_MISMATCH` |
| 重复启用 / 停用基线 | 409 | `BASELINE_ALREADY_ACTIVE` / `BASELINE_ALREADY_INACTIVE` |
| 设备没有启用基线时走便捷对比入口 | 409 | `NO_ACTIVE_BASELINE` |
| 记录 / 阈值 / 告警 / 频谱诊断 / 基线不存在 | 404 | `RECORD_NOT_FOUND` / `THRESHOLD_NOT_FOUND` / `ALARM_NOT_FOUND` / `DIAGNOSIS_NOT_FOUND` / `BASELINE_NOT_FOUND` |

## 测试

```bash
python3 -m pytest tests/ -q
```
