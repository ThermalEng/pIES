# 建模 feature(features/modeling)

模型模板管理、模板表单、直接 YAML 编辑、诊断与保存状态 ——「新建并保存项目模型」工作流
(见 `manual/developer-guide/zh-CN/frontend.md`「新建并保存项目模型」)。

## 分层

```text
pages/model/NewModelPage.tsx        # 路由级组合(模板页签 + YAML 页签 + 保存状态)
  └─ features/modeling/
       ├─ contracts.ts              # 与后端 HTTP JSON 一一对应(snake_case)
       ├─ model.ts                  # 前端领域模型 + 保存状态机 + 诊断
       ├─ form.ts                   # 未提交表单状态(number 以文本保存等)
       ├─ mappers.ts                # 纯转换(表单↔DTO / DTO↔模型), 全部纯函数
       ├─ api.ts                    # 本 feature 的后端调用(契约草案)
       ├─ hooks/                    # 查询 / 表单 / 候选保存用例
       └─ components/               # 列表 / 递归表单 / YAML 编辑器 / 状态 / 诊断 / 结果
```

依赖方向: `pages → features → shared(api/client + types + i18n)`。
页面与组件不直接拼请求 JSON; mapper 不访问网络、缓存或 React 状态。

## 保存状态机

| 状态 | 含义 |
|---|---|
| `editing` | 编辑中: 候选内容在本地表单, 未提交; 数据文件仅临时隔离区 |
| `temporary_uploaded` | 临时已上传: 至少一个 data 字段上传了临时文件(≠ 模型已保存) |
| `validating` | 校验中: 已提交候选, 等待后端完整校验 |
| `validation_failed` | 校验失败: 后端聚合诊断展示, 输入保留, 未分配编号, 未保存 |
| `saved` | 正式已保存: 以后端返回的最终 `_N` ID / 规范 YAML / 摘要 / 项目 revision 替换编辑状态 |

前端不预分配 `_1`/`_2` 编号; 只有 `saved` 才进入项目模型列表并允许进入装配。
传输/服务器错误与"校验不过"区分: 前者保持 `editing` 并展示重试入口, 不显示保存成功。

## 后端联调状态(切片 dm2 已完成)

候选校验/保存与模板目录/详情均对真实后端逐字段对齐(见 `contracts.ts`), 端点清单:

| 端点 | 请求 | 成功 | 失败 |
|---|---|---|---|
| `GET /api/model-templates/catalog` | — | `{items: [TemplateSummary]}` | 错误信封 |
| `GET /api/model-templates/{id}` | — | `{template, document, diagnostics}` | 错误信封 |
| `POST /api/projects/{pid}/models/validate` | `{source, model_yaml?, template_id?, template_revision?, template_sha256?, template_inputs?}` | `{valid, diagnostics}` | 错误信封 |
| `POST /api/projects/{pid}/models` | 同上 + `{expected_revision, idempotency_key, data_files}` | `201 {project_model, receipt, project_revision}` | `400` 错误信封, `params.diagnostics` 为聚合诊断 |
| `POST /api/projects/{pid}/models/temp-files` | multipart `file` + `data_ref` | `201 {temp_file, upload_id}` | 错误信封 |

e2e 场景 12 跑真实后端全链路(模板目录 → 表单 → 数据文件内容锁失败诊断 →
修正 → 保存 `_N` → YAML 直接保存), 不再使用 `page.route` mock。

## 已知边界

- 数组元素的嵌套 object/array 结构不在表单编辑器支持范围, 明确提示走直接 YAML
  编辑(不静默丢弃);
- 模板详情中的 `document` 以已解析的嵌套 JSON 对象传输(后端门面解析), 表单回显/
  提交只消费已解析的 `inputs` 树与 `raw` 引用;
- 数据文件上传为临时隔离区占位: `data_files` 携带 `{data_ref, upload_id,
  object_id, sha256}`, 由后端在完整校验阶段做内容锁绑定(摘要不匹配 → 400 聚合
  诊断, 数据区校验按基础文档、落盘字节绑定最终 `_N` 模型)。
