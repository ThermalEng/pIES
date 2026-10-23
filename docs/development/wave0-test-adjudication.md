# Wave 0：测试裁决清单（一次性交接）

> 基线：`511685c`；状态：执行中、待 Codex 独立验收；本文件只服务本次收口，
> 全局验收通过后随指南删除，不形成长期清单。
> 效力顺序：宪法 → 生效公开契约/模块手册 → 本轮目标结构 → 当前代码 → 当前测试。

## 调用图结论（生产代码为准）

- `api → application 用例 → 领域公开门面 → 本领域 persistence → core` 主链成立；
  api 无直连 ORM（仅 Session 类型 + `Depends(get_db)`）。
- `computation/`、`bootstrap/` 不存在；`computation 阶段 gateway` 尚无生产实现。
- 旧 `engines` 计算模块（balance/devices/eval_run/planning/solver）的生产消费者为零；
  唯一残留生产引用是算法注册表（`calc_config.py` 的 `DEFAULT_ALGORITHM/AlgorithmSpec/
  get_algorithm/list_algorithms`、`builder10.py:545` 懒导入），删除时必须限于计算模块、
  保留注册表（或随 Wave 4 搬迁）。
- `worker/lease.py` 是无业务增量的薄转发；`worker/runner.py` 承担数据解释、SI 换算、
  时间轴物化；`executors.py` 的 calc/plan 等旧链已删（`NotImplementedError`），I/O 三 stub
  抛 `TASK-EXEC-001`，但测试侧尚无正断言覆盖。
- `models/calc.py:101,104` 同存 `assembly_text`（旧）+ `canonical_assembly_text`（新），
  生产只用后者；`tasks/contracts.py:4-5` 已声明旧字段不在契约。

## §4.1 删除（随旧实现删除，不得保留原实现）

- `test_balance.py`、`test_devices.py`（engines.devices 部分）、`test_eval_run.py`、
  `test_planning.py`、`test_solver.py`：只消费旧 engines，无生产消费者。
- `test_wave2_b_models.py::test_old_service_module_gone`：纯“旧文件已删除”断言，
  锁定历史布局而非稳定依赖规则（同文件 `test_new_modules_have_no_direct_models_imports`
  稳定门禁保留）。
- 有价值算法样例待 computation provider 实现时迁入，本轮不造包装器。

## §4.2 改写

- `test_worker_sessions.py` monkeypatch `executors.execute_calc` → 改注公开阶段 gateway，
  保留租约/短事务/取消/阶段顺序断言。
- `test_worker_outcome.py` → 接线改 gateway 注入；`TestUnimplementedIoNeverSucceeds`
  补 `==TASK-EXEC-001` 正断言（当前零正断言，属缺口非保留）。
- `test_tasks_api.py:273` 删除 `assembly_text is None` 双列断言，只验当前 schema。
- `test_model_template_api.py:554-567` 删除建 `legacy.db` 跑兼容迁移块。
- `test_wave1_assembly.py:68-93/153-165` 改测公共装配入口/现行 contract/依赖方向。
- `test_wave2_b_models.py:188-197` 保留新家归属前半，删旧导出后半断言并改名去实施史。
- integration“旧计算链已删除”文案断言：全仓零命中，改写义务为空。

## §4.3 保留

- devices 2.0 / assembly 1.0 / finance 1.0 / 项目包 1.0 现行契约测试；
  API 权限事务幂等；Worker 租约/fencing/取消/迟到写回/短事务行为测试；
  架构门禁稳定规则部分；`test_models.py` 新契约列集合与表注册断言；
  Sequence 递增断言（生产副作用）。

## 门禁调整

本轮门禁保持现状；Wave 5 统一收敛为稳定依赖规则门禁（不锁函数名/行号/白名单）。
