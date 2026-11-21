# 价格序列 CSV

> 契约标识：`ies.price-series`；目标 `schema_version`：`"1.0.0"`；推荐文件名：`<price-id>.price.csv`

价格序列 CSV 专用于交付能源全运营期价格序列或按自然月沿用的日内价格曲线。用户在导入前处理好原始序列，上传入口校验文件自身格式并生成不可变对象引用；序列点数与项目运营期或整天点数的对应核对在任务提交边界完成。

## 文件结构示例

```csv
# schema: ies.price-series
# schema_version: 1.0.0
# resolution: 1h
# unit: CNY/kWh
step,price
0,0.7
1,0.7
2,0.35
```

*(注：示例仅节选片段，正式文件必须包含对应用途所需的完整数据行。)*

## 元数据与表头规范

文件由 `# ` 元数据区、表头与至少一行数据组成（数据区禁止穿插注释，未知或缺失元数据键一律拒绝）：

| 位置 | 规范要求 |
|---|---|
| `schema` | 固定为 `ies.price-series` |
| `schema_version` | 固定为 `"1.0.0"` |
| `resolution` | 采样间隔：`15min` / `30min` / `1h`，须与上传表单一致 |
| `unit` | 价格单位（如 `CNY/kWh`），须与财务配置引用条目的币种与载体单位一致 |
| 表头 | 严格两列 `step,price`（大小写敏感） |
| `step` 列 | 从 `0` 开始严格 `+1` 连续递增整数 |
| `price` 列 | 有限十进制定点字符串（允许正、零、负值，禁止 `NaN`/`Infinity`），保留原始精度 |

## 上传、引用与物化衔接

1. 上传入口根据 `# schema: ies.price-series` 识别文件，校验合格后写入对象存储并在质量报告返回不透明对象标识 `price_object_id`。
2. 财务 Profile 或 Overrides 的 `energy_prices` 仅记录对象引用 `ref` 及元数据 `series_meta`（`resolution`、`point_count`、`unit`）：
   - 用于全期序列（`time_series`）时，实长须等于任务确认的完整运营期点数；
   - 用于[月份日价格](finance-yaml.md#月份日价格)（`monthly_daily`）时，实长须为完整一天（`1h`/`30min`/`15min` 分别对应 `24`/`48`/`96` 点）。
3. 计价时按已物化序列与同轴能量逐点相乘求和（`Σ price[t] * e[t]`），正/零/负价按价格条目的 `direction`（`purchase` / `sale`）统一决定会计记账符号。
