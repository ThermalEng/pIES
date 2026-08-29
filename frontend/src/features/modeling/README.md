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

## 待 C 合并后联调

候选校验/保存端点由阶段 2 worktree C 开发中(尚未合并)。本切片按 C 的契约草案
实现 `api.ts` 与 `contracts.ts`, 端点清单:

| 端点 | 请求 | 成功 | 失败 |
|---|---|---|---|
| `GET /api/model-templates` | — | `{items: [TemplateSummary]}` | 错误信封 |
| `GET /api/model-templates/{id}` | — | `{template, document}` | 错误信封 |
| `POST /api/projects/{pid}/model-candidates` | `{source: template\|yaml, template_id?, inputs?, content?, project_revision, idempotency_key, temp_file_refs}` | `201 {model, project_revision}` | `400` 错误信封, `params.diagnostics` 为聚合诊断 |
| `POST /api/projects/{pid}/temp-files` | multipart `file` | `201 {temp_file_ref, file_name}` | 错误信封 |

C 合并后的联调步骤:

1. 对照 C 的实际 OpenAPI 核对 `contracts.ts` 字段(应已一致, 如有差异只改
   `contracts.ts` + `api.ts` 的解析, 页面与 mapper 不依赖后端字段名);
2. 删除 `frontend/tests/e2e/model-new.spec.ts` 中的 `page.route` mock(改为跑真实
   后端), 并把模板 fixtures 换成后端种子模板;
3. 运行 `frontend/tests/modeling_mappers.test.mjs` 与 e2e 场景 12 确认全链路。

## 已知边界

- 数组元素的嵌套 object/array 结构不在表单编辑器支持范围, 明确提示走直接 YAML
  编辑(不静默丢弃);
- 模板详情中的 `document` 以原始 JSON 透传, 表单回显/提交只消费已解析的
  `inputs` 树与 `raw` 引用;
- 数据文件上传为临时隔离区占位: `temp_file_refs` 携带 `{path, temp_file_ref}`
  映射, 由后端在完整校验阶段绑定 `data_ref` 内容。
