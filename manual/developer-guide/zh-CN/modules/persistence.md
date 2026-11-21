# 持久化

> 代码边界：各领域模块的 `persistence.py` / `tables.py` 与 `backend/iesplan/migrations/`；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

持久化层负责将领域状态可靠落库，并通过数据库约束保护数据唯一性、引用完整性与不可变性。每张数据库表和 Repository 属于唯一领域所有者，禁止作为跨模块共享表模型直接查询。

## 所有权与事务边界

- **表归属隔离**：每个领域仅访问自身表与 Repository（如身份归 `identity`，快照与任务归 `tasks`，证据索引归 `results`，对象引用归 `storage`）；跨领域视图数据在 `application` 层调用多个领域公开接口组装，禁止在 API 或其他领域直接跨表联查对方 ORM。
- **不可变保护**：已发布的项目版本、数据版本、配置修订、计算快照、执行证据与审计日志由数据库约束及触发器禁止原地更新或覆盖。
- **Repository 职责与事务规则**：Repository 负责 ORM 与领域契约映射、唯一键冲突处理（`upsert` 或局部 `savepoint`）与数据库异常分类；禁止在 Repository 内返回懒加载 ORM 对象图或执行全局 `commit`/`rollback`（完整事务生命周期只归 `application` 用例拥有）。
- **大对象分离**：二进制文件与大结果体不存入数据库业务表，统一写入 `storage` 并在数据库保留 `ObjectId` 及引用记录。

## 数据库变更与迁移规范

1. Schema 调整须归属明确领域并编写版本化 Alembic 迁移脚本，禁止在生产或运行时以 `create_all` 替代正式迁移。
2. 唯一性、外键关联、状态枚举与不可变性优先通过数据库约束与触发器保障。
3. 所有 Repository 与迁移测试必须在真实 PostgreSQL Docker 容器中验证升级、幂等重放、外键约束及并发冲突场景。
