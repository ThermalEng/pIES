# API 适配

> 代码边界：`backend/iesplan/api/`；传输规范：[公共契约](../contracts.md)

`api` 是纯粹的 HTTP 传输适配层，负责把路由参数、Cookie 认证上下文与请求载荷翻译为应用用例命令，并将应用结果或异常映射为符合公共契约的 HTTP 响应。判断路由职责是否干净的标准是：**将同一用例切换为 CLI 或后台命令调用时，无需从路由中抽离任何业务逻辑**。

## 职责与边界

- **负责**：FastAPI 路由定义、HTTP 方法与媒体类型/大小限制；认证依赖解析（生成 `ActorContext`）；Pydantic 请求/响应 DTO 校验；统一顶层命名包装与标准错误信封转换；按会话与项目权限校验归属后的受控流式下载响应。
- **不负责**：直接导入或查询 ORM 模型、拼接对象存储路径、在路由内执行 `commit`/`rollback`、串联多个应用用例、或返回新旧兼容并集响应。

## 请求处理流

```text
HTTP 请求
  ↓ Pydantic DTO 结构校验（类型、必填项、禁止未声明字段）
  ↓ 认证依赖 → 构建 ActorContext（不把原始凭证传入业务层）
  ↓ 构造命令并调用单个 application 用例
ApplicationResult / AppError
  ↓ 顶层命名包装序列化（OpenAPI / response_model）或标准错误信封映射
HTTP 响应（200 / 201 / 202 / 204 / 4xx / 500）
```

## 开发与校验规范

- **统一包装与状态码**：成功 JSON 响应使用语义化顶层键（列表使用 `{items, total}` 或 `{items, next_cursor}`），禁止 `{data, meta}` 通用包装，具体结构由 FastAPI `response_model` 与端点契约测试保障；`400` 用于业务规则失败，`422` 用于请求结构或 Schema 不可处理，`409` 用于资源重名或同版本冲突等业务状态冲突（完整定义见[公共契约](../contracts.md#http-语义)）。
- **字段与精度安全**：统一 `snake_case`，ID 为不透明字符串，精确金额采用十进制字符串，禁止暴露内部路径、堆栈或 SQL 细节。
- **OpenAPI 为唯一字段源**：新增或修改端点时先对齐应用用例与 Pydantic `response_model`，由运行程序自动生成 OpenAPI 文档并补充权限、状态码、错误信封与关键数值往返测试。
