# 项目模型及项目包：主线集成收口

日期：2026-10-18；执行及复审：Codex；主线原基线 `d7d6921`，合入分支 `feat/package-project-model-deps`（`e081536`）。本文件为忽略的本地证据，不定义产品规范。

## 精确修改

- precheck 冲突合并保留项目文档解析与 CSV 来源映射公开解析两侧行为；builder 自动合并后确认两侧能力均保留。
- 更新日志保留两分支已接受条目。
- test_source_declaration.py 与 test_task_config_revision.py 的两处旧调用改为显式关键字 `device_documents={}`, `device_name="load1"`，不改生产签名、不增加兼容分支。
- 前端实现/浏览器规格相对已复审切片没有另行修改，既有浏览器证据复用，不重复跑。

## Docker 验证

复用已有 `pies014b2-backend:latest` 仅作依赖环境，当前集成 backend 只读挂载 `/app`；`--network none`，SQLite 内存库/内存队列，测试 fixture 使用容器临时数据，无用户数据挂载、不建新镜像。

```sh
docker run --rm --name pies-codex-model-integration --pull=never --network none \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/app:/app/tests \
  -e IESPLAN_DB_URL=sqlite+pysqlite:// -e IESPLAN_QUEUE=memory \
  -v /home/mc/Documents/工作文档/pIES/backend:/app:ro -w /app \
  pies014b2-backend:latest python -m pytest -q -p no:cacheprovider \
  tests/test_source_declaration.py tests/test_task_config_revision.py \
  tests/test_project_model_consumption.py tests/test_package_project_models.py
```

结果：`80 passed, 56 warnings in 61.08s`，exit 0。警告为 TestClient 弃用与既有模板循环外键排序，不伪称零告警。

反例对照：同镜像参数只读挂载未修的 `.claude/worktrees/pies-package-model-deps/backend`，运行 `tests/test_task_config_revision.py::test_nonfinite_predefined_constant_rejected_at_entry`，结果 `1 failed, 2 warnings in 1.89s`，exit 1。实际异常 `AttributeError: 'str' object has no attribute 'get'`，`device_documents='load1'`，证明不是推测或元数据收集失败。

容器均随 `--rm` 退出，不创建镜像、不清理任何其他 Docker 资源。未运行全仓、未推送。测试修正与冲突合并只闭合当前集成问题，不宣告 0.15 整体完成。
