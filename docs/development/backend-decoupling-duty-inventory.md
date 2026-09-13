# 后端依赖/职责清单（R2 第二次验收纠偏，源码生成）

生成方式：标准库 AST 扫描 `backend/iesplan` 顶层导入边；只记录包级事实，
不含行号。核对点：门禁 `tests/test_architecture_gates.py`（23 项）与各行为测试。

## 一、职责归属（指南 A～G 关闭点）

- API 每个业务动作只调用一个完整 application 用例 → `application 各用例包`（门禁13(空集)＋行为测试）
- 组合授权唯一生产实现 → `application.projects.authorization.ensure_access`（门禁19）
- Worker 运行编排／不解释结果／未实现不成功 → `worker→application.worker 分阶段命令`（门禁15/20＋test_worker_outcome）
- 用户名/邮箱规则 → `identity.contracts`（门禁18）
- 幂等键规则 → `tasks.contracts`（门禁18）
- 业务结局集合 BUSINESS_OUTCOMES → `tasks.contracts`（runner/complete_task 显式门）
- 检查结局 check_outcome → `results.rules`（Worker 只消费）
- 证据结构校验 → `results.rules.validate_evidence_structure`（application 只做跨域一致性）
- 四维摘要 → `results.rules.summarize_assessment`（application 不直调 metrics）
- 结果采用补丁 → `证据原生契约 capacities`（不经 engines 静态映射）
- core → `无领域业务规则`（门禁1/18）

## 二、包级依赖边（application/api/worker → 领域）

### application
- → `assembly`：1 个模块
- → `audit`：2 个模块
- → `config`：4 个模块
- → `configuration`：3 个模块
- → `core`：53 个模块
- → `dataset`：5 个模块
- → `devices`：9 个模块
- → `engines`：1 个模块
- → `finance`：2 个模块
- → `identity`：21 个模块
- → `model`：4 个模块
- → `package`：4 个模块
- → `planning`：2 个模块
- → `project`：10 个模块
- → `results`：4 个模块
- → `storage`：16 个模块
- → `tasks`：9 个模块

### api
- → `config`：3 个模块
- → `core`：10 个模块
- → `db`：15 个模块
- → `identity`：1 个模块
- → `tasks`：1 个模块

### worker
- → `config`：1 个模块
- → `core`：6 个模块
- → `db`：1 个模块

## 三、领域间直接依赖（应仅剩 contracts／常设豁免）

### results
- results/rules.py → `iesplan.metrics.financial`
- results/rules.py → `iesplan.metrics.validity`

### engines
- engines/planning.py → `iesplan.metrics.financial`

### assembly
- assembly/builder.py → `iesplan.devices`
- assembly/builder10.py → `iesplan.devices`
- assembly/builder10.py → `iesplan.engines.registry`
- assembly/context.py → `iesplan.devices`
- assembly/validator.py → `iesplan.devices`

（空即无领域间行为直调；metrics 纯函数复用见门禁常设豁免。）
