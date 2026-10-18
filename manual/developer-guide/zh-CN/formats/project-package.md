# 项目包

> 契约标识：`ies.project-package`；目标 `format_version`：`3.0.0`；包文件：`*.zip`（`manifest.json` 在根）。
> 文档状态：生效目标契约；只定义目标文件语义，不声明实现进度。`1.x` / `2.x` 不再保留解析路径，主版本不兼容。

项目包是项目的离线交换载体：当前工作状态 + 包内历史版本 + 恢复它们所需的精确依赖（含项目已保存模型）。账号、权限、会话、密钥、宿主机路径、ORM 字段不得入包；来源数据库标识（如 `dataset_id`、`dataset_version_id`、配置 revision 号）仅作为包内映射标识出现，确认导入时一律映射为新身份，不得直接复用为目标库身份（通用书写规则见[文件格式标准](../file-formats.md)）。

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
models/{来源 device_id}.json     # 草稿与历史版本引用的项目模型规范文本（见下）
```

`manifest.json` 形状：`{format_version, package_type: "project", objects: [{path, size_bytes, media_type}], files: {project, draft, configs, versions[], datasets[], evidence[], version_configs, models[]}}`。`files` 必须为映射；对象清单与包内文件一一对应（除清单本身），大小一致；重复路径拒绝。清单、内容文档与携带模型文本的结构非法一律在**上传入口**给可定位诊断（`params.reasons`）并拒绝整个导入，不静默归一为空集、不落内部错误。

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

## files.models（唯一形状）

包内草稿与包内全部历史版本内容引用的**每个**项目模型都必须随包携带规范模型文本；携带集合与引用集合必须恰好相等——内容引用了未携带的模型 → 拒绝导入；携带了内容未引用的模型 → 拒绝导入。仅被历史版本引用（当前草稿已不引用）的模型同样携带。

```json
"models": [
  {
    "device_id": "acme.device.load_1",
    "base_device_id": "acme.device.load",
    "model_text": "models/acme.device.load_1.json"
  }
]
```

- 每个条目一个文件 `models/{来源最终 device_id}.json`，内容为该模型行**已冻结的规范模型文本原样**（紧凑 JSON、稳定键序；不是用户提交的原始 YAML，也不是模板实例化中间产物），条目按来源 suffix（编号）升序排列。
- `device_id`（来源最终 ID）与 `base_device_id`（来源基础 ID）只用于在包内定位来源与重建，**不作为目标数据库的身份或编号**；导入时按 `base_device_id` 经正式保存路径重建本项目模型行，分配本项目新编号（新 suffix、新行 id，编号不复用来源数字）。
- 导入重建后，草稿与每个历史版本内容里的 `project_models` 引用及其 `model` 节设备实例的 `device_type` 串，一律映射为重建后的真实新 ID（不假设编号与来源相同；源项目删除过模型形成的编号空号不影响映射）。历史版本内容按来源内容逐一映射；导入草稿的引用由保存路径按本项目重建后的清单重建，等于包内携带的全部模型集合（与项目内保存/删除模型的引用重建规则一致）。
- 模型回执（receipt）与来源溯源不随包携带：导入重建时由正式保存路径对规范文本重新生成回执，重建行来源记为 `direct_yaml`；包内规范文本即该模型的内容事实，源项目的模板来源标签不作为目标库身份或溯源。
- 模型正文的正式校验在确认导入时由正式保存路径一次完成（候选校验 → 新编号 → 规范化 → 回执）；提案阶段只校验依赖携带关系与消费结构（`files` / `files.models` 与内容 `project_models` 引用的形状、`model_text` 文件在包内且能解码为带 `device` 节的对象、内容 `model` 节对象 / `devices` 列表 / 设备条目对象），不重复跑模型语义校验。内容消费结构校验先于历史配置依赖等其他内容消费者执行；字段缺席与显式 `null` 区分——缺席按现有契约处理，字段存在时严格要求上述形状。结构非法 → 上传入口拒绝；正文非法 → 确认导入经该权威路径拒绝并整体回滚（无新项目与模型行、提案不进入已应用状态）。
- 历史结果证据里冻结的设备类型串属于证据原文，不改写。

## 例子（片段，非可导入包）

以下片段只示意关键条目的形状，不构成完整可导入包（完整包还须含 `draft.json`、全部对象条目、各版本全部依赖文件等）：

```json
// manifest.json（片段）
{
  "format_version": "3.0.0",
  "package_type": "project",
  "objects": [
    {"path": "project.json", "size_bytes": 412, "media_type": "application/json"},
    {"path": "datasets/7/v2/dataset.json", "size_bytes": 388, "media_type": "application/json"},
    {"path": "datasets/7/v2/data.csv", "size_bytes": 1024, "media_type": "text/csv"},
    {"path": "models/acme.device.load_1.json", "size_bytes": 320, "media_type": "application/json"},
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
    }},
    "models": [{
      "device_id": "acme.device.load_1",
      "base_device_id": "acme.device.load",
      "model_text": "models/acme.device.load_1.json"
    }]
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

确认导入时先建立来源到新项目的显式映射（配置按 `(kind, 来源 revision)` → 新 revision 行，数据版本按来源 id → 新版本 id，项目模型按来源 device_id → 新项目模型行身份，均用新编号，不复用来源数字），改写新版本内容引用后再发布版本；原项目、原版本、原历史行一律不变。映射缺失（包内无对应依赖）则拒绝确认，不回填、不默认。

## 格式变更决策记录

- `2.0.0` → `3.0.0`（主版本）：新增 `files.models` 携带草稿与历史版本引用的项目模型依赖，导入时经正式保存路径重建并映射引用。同一输入（内容引用了项目模型的包）在 `2.0.0` 下被接受但副本消费源项目模型行（跨项目身份泄漏、源项目删除模型即断链），在 `3.0.0` 下要么携带并映射、要么拒绝导入——旧消费者无法安全忽略该依赖，故按主版本升级；导入不再解析 `2.x`。
- `1.0` → `2.0.0`（主版本）：数据集元数据路径从按数据集唯一改为按数据版本唯一；新增 `files.version_configs` 携带历史精确依赖；导入不再解析 `1.x`。旧包（仅当前配置、无多版本数据集）不在新读取器下重释，多版本 `1.0` 包本就无法通过上传校验、无有效存量。
- 否决（`2.0.0` → `3.0.0`）：把包内模型行 id 复用为目标身份；让副本继续消费源项目模型行（本决策修正的泄漏）；模型依赖缺失时静默清空内容引用；把携带模型推迟为可选扩展而保持 `2.x`（旧消费者会把无依赖的包当有效包消费）。
- 否决：保留 `1.x` 解析双轨（多版本旧包无有效语义，兼容即伪装）；把所有历史版本指向当前重建 revision（即本次修正的静默错值）；为缺失依赖静默回填当前值。
