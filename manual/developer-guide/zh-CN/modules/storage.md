# 对象存储

> 文档状态：生效蓝图；代码边界：`backend/iesplan/storage/`

## 作用

`storage` 统一管理数据文件、逐时结果、证据和导出包等大型不可变内容。它把业务对象与文件系统/S3 等具体实现隔开，并保证内容摘要、引用、容量和清理始终可解释。

## 边界

模块负责：

- 内容寻址、摘要、大小和媒体类型；
- `ObjectId`、`ObjectHandle`、`ObjectOwner` 与公开 `ObjectStore` 协议；
- owner 引用的附加、解除、保留与清理；
- 对象清理的软删/保留期与恢复（待物理回收 → 恢复 → 到期物理回收）；
- BlobStore provider、完整性校验、容量和 reconciliation；
- 本模块健康状态。

模块不理解项目版本、数据集字段、计算指标或用户权限。业务模块决定“为什么拥有这个对象”，storage 只保证 owner 引用与字节生命周期一致。

## 对象清理恢复路径（软删/保留期）

管理员的对象清理为两阶段（先计划后执行），执行采用软删/保留期，不立即物理删文件：

- `safe_cleanup(dry_run=False)` 把无引用的孤儿对象标记为 `pending_deletion`，记录软删时间与保留截止（`pending_delete_until`）；
- 保留期默认 7 天；保留期内对象文件保留在磁盘、内容仍可读取；
- 保留期内可恢复：管理员经 `undelete_object` 恢复，或对象重新获得 owner 引用时自动恢复；
- 物理回收由 `purge_expired` 负责，只删除已过保留期的待回收对象；`reconcile` 巡检兜底执行；
- `list_pending_deleted` 列出“已删除待回收”对象，供管理员查看将被物理回收的对象。

设计意图：管理员是受信任运维角色，软删/保留期为明显危险的清理误操作提供延迟物理删除与恢复路径，不引入双人审批或重型回收站机制。

## 输入

| 输入 | 进入条件 |
|---|---|
| 不可变字节 | 媒体类型、大小上限和预期摘要明确 |
| `ObjectOwner` | namespace、owner ID 和 purpose 稳定 |
| 对象读取请求 | `ObjectId` 合法且调用方已在 application 完成授权 |
| provider 配置 | 根、凭证和能力由组合根注入 |

模块不接受业务模块拼好的磁盘路径，也不从 HTTP 请求直接推断 owner。

## 输出

`ObjectHandle` 至少包含对象 ID、摘要、大小、媒体类型和完整性相关元数据。其他公开输出包括 owner 引用、容量状态、校验报告与 reconciliation 结果。

返回 handle 不等于授予最终用户下载权限；授权和短期下载能力由 application/API 负责。

## 写入与引用流程

```text
校验容量与输入限制
    ↓
流式写入临时对象并计算摘要
    ↓
原子发布确定性内容对象
    ↓
幂等写入元数据
    ↓
application 在业务事务中附加 owner 引用
```

数据库与 BlobStore 不能伪装成一个 ACID 事务。任一阶段中断后，重复调用或 reconciliation 必须能识别已完成部分并安全收敛。

## 增加存储 provider

1. 实现字节级 put/get/stat/delete 和健康能力；
2. 明确原子发布、并发相同内容和一致性模型；
3. 不在 provider 内实现 owner、配额或业务保留规则；
4. 通过同一 ObjectStore 契约测试集；
5. 测试断电/异常后的临时对象、缺失对象和摘要损坏；
6. 由组合根选择 provider，业务代码不增加 provider 分支。

## 失败语义

- 容量未知或不足：拒绝新写入，公开 degraded/not-ready；
- 对象缺失：返回明确缺失，不回退同名旧文件；
- 摘要或大小不一致：标记损坏并阻止消费；
- attach/detach 重复：按公开幂等语义处理；
- 文件与元数据不一致：进入 reconciliation 报告，不静默删除；
- provider 不可用：保留外部故障原因，调用方决定重试。

## 必须遵循的规范

- 只有 adapter 解释具体存储路径；
- 引用清单是权威事实，`ref_count` 只能是缓存；
- 任一有效 owner 引用都阻止清理；
- 业务模块只保存对象 ID/handle，不保存 adapter 路径；
- 内容对象不可原地覆盖；
- 导出和临时文件必须纳入明确生命周期。

## 完成标准

- put/get/stat、重复写、并发写和摘要验证通过统一协议测试；
- attach/detach 对称，删除与保留边界有测试；
- reconciliation 覆盖有文件无元数据、有元数据无文件、损坏和超龄临时对象；
- 替换 provider 不修改项目、数据、结果和前端 contract；
- 故障不会返回空字节或伪成功 handle。

代码阅读从 storage contract 和公开门面开始，再进入服务、持久化与 adapter；对应测试以对象 API、存储服务和项目包测试为入口。
