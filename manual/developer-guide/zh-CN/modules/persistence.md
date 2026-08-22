# 持久化

> 文档状态：生效蓝图；代码边界：各领域 repository、`backend/iesplan/models/` 与 migrations

## 作用

持久化层把领域事实可靠地保存到数据库，并用约束保护唯一性、引用、状态转换和不可变性。它是领域模块的适配器，不是跨模块共享数据模型。

当前 ORM 物理上集中在 `models/`，只是迁移状态；开发者不能因此把所有表视为可从任意模块直接查询的公共接口。

## 所有权与边界

每张表和 repository 都必须有唯一领域所有者。例如身份事实属于 identity，用于计算的快照和任务事实属于 tasks，证据索引属于 results，对象元数据与引用属于 storage。

repository 负责：

- 领域需要的查询和持久化命令；
- ORM 与领域 contract/read model 的映射；
- flush、savepoint、锁和数据库错误分类；
- 依赖数据库才能保证的约束。

repository 不负责 HTTP DTO、权限、跨领域编排和顶层 commit/rollback。其他模块不能导入其 ORM 或拼跨领域查询。

## 输入与输出

| 输入 | 进入条件 | 输出 |
|---|---|---|
| 领域实体/持久化命令 | 已通过领域校验 | 新 revision、实体 contract 或写入回执 |
| 查询条件 | 使用领域 ID 与稳定语义 | read model、实体或明确不存在 |
| 当前事务/session | 由 application 用例拥有 | flush 后的事务内状态 |
| migration | 版本、前置条件和回滚策略明确 | 可验证的新 schema 版本 |

repository 不向调用方返回懒加载 ORM 图；输出必须在 repository 生命周期之外仍有明确含义。

## 写入思路

```text
application 开启事务
    ↓
领域规则生成持久化命令
    ↓
repository 写入 / flush / 必要时 savepoint
    ↓ 数据库约束验证
repository 返回领域结果
    ↓
application 协调其他模块并最终 commit 或 rollback
```

唯一键竞争优先使用 upsert 或局部 savepoint。下层捕获约束错误时只回滚局部操作，不能调用全局 rollback 破坏调用方已经完成的其他步骤。

## 查询思路

模块内部查询返回领域实体或 read model。跨领域页面需要组合数据时，由 application query 调用多个公开 repository/read model，再形成应用结果；不能让 API 写一条跨所有表的 ORM 查询作为新的隐式领域层。

大型字节不存入普通业务表，由 storage 管理；数据库只保存对象 ID、摘要、owner 引用和必要索引。

## 修改 schema

1. 确定变更所属领域和产品版本影响；
2. 更新领域 contract 和 repository 语义；
3. 设计版本化 migration、数据转换和失败回滚；
4. 尽可能用外键、唯一、检查约束和 trigger 保护不变量；
5. 在真实 PostgreSQL Docker 环境验证升级、重复执行和回滚；
6. 更新备份恢复、快照或导入格式的迁移说明；
7. 禁止以运行时 `create_all` 代替正式 migration。

## 失败语义

- 唯一冲突：映射为领域冲突或幂等已有结果；
- 外键/状态约束失败：视为领域或并发冲突，不返回伪成功；
- 数据库不可用：事务失败并影响 readiness；
- 乐观锁 revision 不匹配：返回当前 revision，不静默覆盖；
- migration 前置条件不满足：停止升级并保留可恢复状态；
- ORM 映射意外失败：记录追踪，不能返回半填充实体。

## 必须遵循的规范

- application 是 commit/rollback 所有者；
- ORM 只在领域持久化边界内传播；
- 跨领域访问通过公开 repository/read model/application query；
- 不可变证据、审计和已发布版本由数据库约束防止更新；
- 时间戳、Decimal、ID 和枚举遵循公共 contract；
- schema 和数据迁移都具有明确版本，不依赖开发者手工 SQL。

## 完成标准

- repository 的正常、未找到、唯一冲突和并发冲突有测试；
- 关键不变量同时有领域测试和 PostgreSQL 约束测试；
- migration 可从支持的上一版本升级，并能在失败后安全恢复；
- API、Worker 和其他模块不直接导入领域 ORM；
- 删除、软删除、保留和对象引用生命周期一致。

阅读代码时先确认表和 repository 的领域所有者，再从公开 repository 行为进入 ORM；`models/` 的物理位置不能替代所有权判断。数据库测试必须在 Docker 的 PostgreSQL 环境运行。
