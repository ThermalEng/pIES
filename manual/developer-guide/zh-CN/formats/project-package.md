# 项目包

> 契约标识：`ies.project-package`；目标 `format_version`：`2.0.0`；包文件：`*.zip`（`manifest.json` 在根）。
> 文档状态：生效目标契约；只定义目标文件语义，不声明实现进度。`1.x` 不再保留解析路径，主版本不兼容。

项目包是项目的离线交换载体：当前工作状态 + 包内历史版本 + 恢复它们所需的精确依赖。账号、权限、会话、密钥、宿主机路径、ORM 字段不得入包；来源数据库标识（如 `dataset_id`、`dataset_version_id`、配置 revision 号）仅作为包内映射标识出现，确认导入时一律映射为新身份，不得直接复用为目标库身份（通用书写规则见[文件格式标准](../file-formats.md)）。

## 布局

```text
manifest.json
project.json
draft.json
finance_profile.yaml            # 当前工作状态的财务 Profile（正式财务契约）
finance_overrides.yaml          # 当前工作状态的 Overrides（无覆盖时为显式空文档）
effective_finance.yaml          # 当前工作状态的 Effective（合并器产物，不可人工改写）
planning_config.yaml            # 当前工作状态的规划配置（当前无规划时缺席）
versions/0001.json …            # 包内历史版本（内容自包含，引用见下）
datasets/{dataset_id}/v{version_no}/dataset.json    # 每个数据版本独立元数据条目
datasets/{dataset_id}/v{version_no}/{file_kind}.csv # 该版本数据文件
evidence/{package_id}.json …    # 历史结果证据（只登记引用，不伪造本地任务）
version_configs/{NNNN}/*.yaml   # 每个历史版本恢复所需的精确配置依赖（见下）
```

`manifest.json` 形状：`{format_version, package_type: "project", objects: [{path, size_bytes, media_type}], files: {project, draft, configs, versions[], datasets[], evidence[], version_configs}}`。对象清单与包内文件一一对应（除清单本身），大小一致；重复路径拒绝。

## files.version_configs（唯一形状）

每个包内历史版本 `versions/NNNN.json` 若其内容带 `effective_finance` / `planning_config` 引用块，则 `files.version_configs` 必须有同名键 `NNNN`，值为该版本恢复所需的依赖文件映射：

```json
"version_configs": {
  "0001": {
    "finance_profile": "version_configs/0001/finance_profile.yaml",
    "finance_overrides": "version_configs/0001/finance_overrides.yaml",
    "effective_finance": "version_configs/0001/effective_finance.yaml",
    "planning_config": "version_configs/0001/planning_config.yaml"
  }
}
```

- 四个文件分别使用正式财务/规划 YAML 契约，内容为该版本冻结引用指向的**原行内容原样**（Profile 稳定来源、Overrides/Effective/Planning 各自 revision 行）。
- 键按需缺席：版本内容无 `effective_finance` 块则前三个键缺席，无 `planning_config` 块则第四个键缺席；缺席保持缺席，导入后恢复也不建立对应指针。
- 来源键：版本内容 `effective_finance` 块的 `{profile_id, overrides_revision, revision}` 与 `planning_config` 块的 `{revision}` 即来源键，只用于在包内定位上述文件，**不作为目标数据库的身份或修订号**。
- Profile 自包含：每组财务依赖携带其精确来源的 `FinanceProfile` 文件；确认导入时先注册包内来源（同稳定 id 复用既有行），内容不一致即身份冲突、明确拒绝。绝不用当前 `finance_profile.yaml` 冒充历史，也绝不用目标库同名或最新 Profile 替代包内来源。
- 当前工作状态指向顶层 `finance_*.yaml` / `planning_config.yaml` 这一组（`files.configs`），与 `version_configs` 无关；导入后当前指针按该组重建，不隐式变成任一历史版本的配置。

## 例子（片段，非可导入包）

以下片段只示意关键条目的形状，不构成完整可导入包（完整包还须含 `draft.json`、全部对象条目、各版本全部依赖文件等）：

```json
// manifest.json（片段）
{
  "format_version": "2.0.0",
  "package_type": "project",
  "objects": [
    {"path": "project.json", "size_bytes": 412, "media_type": "application/json"},
    {"path": "datasets/7/v2/dataset.json", "size_bytes": 388, "media_type": "application/json"},
    {"path": "datasets/7/v2/data.csv", "size_bytes": 1024, "media_type": "text/csv"},
    {"path": "version_configs/0001/finance_profile.yaml", "size_bytes": 190, "media_type": "application/yaml"},
    {"path": "version_configs/0001/finance_overrides.yaml", "size_bytes": 210, "media_type": "application/yaml"}
  ],
  "files": {
    "project": "project.json",
    "draft": "draft.json",
    "configs": {"finance_profile": "finance_profile.yaml"},
    "versions": ["versions/0001.json"],
    "datasets": ["datasets/7/v2/dataset.json"],
    "evidence": [],
    "version_configs": {"0001": {
      "finance_profile": "version_configs/0001/finance_profile.yaml",
      "finance_overrides": "version_configs/0001/finance_overrides.yaml"
    }}
  }
}
```

```json
// datasets/7/v2/dataset.json（节选）：dataset_version_id 精确到版本
{
  "dataset_id": 7,
  "dataset_version_id": 19,
  "dataset": {"name": "负荷数据", "status": "published"},
  "version": {"version_no": 2, "timeline": "hourly", "resolution": "1h"},
  "files": [{"file_kind": "data", "format": "csv", "row_count": 8760, "size_bytes": 1024}]
}
```

## 导入语义（实现约束摘要）

确认导入时先建立来源到新项目的显式映射（配置按 `(kind, 来源 revision)` → 新 revision 行，数据版本按来源 id → 新版本 id，均用新编号，不复用来源数字），改写新版本内容引用后再发布版本；原项目、原版本、原历史行一律不变。映射缺失（包内无对应依赖）则拒绝确认，不回填、不默认。

## 格式变更决策记录

- `1.0` → `2.0.0`（主版本）：数据集元数据路径从按数据集唯一改为按数据版本唯一；新增 `files.version_configs` 携带历史精确依赖；导入不再解析 `1.x`。旧包（仅当前配置、无多版本数据集）不在新读取器下重释，多版本 `1.0` 包本就无法通过上传校验、无有效存量。
- 否决：保留 `1.x` 解析双轨（多版本旧包无有效语义，兼容即伪装）；把所有历史版本指向当前重建 revision（即本次修正的静默错值）；为缺失依赖静默回填当前值。
