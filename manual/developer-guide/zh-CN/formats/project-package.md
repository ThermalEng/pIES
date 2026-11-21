# 项目包

> 契约标识：`ies.project-package`；目标 `format_version`：`"1.0.0"`；文件格式：`*.zip`（根含 `manifest.json`）

项目包是项目的离线交换与归档载体，完整包含当前工作状态、包内历史版本及其恢复所需的财务配置、规划配置、数据集版本、项目模型与历史结果证据。包内不包含账号、密码、会话、权限或宿主机路径；源库内部 ID 仅作为包内映射键，导入确认时统一在目标库重新分配身份。

## ZIP 目录布局

```text
manifest.json
project.json
draft.json
finance_profile.yaml            # 当前工作状态财务 Profile
finance_overrides.yaml          # 当前工作状态财务 Overrides（无覆盖时为空覆盖文档）
effective_finance.yaml          # 当前工作状态 Effective 快照（由来源合并验证）
planning_config.yaml            # 当前工作状态规划配置（当前无规划时缺席）
versions/0001.json …            # 包内历史版本快照
datasets/{dataset_id}/v{version_no}/dataset.json    # 数据版本元数据
datasets/{dataset_id}/v{version_no}/{file_kind}.csv # 对应版本数据 CSV
evidence/{package_id}.json …    # 历史结果证据（保留来源记录，不伪造本地任务）
version_configs/{NNNN}/*.yaml   # 各历史版本对应的独立财务与规划依赖文件
models/{来源 device_id}.json     # 草稿与历史版本引用的项目模型规范文本
```

## `manifest.json` 关键结构

`manifest.json` 顶层包含 `format_version`、`package_type: "project"`、`objects: [{path, size_bytes, media_type}]` 与 `files: {project, draft, configs, versions[], datasets[], evidence[], version_configs, models[]}`。所有路径必须与 `objects` 清单及包内实际文件一一对应且大小一致，拒绝重复路径或越界路径。

```json
{
  "format_version": "1.0.0",
  "package_type": "project",
  "objects": [
    {"path": "project.json", "size_bytes": 412, "media_type": "application/json"},
    {"path": "datasets/7/v2/dataset.json", "size_bytes": 388, "media_type": "application/json"},
    {"path": "datasets/7/v2/data.csv", "size_bytes": 1024, "media_type": "text/csv"},
    {"path": "models/acme.device.load_1.json", "size_bytes": 320, "media_type": "application/json"},
    {"path": "version_configs/0001/finance_profile.yaml", "size_bytes": 190, "media_type": "application/yaml"}
  ],
  "files": {
    "project": "project.json",
    "draft": "draft.json",
    "configs": {"finance_profile": "finance_profile.yaml"},
    "versions": ["versions/0001.json"],
    "datasets": ["datasets/7/v2/dataset.json"],
    "evidence": [],
    "version_configs": {
      "0001": {
        "finance_profile": "version_configs/0001/finance_profile.yaml",
        "finance_overrides": "version_configs/0001/finance_overrides.yaml"
      }
    },
    "models": [
      {
        "device_id": "acme.device.load_1",
        "base_device_id": "acme.device.load",
        "model_text": "models/acme.device.load_1.json"
      }
    ]
  }
}
```

### `files.version_configs`

每个带有 `effective_finance` 或 `planning_config` 引用的历史版本 `versions/NNNN.json`，必须在 `files.version_configs["NNNN"]` 携带对应 YAML 文件（`finance_profile`、`finance_overrides`、`effective_finance`、`planning_config`，无对应配置时保持缺席）。导入时必须依据包内原始 `FinanceProfile` 与 `FinanceOverrides` 重新合并校验财务快照（若目标库已存在同 ID Profile 但内容冲突则拒绝导入），绝不用当前配置或目标库最新 Profile 替代历史版本依赖。

### `files.models`

- 必须精确携带当前草稿及包内全部历史版本内容引用的所有项目模型规范 JSON 文本（按来源编号升序），携带集合与引用集合必须完全相等（缺少引用模型或包含未引用模型均拒绝导入）；仅出现于历史结果证据的设备类型字符串不纳入此集合。
- 每项提供 `{device_id, base_device_id, model_text}`：确认导入时按 `base_device_id` 经正式模型保存入口在目标项目重新校验、分配本项目新 `_N` 编号与生成校验回执，并将草稿与历史版本中的模型引用和实例 `device_type` 映射为新 ID。

## 两阶段导入语义

1. **上传与提案**：校验归档完整性、Manifest 结构、消费骨架、模型引用全集及财务合并一致性，失败时通过 `params.reasons` 返回完整诊断；校验通过则生成暂存提案（不创建项目）。
2. **确认发布**：在单一数据库事务内新建目标项目、重建配置修订、数据集版本、项目模型与历史版本引用映射并发布提案，任一步骤失败整事务回滚，不产生半成品项目，已接受提案仍可重试。
