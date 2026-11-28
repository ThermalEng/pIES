# 部署与运行

> 文档状态：生效蓝图；规范版本：`1.2.0`；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

## 运行环境边界

pIES 的构建、测试、格式化、数据库迁移验证及浏览器端到端验收统一在 Docker 容器内完成，宿主机不安装应用运行依赖，仅保留 Docker 引擎、代码仓库与受控数据目录。

## 统一部署配置

控制面服务及临时容器执行策略共用同一份 TOML 部署配置文件（默认位于 `backend/deploy/pies.toml`，镜像内路径 `/app/deploy/pies.toml`），由组合根在启动时解析为不可变类型化配置：

- **配置覆盖优先级**：代码显式传入 ＞ 环境变量 `IESPLAN_DEPLOY_CONFIG_FILE` 指向的挂载文件 ＞ 包内默认文件；应用运行参数不接受零散环境变量覆盖，避免配置多头维护。
- **默认值唯一源**：任务并发槽位、超时上限、求解与容器资源策略及新建表单默认初值（前端通过 `GET /api/deployment/defaults` 读取）仅在此文件维护。
- **敏感信息与编排参数**：数据库凭证、`IESPLAN_SECRET_KEY` 等密钥与容器端口、网络编排仍由环境变量或部署平台注入，禁止提交到仓库默认配置。

## Docker Compose 部署架构（`1.2.0`）

在仓库根目录执行启动命令（源码或 `Dockerfile` 变更后须加 `--build` 重新构建受影响服务）：

```bash
docker compose up -d --build
```

默认 Web 入口为 `http://localhost:8080`。常驻服务精简收敛为五个核心容器：

- `web`：静态前端站点及帮助中心（`nginx`）；
- `backend`：统一控制面进程——承担同步 HTTP API、权限与事务、编辑态快速校验、IO 编排及异步计算资源调度（内部按独立线程/子进程划分 HTTP 请求与长时调度/大文件 IO/镜像构建，并分别限流，确保请求线程不卡死）；
- `job_runner`：容器编排代理服务——仅暴露五个固定业务操作契约（`build_plugin`、`start_stage`、`inspect_stage`、`stop_stage`、`cleanup_stage`），负责按统一契约构建插件镜像及拉起/回收临时断网计算容器；
- `postgres` / `redis`：权威关系数据库与可重建任务队列/进度状态。

**任务并发配置**：实际计算并发由部署配置 `[worker]` 中的槽位上限与宿主机容器资源共同控制；各计算步骤均以独立临时断网容器运行，在状态与有效产物持久化后清理容器与临时文件，不再部署独立的常驻 `compute_worker` 与 `io_worker` 守护进程。

**评价器并发预算（`[stage].evaluator_max_concurrency`）**：核心求解器完成且获得可行工程解后，所有已配置的 `evaluators[]` 在语义上保持逻辑并联（各评价器消费独立检查点并相互故障隔离）。部署配置 `[stage].evaluator_max_concurrency` 默认值为 `1`，用于在中小宿主机部署时将每个计算槽位在评价阶段的峰值并发容器数收敛到 1，防止单任务同时拉起多个容器压垮宿主机 CPU/内存预算；在资源充足的宿主机或生产集群中调高该值后，单个任务的多个评价器阶段即可启用物理多容器并发执行。

**空库启动注意**：首次初始化空数据库时，应由单 `backend` 控制面实例先行完成建表与内置身份播种后再扩容其他副本；如初始化失败，先排查日志与库状态。

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
   - **后端改动**：在 Docker 中运行受影响模块、Application/API/调度与容器执行测试，涉及持久化或迁移时使用真实 PostgreSQL 验证；
   - **前端或交互改动**：运行逻辑单测并在隔离 Docker 栈执行 Playwright 真实浏览器回归（测试环境使用独立 Compose 项目与数据卷，不混用开发数据）。
2. 测试完成后及时清理临时测试容器与测试生成的镜像（保留基础镜像），释放磁盘空间。

## 临时计算容器隔离与资源观测

- **按文件交接与不可变节点划分容器**：只在需要计算隔离、资源控制或第三方可替换的计算环节（`predictor`、`generator`、`solver`、`evaluator` 及一体化 `algorithm`）建立临时容器边界；设备方程解析、单位换算、装配校验与纯函数库保持为可复用 Python 库，不盲目拆成容器间调用。
- **全步骤断网容器隔离**：计算环节由 `backend` 经 `job_runner` 固定业务接口拉起独立一次性容器执行：默认 `--network=none`、`--read-only`、`--cap-drop=ALL`、非 root 身份运行，无数据库/Redis/对象存储凭证与宿主机代码目录权限；仅按步骤只读挂载 `/work/in` 冻结输入与可写挂载 `/work/out` 声明输出，容器生命周期严格遵循统一顺序：

