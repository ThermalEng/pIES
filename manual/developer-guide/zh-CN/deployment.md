# 部署与运行

> 文档状态：生效蓝图；规范版本：1.0.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

## 运行环境边界

pIES 的构建、测试、格式化、数据库迁移验证及浏览器端到端验收统一在 Docker 容器内完成，宿主机不安装应用运行依赖，仅保留 Docker 引擎、代码仓库与受控数据目录。

## 统一部署配置

API 服务与各类 Worker 共用同一份 TOML 部署配置文件（默认位于 `backend/deploy/pies.toml`，镜像内路径 `/app/deploy/pies.toml`），由组合根在启动时解析为不可变类型化配置：

- **配置覆盖优先级**：代码显式传入 ＞ 环境变量 `IESPLAN_DEPLOY_CONFIG_FILE` 指向的挂载文件 ＞ 包内默认文件；应用运行参数不接受零散环境变量覆盖，避免配置多头维护。
- **默认值唯一源**：Worker 槽位（`compute_slots`、`io_slots`）、超时上限、求解策略与新建表单默认初值（前端通过 `GET /api/deployment/defaults` 读取）仅在此文件维护。
- **敏感信息与编排参数**：数据库凭证、`IESPLAN_SECRET_KEY` 等密钥与容器端口、网络编排仍由环境变量或部署平台注入，禁止提交到仓库默认配置。

## Docker Compose 开发部署

在仓库根目录执行启动命令（源码或 `Dockerfile` 变更后须加 `--build` 重新构建受影响服务）：

```bash
docker compose up -d --build
```

默认 Web 入口为 `http://localhost:8080`。服务组件职责如下：

- `web`：静态前端站点及帮助中心（nginx）；
- `backend`：同步 HTTP API 服务；
- `compute_worker` / `io_worker`：后台计算与 I/O 任务守护进程；
- `postgres` / `redis`：权威关系数据库与可重建任务队列/进度状态。

**Worker 并发配置**：单个 Worker 进程一次执行一个任务。提升并发需同时扩展 `compute_worker`（或 `io_worker`）副本数并调整部署配置 `[worker]` 中的 `compute_slots`（或 `io_slots`）总槽位上限，实际并发受两者较小值制约。

**空库启动注意**：首次初始化空数据库时，应由单 API 进程先行完成建表与内置身份播种后再启动其他副本与 Worker；如初始化失败，先排查日志与库状态。

### 移动或重命名仓库目录

`docker-compose.yml` 已固定项目名称为 `pies`，迁移仓库目录前建议先停止现有容器再在新路径重建：

```bash
docker compose down
# 移动或改名目录后在新目录执行
docker compose up -d --build
```

`./data` 目录下的持久化数据随仓库整体移动保留。若存在早期的遗留 Compose 项目名，先通过 `docker compose ls -a` 确认，再执行 `docker compose -p <旧项目名> down --remove-orphans` 清理旧容器引用。

## 开发者日常验证流程

1. 按变更范围构建并启动测试容器，遵循[前端验收分工](frontend.md#浏览器探索与自动回归)选择验证内容：
   - **纯文档改动**：构建前端帮助中心并校验目录链接、Markdown 解析与示例测试；
   - **后端改动**：在 Docker 中运行受影响模块、Application/API/Worker 测试，涉及持久化或迁移时使用真实 PostgreSQL 验证；
   - **前端或交互改动**：运行逻辑单测并在隔离 Docker 栈执行 Playwright 真实浏览器回归（测试环境使用独立 Compose 项目与数据卷，不混用开发数据）。
2. 测试完成后及时清理临时测试容器与测试生成的镜像（保留基础镜像），释放磁盘空间。

## Solver 执行隔离与资源观测

- **Executor 隔离边界**：`SolverRuntime` 必须通过组合根装配的受信任 `ExecutorProvider` 启动进程。默认策略禁用网络（`network: false`），运行身份无权访问业务数据库、对象存储凭证与仓库工作区；每次 Attempt 使用独立工作目录写入输入并按块交接 Manifest 声明输出。
- **算法插件隔离（`1.1.0` 目标）**：启用用户算法插件后须部署独立 `plugin_runner` 服务，以非 root 身份、只读根文件系统、断网策略与离线依赖环境运行插件载荷，严禁普通 Worker 挂载 Docker Socket。
- **资源限额与 cgroup v2 观测**：系统不设定统一的固定内存上限，部署者依据宿主承载力配置容器限额与部署策略。Linux/Docker 部署下任务进程通过本容器 cgroup v2 读取内存/交换空间限额、当前用量、生命周期峰值及 OOM 事件并在任务详情展示；无法读取时记为未知，不阻断运行。确认发生 OOM 或 `MemoryError` 时任务按资源耗尽失败并释放槽位，不自动在相同资源下重试或擅自修改模型/精度掩盖问题。

## 生产安全基线

生产环境上线前必须核对：

1. 设置高强度且唯一的 `IESPLAN_SECRET_KEY`；
2. 注入随机初始管理员密码并在首次登录后修改；
3. 替换默认数据库凭证，禁止开发默认密码进入生产；
4. 在可信反向代理层启用 HTTPS，并限制数据库、Redis、对象存储及管理端口的外部访问；
5. 制定数据目录、卷容量、备份周期与恢复演练责任机制。

## 健康检查、备份与恢复

- **健康与就绪分离**：`healthz` 仅反映进程存活；`readyz` 聚合检查数据库、存储及必需运行依赖（计算 Worker 主进程启动时核验内置求解器精确版本与最小自检，后续任务直接复用同一执行器）。故障排查时按 `容器状态 → healthz → readyz 失败项 → 模块日志（关联 request/task/attempt ID）` 逐级定位。
- **备份范围**：有效备份必须同时覆盖 **PostgreSQL 数据库**、**受管对象存储目录** 与 **部署配置及密钥**（Redis 队列与查询投影属于可重建状态）。仅备份数据库或仅备份文件均无法完成完整恢复。
- **升级与恢复演练**：升级前先执行全量备份，再在隔离 Docker 环境验证版本化数据库迁移与核心读/算流程。恢复演练须通过正常公开读取链路验证代表性项目、任务快照与封存结果，不靠直接修补磁盘路径验证。
