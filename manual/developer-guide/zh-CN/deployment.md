# 部署与运行

> 文档状态：生效蓝图；规范版本：1.0.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

## 环境边界

pIES 的依赖安装、编译、测试、格式化、数据库验证和浏览器验收只能在 Docker 中运行。主机只提供 Docker、仓库和受控数据目录，不安装项目 Python 或 Node 依赖。

## 开发部署

在仓库根目录执行：

```bash
docker compose up -d --build
```

默认 Web 入口为 `http://localhost:8080`。查看运行状态时使用 Docker/Compose 提供的日志与健康信息，不在主机直接运行应用进程。

## 开发者日常流程

```text
确认当前版本与 Roadmap 目标
  ↓
构建受影响的 Docker 服务
  ↓
运行模块/契约测试
  ↓
启动完整依赖并检查 readiness
  ↓
运行浏览器或人工验收
  ↓
停止测试服务并保留必要证据
```

只修改纯文档时，至少构建帮助中心并检查目录、链接和 Markdown；修改后端模块时先运行模块测试，再运行与其相邻的 application/API/Worker 测试；修改数据库或对象生命周期时必须使用真实 PostgreSQL 和受控对象目录验证恢复。

Compose 服务承担不同职责：Web 提供静态前端，backend 处理同步 HTTP，compute/I/O Worker 执行相应任务，PostgreSQL 保存权威事实，Redis 保存可重建队列与进度。某个服务能够启动，不代表整体已经 ready。

## Solver 执行隔离

计算 Worker 的 SolverRuntime 必须使用部署批准的 ExecutorProvider。部署配置负责把稳定 executable ID 映射到固定 solver 镜像或二进制，并声明参数/环境 allowlist、CPU、内存、运行时间、文件大小、进程数和网络策略；这些宿主机细节不得写入装配 YAML 或 Solver Bundle。

普通 solver 默认断网，运行身份不能访问业务数据库、对象存储凭证和仓库工作区。每个 attempt 使用独立可写目录，输入只读，输出只允许 manifest 声明的相对路径。取消、超时和租约失效后必须终止并回收完整进程组。

readiness 至少核验 executor 隔离能力、solver 精确版本和最小自检 Bundle；不能仅因宿主机存在同名命令就视为可用。真实 solver 的生成/执行集成测试同样只在 Docker 中运行。

## 生产安全基线

生产环境至少完成：

- 设置高强度、唯一的 `IESPLAN_SECRET_KEY`；
- 设置随机初始管理员密码并在首次登录后修改；
- 修改数据库凭证；
- 在可信反向代理或入口层启用 HTTPS；
- 限制数据库、缓存、对象存储和管理入口的网络访问；
- 明确数据目录、容量配额、备份周期和恢复责任人；
- 禁止把真实秘密写入镜像、仓库、普通日志或帮助文档。

开发默认值不能用于生产。

## 健康与就绪

存活只表示进程存在；就绪表示实例具备承接请求或任务的必要依赖。数据库、存储、必需 provider、generator、executor、solver、result adapter 或 Worker 命令不可用时，实例必须拒绝相应工作，不能以“降级”掩盖错误结果。

管理员界面可以查看基础健康和存储容量。更详细的运维检查应由部署平台完成，并关联请求、任务和执行尝试的追踪标识。

排查时先看公开健康层级：

1. 容器是否存活；
2. 进程 `healthz` 是否响应；
3. `readyz` 中哪个必需依赖不可用；
4. 失败属于数据库、storage、provider、命令目录、generator、runtime 还是 Worker；
5. 再进入对应模块日志，并使用 request/task/attempt ID 关联。

不要因为 Redis、对象存储或 provider 失败而在部署层强行改用另一实现；替换 provider 必须通过配置、组合根和版本记录完成。

## 数据与备份

备份必须覆盖：

- 权威数据库；
- 受管对象存储；
- 部署配置与密钥的安全副本；
- 当前产品镜像、扩展版本和迁移记录。

缓存和队列状态应可重建，不作为唯一备份。只备份数据库或只备份对象文件都不足以恢复完整项目。

## 恢复与升级

恢复演练需要验证数据库记录、对象摘要、owner 引用、项目版本、任务快照和结果之间一致。发现孤儿文件、缺失元数据或摘要不一致时，通过受控 reconciliation 处理，不手工拼路径修补。

升级遵循[版本化与发布](versioning-and-release.md)：先备份，运行版本化迁移，在隔离 Docker 环境验证，再切换服务。正式发布后，破坏性升级必须提供明确迁移和回滚说明。

## 完成标准

- 新环境只依赖 Docker 和受控配置即可启动；
- 实例展示实际产品与 provider 版本；
- compute Worker 能展示并自检实际 generator/executor/solver/result adapter 版本与隔离能力；
- 必需依赖失败时不进入 ready；
- 备份能同时恢复数据库与对象，并通过摘要核对；
- 升级、回滚和故障演练都有可重复步骤；
- 开发默认秘密、宿主机路径和临时调试配置没有进入正式镜像。