```text
启动容器 → 独立执行 → 自然退出 / 取消 / 超时 → 收集退出状态与声明输出 → 持久化有效产物 → 清理容器与临时文件
```
- **构建与运行分离**：安装含额外依赖的插件时，由 `job_runner` 的 `build_plugin` 流程基于官方基础镜像生成插件计算镜像，运行期严禁联网或动态下载依赖；计算可追溯性依靠冻结生成的不可变输入文件、快照与持久化产物保障，不强行要求一次即毁的容器运行环境不可变。
- **固定业务级容器接口**：`backend` 与所有临时计算容器均不直接挂载宿主机 `/var/run/docker.sock`，容器编排统一收口于 `job_runner` 固定五个业务操作契约，不透传通用 Docker 参数或任意宿主机路径。
- **资源限额与 cgroup v2 观测**：系统不设定统一的固定内存上限，部署者依据宿主承载力配置容器限额与部署策略。临时计算容器通过 cgroup v2 实施并观测 CPU、内存/交换空间上限、生命周期峰值及 OOM 事件，并在任务详情展示；无法读取时记为未知，不阻断运行。确认发生 OOM 或 `MemoryError` 时任务按资源耗尽失败并释放槽位，不自动在相同资源下重试或擅自修改模型/精度掩盖问题。

## 生产安全基线

生产环境上线前必须核对：

1. 设置高强度且唯一的 `IESPLAN_SECRET_KEY`；
2. 注入随机初始管理员密码并在首次登录后修改；
3. 替换默认数据库凭证，禁止开发默认密码进入生产；
4. 在可信反向代理层启用 HTTPS，并限制数据库、Redis、对象存储、`job_runner` 及管理端口的外部访问；
5. 制定数据目录、卷容量、备份周期与恢复演练责任机制。

## `1.1.5 / 1.1.6 → 1.2.0` 升级与迁移手册

从 `1.1.5` 或 `1.1.6` 升级至 `1.2.0` 时严格执行以下顺序，严禁跳过任务排空步骤或对历史冻结数据做原地改写：

1. **排空在途任务并完成全量备份**：
   - 升级前等待或取消全部非终结任务（`queued`、`running`、`cancelling`）；若仍存在未终结任务，迁移器将显式抛出 `UpgradeDrainError` 并阻断迁移。
   - 备份 PostgreSQL 数据库、受管对象存储目录、`backend/deploy/pies.toml` 及环境密钥。
2. **执行幂等版本化数据库迁移**：
   - 升级 `backend` 镜像后执行 `python -m iesplan.migrations`（或由受控单实例启动触发 `apply_migrations()`），按序幂等应用两项迁移并登记至 `schema_migrations`：
     - `v1_2_0_calc_config`（版本 ID `20261126_120_calc_config`）：将项目已保存的可编辑 `CalculationConfig` 修订一次性转为 `1.2.0` 13 字段结构（移除 `convergence`、将 `evaluator` 映射为并联 `evaluators[]`、按分槽迁入 `options.generator` / `options.algorithm`）；已完成的历史 `CalcSnapshot` 与 `Evidence` 保持只读不变。
     - `v1_2_0_ecosystem`（版本 ID `20261127_120_ecosystem`）：创建 `ecosystem_settings`、`local_artifacts`、`member_artifact_installs` 表及单当前生效版本索引，并播种默认离线设置行（`market_mode="local"`、`local_upload_requires_approval=True`）。
3. **重建并拉起容器栈（含内置镜像与插件基础镜像）**：
   - 在仓库根目录执行 `docker compose up -d --build`，随同拉起核心五容器并由一次性 `builtin_stages` 服务构建 `pies-builtin-stages:1.2.0` 与插件基础镜像 `pies-plugin-base:latest`。
4. **可选独立公共市场与无状态投稿网关**：
   - 若需联网生态或自建团队市场，可单独部署静态市场目录（`market/` 产物与 `index.json`，推荐静态站点/GitHub Pages 托管）及独立无状态投稿网关（`market_gateway/`，配置 `MARKET_HMAC_SECRET` 与 SMTP/GitHub App 凭证）；随后由管理员在 `/admin/ecosystem` 切换 `market_mode = "online"` 并配置 `market_index_url`、`submission_gateway_url` 与可选单源 `pip_index_url`。
5. **单当前生效版本升级策略**：
   - `1.2.0` 按 `(kind, stable_id)` 仅保留 **1 个当前生效版本**，不建设平台级跨版本兼容层或历史版本多选切换器。升级或下架本地插件前，系统无状态检查在途 `CalcSnapshot` 引用（有在途引用返回 `409 ECO-UNINSTALL-IN-USE`）；新版本是否兼容既有项目输入完全由高版本模型/插件自身实现决定。

## 健康检查、备份与恢复

- **健康与就绪分离**：`healthz` 仅反映进程存活；`readyz` 聚合检查数据库、存储、`job_runner` 代理连通性及内置计算镜像目录可用性。故障排查时按 `容器状态 → healthz → readyz 失败项 → 模块日志（关联 request/task/attempt ID）` 逐级定位。
- **备份范围**：有效备份必须同时覆盖 **PostgreSQL 数据库**、**受管对象存储目录** 与 **部署配置及密钥**（Redis 队列与查询投影属于可重建状态）。仅备份数据库或仅备份文件均无法完成完整恢复。
- **升级与恢复演练**：升级前先执行全量备份，再在隔离 Docker 环境验证版本化数据库迁移与核心读/算流程。恢复演练须通过正常公开读取链路验证代表性项目、任务快照与封存结果，不靠直接修补磁盘路径验证。
