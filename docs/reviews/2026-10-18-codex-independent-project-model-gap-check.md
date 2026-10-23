# 自建项目模型断点：独立核查

日期：2026-10-18；核查者：Codex；基线：主线 `974e1b7`。
性质：只读诊断，不授权开发，不修改里程碑、产品规范或现有业务代码。

## 结论

断点真实，但不是近期四批引入的回归，也不是整个计算链失效。此前测试分别证明“项目模型能保存”和“目录中已注册设备能求解”，未证明保存后的项目模型能进入画布及求解。把两类通过拼成完整自建模型交付结论属于验收推断过度。

## 独立 HTTP 实测

临时诊断文件：`/tmp/pies-codex-model-gap.y41UO8/test_codex_model_gap_probe.py`。
复用现有镜像 `pies014b2-backend:latest` 仅提供依赖，主线当前 backend 只读挂载为 `/app`；无网络、SQLite 内存库，现有测试 fixture 把对象数据放在容器临时目录。没有挂载用户数据，没有构建或删除镜像。

命令：

```sh
docker run --rm --pull=never --network none \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/app:/app/tests \
  -e IESPLAN_DB_URL=sqlite+pysqlite:// -e IESPLAN_QUEUE=memory \
  -v /home/mc/Documents/工作文档/pIES/backend:/app:ro \
  -v /tmp/pies-codex-model-gap.y41UO8:/probe:ro -w /app \
  pies014b2-backend:latest python -m pytest -q -s -p no:cacheprovider \
  /probe/test_codex_model_gap_probe.py
```

最终输出：

```text
SAVE=201 SAVED_ID=acme.device.heat_pump_1 LISTED=True PALETTE=False
USE=400 BODY={"error":{"code":"PROJ-CMD-005","message_key":"ies.diag.param.invalid","severity":"error","blocking":false,"params":{"device_type":"acme.device.heat_pump_1"},"location":null,"fix_hint_key":"","ref_ids":[]}}
REGISTERED_CONTROL=200
1 passed, 1 warning in 2.57s
```

注意：此处 PASS 表示诊断成功复现缺陷，不代表功能验收通过。相同项目/会话/revision 下，保存返回 ID 建模为 400，内置热泵类型对照为 200，排除了权限或修订号导致的假断点。探针首轮嵌套只读文件挂载失败，改为独立 `/probe` 只读目录后成功；没有把挂载失败误当业务结论。

## 为什么原有测试仍能通过

- `frontend/tests/e2e/model-new.spec.ts:167-176`：保存后检查 `_N`、revision 和“ready for assembly”提示，随后转到另一编辑/管理流程；没有把 `_N` 加入画布、装配或任务。填了 `data/e2e_load.csv`，但该场景未创建对应 CSV，因此也没有验证路径的真实读取。
- `backend/tests/test_project_model_save.py`：验证保存、编号、引用、事务和模板/直接 YAML 汇合；未将保存返回的设备 ID 用于画布建模命令。
- `frontend/tests/e2e/financial-scaffold.ts:100-126`：计算场景准备的是 `ies.device.grid_connection/electric_load/heat_pump` 等进程目录设备，不使用项目保存产生的 `_N`。
- 新外部设备验收通过挂载将设备放入启动目录注册表，证明的是内容扩展路径，同样不能替代项目私有模型的消费验收。

## 历史证据

- `git blame` 将 `application/projects/lifecycle.py:568-585` 的目录类型准入门追溯到 `c3c220b`（2026-09-18）；该提交中的函数和当前一致，仅向 `devices.get_device` 查询。
- “ready for assembly”页面断言源于 `f60335d`（2026-08-29），不是近期增加的真实下游验收。
- `git diff d2745fb..974e1b7 -- backend/iesplan/application/projects/lifecycle.py backend/iesplan/assembly/builder.py backend/iesplan/devices/registry.py` 为空。近期四批及其后的设备扩展验收没有改变这三处消费者。

## 范围判断

原四批针对既有前端契约家族、依赖归属及有限集成验收。记录明确限制为现有模板/项目模型响应家族收敛，并保持当前语义，而不是重新建设模型装配功能。

真正的流程偏离发生在 Codex 将外部扩展测试发现的目标版本差距继续展开为模板语义，再把项目模型消费接线自动批准为当前前置开发。已发现问题值得记录，但修复优先级与是否重开完成标准必须独立裁决，不能由执行者的依赖清单替代原计划。

已有四批工作和计算实测不因这个断点全部作废；但此前 0.14 收口不能作为“自建模型到计算已完整交付”的证据。现维持执行者暂停、保留历史与现场，不继续实施接线或 schema 扩张，不自行回滚已有提交。
