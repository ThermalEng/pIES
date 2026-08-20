# 开发、测试与文档

> 状态：生效

## 环境

项目依赖、编译、测试、格式化和数据库验证只能在 Docker 中运行。不得在主机安装项目依赖；只允许读写本仓库和 `/tmp`。

常用命令：

```bash
docker compose build backend web
docker compose run --rm backend pytest -q
docker compose up -d --build
```

实际服务名以 `docker-compose.yml` 为准。

## 变更规则

1. 先阅读[架构宪法](ARCHITECTURE_CONSTITUTION.md)。
2. 定义或更新公开 contract。
3. 只通过允许的模块边界实现。
4. 为正常、错误、事务和恢复路径增加测试。
5. 同步更新正式指南和 `docs/` 过程材料。
6. 在 Docker 中运行与风险相称的验证。

repository 和领域服务不得提交或回滚调用方事务。API 不直接查询 ORM。前端页面不直接调用底层 HTTP，DTO mapper 必须是纯函数。

## 文档

- `manual/user-guide/`：用户可见功能和操作；
- `manual/developer-guide/`：稳定公共契约、扩展和维护知识；
- `docs/`：当前规格、方案、审查、调研和迁移记录。

成熟结论应经过整理后提炼到正式指南，不能复制过程文档形成双事实源。帮助中心直接渲染 `manual/` 下的 Markdown，因此标题、链接、代码块和目录顺序必须可被网页渲染器处理。

## 完成标准

公开契约、权限、失败语义、迁移、前端 mapper、Docker 测试和相应文档全部完成后，功能才算完成。现有测试行为不能凌驾于架构宪法。
