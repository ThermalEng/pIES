# 设备数据 CSV

> 契约标识：`ies.device-data`；目标 `schema_version`：`"1.0.0"`；推荐文件名：`<dataset-id>.data.csv`

设备数据 CSV 用于向设备的 `predefined` 接口提供带采样间隔、单位与连续 `step` 的时序输入。用户在上传前须自行保证数据完整、无缺失且分辨率与项目一致；平台在导入与装配边界仅做严格校验，不做隐式插值、重采样或自动补点。

## 文件结构示例

```csv
# schema: ies.device-data
# schema_version: 1.0.0
# dataset_id: campus_electric_load_2025
# device_id: acme.device.electric_load
# source_mode: data_predict
# resolution: 1h
# unit.electric_demand: kW
step,electric_demand
0,48.3
1,46.7
2,45.9
```

*(注：示例仅节选前 3 行，正式文件必须满足对应 `source_mode` 的完整行数要求。)*

## 文件布局与元数据

文件自上而下依次为：`# ` 开头的元数据区（每个键最多出现一次）、一行 CSV 表头、以及数据区（数据区不允许穿插注释；普通备注可使用 `# note.<name>:` 置于元数据区）。

| 元数据键 | 说明 |
|---|---|
| `schema` | 固定为 `ies.device-data` |
| `schema_version` | 固定为 `"1.0.0"` |
| `dataset_id` | 稳定数据集标识 |
| `device_id` | 稳定基础设备类型 ID（不含项目模型的 `_N` 后缀） |
| `source_mode` | 数据来源模式：`data_repeat`、`data_full` 或 `data_predict`（`constant` 直接在装配内填值，无 CSV） |
| `resolution` | 采样间隔（如 `15min`、`30min`、`1h`），必须与项目基线一致 |
| `unit.<column>` | 每个数据列的显式单位，须与设备模型对应接口量纲兼容 |

## CSV 方言与表头要求

- **编码与格式**：UTF-8（无 BOM）、LF 换行、英文逗号分隔（RFC 4180 转义）、英文句点小数点；布尔值写 `true`/`false`。
- **严禁缺失与非有限值**：禁止空单元格、`NaN`、`Inf`/`Infinity`、区域化千分位分隔符及电子表格公式。
- **表头与序列连续性**：第一列固定为 `step`，必须从 `0` 开始严格以步长 `+1` 连续递增；后续列名须与目标设备模型中的 `predefined` 接口 ID 一致，拒绝未知列与重复列。

## 来源模式覆盖与物化对齐

不同来源在装配与物化阶段分别满足以下覆盖约定：

| 模式 | 文件输入覆盖要求 | 计算阶段物化行为 |
|---|---|---|
| `constant` | 无 CSV 文件，在装配绑定中直接给标量值 | 展开为全周期等值连续序列 |
| `data_repeat` | 完整连续的日、周或年序列（不设独立周期字段，整份文件即重复基线） | 从 `step=0` 起循环展开至全运营期总点数 |
| `data_full` | 必须从 `step=0` 起原样覆盖由起始年与运营年限推导的全部运营期点数（任务提交边界核对） | 原样直接消费，不循环、不截断、不外推 |
| `data_predict` | 满足对应预测目标类型的历史训练/输入要求；`target_type` 在装配绑定处声明 | 由所选预测算法在计算阶段训练并输出全周期预测序列与回执 |

不同设备的原始来源在物化前不要求文件行数相同；进入计算阶段物化后，所有序列统一对齐至项目基线分辨率与全运营期连续 `step`。

## 上传入口识别（`POST …/datasets/{id}/versions`）

数据集上传入口依据文件首行 `# schema: ies.device-data` 自动识别 step CSV（无标记文件走通用时间戳 CSV 链路；未知 `# schema` 以 `DATA-META-003` 拒绝）：

- 校验 `# resolution` 与上传请求一致、`step` 自 `0` 连续、数据单元格非空且有限、每列声明 `unit.<列>`（违规返回 400 `DATA-VAL-001`）。
- 表单中的通用 `fields` 参数不适用于自描述 step 文件，传入时记录非阻断警告 `DATA-FILE-005`。
- 具体设备、接口与 `source_mode` 的匹配关系在项目装配实例绑定入口核对。

## 常见拒绝原因与处理

| 问题 | 处理方式 |
|---|---|
| `step` 缺口、重复或倒序 | 阻断并定位行号，须修复源文件后重新上传版本 |
| `data_repeat` 非完整日/周/年长度 | 阻断，补齐完整周期基线后重新上传 |
| 分辨率与项目基线不匹配、或数值超出设备 `valid_range` | 阻断，在外部预处理修正后形成新数据版本 |
| 列单位与接口量纲不兼容（如需要 `kW` 却提供 `kWh`） | 阻断，核对设备接口量纲 |
| 包含额外备注数据列 | 移入元数据区 `# note.*:` |
