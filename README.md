<div align="center">
  <img src="frontend/src/assets/brand/logo-lockup.svg" alt="pIES Logo" width="210" />

  <h1>pIES — 综合能源系统规划与工程决策平台</h1>

  <p>
    <strong>面向低碳园区与建筑的多能互补拓扑建模、8760 小时联合优化与嵌入式工程财务决策平台</strong><br />
    <em>透明物理机理 · 声明式约束建模 · HiGHS 混合整数规划 · 同源不可变财务与报告交付</em>
  </p>

  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MPL--2.0-0e5cad.svg?style=flat-square" alt="License: MPL-2.0" /></a>
    <img src="https://img.shields.io/badge/Docker-Compose%20Ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker Ready" />
    <img src="https://img.shields.io/badge/Backend-Python%203.12%20%7C%20FastAPI-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12 FastAPI" />
    <img src="https://img.shields.io/badge/Frontend-React%2018%20%7C%20TypeScript-0891b2?style=flat-square&logo=react&logoColor=white" alt="React 18 TypeScript" />
    <img src="https://img.shields.io/badge/Solver-HiGHS%201.7.2%20MILP-1e7d32?style=flat-square" alt="Solver: HiGHS 1.7.2" />
    <img src="https://img.shields.io/badge/E2E-Playwright%20Chromium-2ead33?style=flat-square&logo=playwright&logoColor=white" alt="Playwright E2E" />
    <a href="manual/changelog/README.md#当前版本状态"><img src="https://img.shields.io/badge/Milestone-0.14.5%20Complete-1e7d32?style=flat-square" alt="开发里程碑 0.14.5 已完成" /></a>
  </p>

  <p>
    <a href="#-真实系统全流程动态演示">实景演示</a> ·
    <a href="#-为什么选择-pies给能源规划工程师">工程能力</a> ·
    <a href="#%EF%B8%8F-架构美学与开放扩展给开发者与研究者">技术架构</a> ·
    <a href="#-3-步快速上手docker">快速部署</a> ·
    <a href="#-统一文档与规范入口">正式文档</a>
  </p>

  <br />

  <img src="frontend/src/assets/brand/pies-hero-flow.svg" alt="pIES 多能网络拓扑、HiGHS 联合求解与嵌入式财务评价动画架构图" width="100%" />
</div>

---

## 🎬 真实系统全流程动态演示

以下动态演示与界面截图全部由 **Docker Chromium Playwright 真实交互测试** 运行真实双热泵园区算例（内置 **HiGHS 1.7.2 MILP** 全局优化至 `Gap 0.0%`）直接录制生成：

<p align="center">
  <img src="frontend/src/assets/brand/pies-workbench-tour.gif" alt="pIES 真实操作工作流动态演示：系统建模 → 8760h 数据校验 → 规划配置 → 成本与电价绑定 → 结果分析（浅色/深色主题）" width="90%" />
</p>

### 实景界面速览（浅色 / 深色双主题 & 响应式布局）

<table>
  <tr>
    <td width="50%" align="center">
      <img src="frontend/src/assets/brand/showcase-canvas.png" alt="系统建模画布：电/热/冷/气多载能端口校验与存量/新增混装" width="100%" /><br />
      <sub><strong>1. 声明式多能网络画布</strong> — 端口载能色标、方向校验与 <code>existing</code> / <code>new</code> 设备混装</sub>
    </td>
    <td width="50%" align="center">
      <img src="frontend/src/assets/brand/showcase-planning.png" alt="规划配置：受限声明式目标函数与设备决策变量上下界" width="100%" /><br />
      <sub><strong>2. 规划目标与决策变量</strong> — 精确十进制边界、结构化表单与完整 YAML 视图无损互转</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src="frontend/src/assets/brand/showcase-results-light.png" alt="结果分析总览（浅色主题）：HiGHS 证明最优 Gap 0.0% 与四维有效性结论" width="100%" /><br />
      <sub><strong>3. 不可变求解证据与四维结论</strong> — 物理可行性、最优性、财务与可靠性独立评估</sub>
    </td>
    <td width="50%" align="center">
      <img src="frontend/src/assets/brand/showcase-results-dark.png" alt="结果分析与现金流图表（深色主题）：封存现金流柱折组合图与建设决策" width="100%" /><br />
      <sub><strong>4. 原生 SVG 现金流图与深色主题</strong> — 建设决策、规划规模（<code>50 kW</code> / <code>200 kW</code>）与回写入口</sub>
    </td>
  </tr>
</table>

---

## 💡 为什么选择 pIES？（给能源规划工程师）

园区与建筑综合能源系统正从单一供能走向“源网荷储”多能互补，但规划团队往往受困于**商业软件黑盒不可审计**或**手工 Excel 难以统筹 8760 小时耦合调度与税务现金流**。pIES 以同一份透明物理与财务事实贯通全业务链：

| 对比维度 | 传统商业黑盒 / 手工 Excel 拼表 | pIES 一体化闭环方案 |
| :--- | :--- | :--- |
| **多能拓扑与量纲** | 电、热、冷专业割裂建模，单位换算易遗漏 | 统一电/热/冷/气端口拓扑，后端装配门禁强制校验量纲、方向与接口闭合 |
| **容量与调度耦合** | 典型日估算或先定容量后模拟，难以发现峰谷套利极限 | 8760h（或 15/30/60 min 分辨率）连续步调度与设备容量 **HiGHS MILP** 同次联合求解 |
| **电价与税务口径** | 静态平均电价、手工外挂折旧与退税表，易与调度脱节 | 地区 `FinanceProfile` + 项目覆盖生成不可变财务快照；两部制自然月最大需量、VAT 抵扣池、计税折旧直接参与规划求解 |
| **证据追溯与交付** | 改完模型覆盖原表，界面数值与上报 Excel 常有出入 | `CalcSnapshot` 不可变封存；页面、规划解回写与双语 Excel 报告严格消费同一份证据包 |

### 核心工程能力

1. **高保真多能设备与混合装配**：内置光伏（`pv`）、电化学储能（`battery`）、热泵（`heat_pump`）、燃气锅炉（`gas_boiler`）、电制冷机（`electric_chiller`）、电网连接（`grid_connection`）及多类负荷模型；支持项目内自定义模板（[模型与算法](manual/developer-guide/zh-CN/customization-center.md)）与存量/新增混合配置。
2. **8760h 时序数据集与质量诊断**：支持标准化 365 天非闰年 CSV 上传与内置园区样例数据一键生成，提供逐行结构诊断与固定 UTC 偏移检查。
3. **全周期工程财务与四维评价**：支持建设期分期付款（`payments`）、运营期大修换新（`maintenance_expenses`）、静态/动态回收期、NPV、FIRR 分类状态及[基准差额对比](manual/user-guide/zh-CN/results-and-exports.md)，严格区分税务与含税口径边界（详见[概念、单位与当前限制](manual/user-guide/zh-CN/concepts-and-limits.md)）。
4. **Git 级版本演进与离线项目包**：规划解可一键回写生成不可变新版本（`Revision`），并支持完整项目打包（`.zip`）导出及带冲突提案的离线导入。

---

## 🛠️ 架构美学与开放扩展（给开发者与研究者）

pIES 在设计上追求**开放优先于效率、正确性优先于兼容、边界公开可验证**，前后端均采用严格的依赖方向分层与静态架构门禁约束：

```mermaid
flowchart TB
    subgraph FE["Frontend · React 18 + TypeScript (@xyflow/react + 原生 SVG 图表)"]
        direction LR
        Pages["app / pages<br/><sub>路由装配 · 工作台上下文</sub>"] --> Features["features/*<br/><sub>contracts · model · mappers · api</sub>"] --> Shared["shared / components<br/><sub>HTTP · i18n · 设计令牌</sub>"]
    end

    subgraph BE["Backend · Python 3.12 + FastAPI + Worker"]
        direction LR
        API["api / worker<br/><sub>HTTP 传输 · 租约执行</sub>"] --> App["application<br/><sub>跨模块用例与事务边界</sub>"] --> Domain["domain modules<br/><sub>devices · modeling · assembly<br/>computation · finance · analysis</sub>"] --> Core["core<br/><sub>无状态量纲 · 诊断 · 契约</sub>"]
    end

    subgraph Pipe["Computation 3-Stage Boundary"]
        direction LR
        Art["ValidatedAssemblyArtifact<br/>+ EffectiveFinanceConfig"] --> Gen["GeneratorProvider<br/><sub>单位换算 · Solver Bundle</sub>"] --> Run["SolverRuntime<br/><sub>隔离执行 HiGHS 1.7.2</sub>"] --> Adp["ResultAdapter<br/><sub>封存 ComputeResult</sub>"]
    end

    FE -- "OpenAPI / snake_case DTO + 乐观锁 revision" --> API
    Domain -. "强制校验" .-> Pipe
```

- **声明式约束与方程扩展**：通过 `system_equations`、`step_relation`（跨步储能/爬坡/蓄热耦合）和 `amount_relation`（财务分量与目标约束）表达复杂工艺与规则，无需修改通用调度或求解运行时。
- **零信任猜测边界**：后端统一校验设备 YAML、装配 YAML、财务 JSON 与计算配置；前端只维护展示状态与未提交草稿，409 并发冲突与 422 字段级诊断均保留现场输入。
- **开箱即用的自动化质量门禁**：
  - 后端 [test_architecture_gates.py](backend/tests/test_architecture_gates.py)：通过纯 Python AST 静态扫描强制锁死 `core`、领域层、`application`、`api` 与 `worker` 间的所有跨层穿透；
  - 前端 27 组纯逻辑单元测试（[frontend/tests/](frontend/tests/)）+ 全套 **Docker Chromium Playwright E2E** 真实业务回归（[frontend/tests/e2e/](frontend/tests/e2e/)）。

---

## ⚡ 3 步快速上手（Docker）

项目采用完整容器化编排（`web` + `backend` + `io_worker` + `compute_worker` + `postgres` + `redis`），宿主机无需安装 Python、Node 或 HiGHS 依赖。

### 1. 一键构建并启动全套服务

```bash
git clone https://github.com/ThermalEng/pIES.git
cd pIES
docker compose up -d --build
```

服务就绪后（可通过 `curl http://localhost:8080/api/readyz` 确认数据库、存储与设备/求解器注册表状态）：
- **Web 控制台**：`http://localhost:8080`
- **内置离线帮助中心**：`http://localhost:8080/help/`（前端构建期直接从 `manual/` 生成，后端不可用时仍可静态阅读）

### 2. 登录与体验内置园区算例

- 默认种子管理员账号为 `admin`，初始密码由环境变量 `IESPLAN_DEFAULT_ADMIN_PASSWORD` 指定（首次登录强制要求修改密码）。
- 建议使用普通规划账号创建项目，依次体验：**拖拽搭建多能拓扑 → 生成内置 8760h 样例数据 → 引用财务画像并绑定成本/电价 → 运行全项目校验 → 提交 HiGHS 规划任务 → 查看封存结果与导出双语 Excel**。

### 3. 运行测试与架构门禁（均在 Docker 内）

```bash
# 后端架构门禁与单元测试
docker compose exec backend pytest tests/test_architecture_gates.py

# 前端构建与逻辑单元测试
docker build --target build -f frontend/Dockerfile -t pies-fe-check . && \
  docker run --rm pies-fe-check node scripts/run-logic-tests.mjs

# 真实浏览器 Playwright E2E 验收
docker compose --profile e2e up -d --build e2e
```

> [!CAUTION]
> **生产安全提示**：上线前必须更换默认管理员密码、数据库凭证与 `IESPLAN_SECRET_KEY`，并在反向代理层配置 HTTPS。完整配置项与 Worker 隔离参数见 [部署与运行指南](manual/developer-guide/zh-CN/deployment.md)。

---

## 📚 统一文档与规范入口

本仓库以 [`manual/`](manual/README.md) 作为唯一长期正式正文来源（网页“帮助中心”同步托管该目录）：

| 正式入口 | 面向读者 | 核心内容 |
| :--- | :--- | :--- |
| 📖 **[使用者指南](manual/user-guide/README.md)**（[中文](manual/user-guide/zh-CN/README.md) \| [EN](manual/user-guide/en-US/README.md)） | 能源规划师、工程咨询师、管理员 | 登录会话、画布建模、CSV 数据准备、财务/规划配置、任务排查与 Excel 导出 |
| 🛠️ **[开发者指南](manual/developer-guide/README.md)**（[中文](manual/developer-guide/zh-CN/README.md) \| [EN](manual/developer-guide/en-US/README.md)） | 架构师、二次开发者、算法研究员 | [架构宪法](manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md)、[系统蓝图](manual/developer-guide/zh-CN/architecture.md)、[文件格式契约](manual/developer-guide/zh-CN/file-formats.md)、[前端规范](manual/developer-guide/zh-CN/frontend.md) 与 [部署指南](manual/developer-guide/zh-CN/deployment.md) |
| ⏱️ **[更新日志与 Roadmap](manual/changelog/README.md)**（[中文 Roadmap](manual/changelog/roadmap.md) \| [EN Changelog](manual/changelog/en-US/README.md) \| [EN Roadmap](manual/changelog/en-US/roadmap.md)） | 关注演进进度的用户与贡献者 | 按时间倒序记录已验收事实与下一阶段计划 |
| 🔍 **[审查证据与历史归档](docs/README.md)**（[Reviews](docs/reviews/) \| [Archive](docs/archive/README.md)） | 审计人员与历史溯源 | 带基线日期的架构/安全审查快照与已替代历史材料（非现行规范） |

---

## 🗺️ 开发状态与开源许可

- **当前进度**：产品元数据版本 `0.1.0`（[pyproject.toml](backend/pyproject.toml)），开发里程碑 **`0.14.5`** 已完成；下一目标 **`0.15.0`** 聚焦 Provider 扩展与工程全流程验收，详见 [更新日志](manual/changelog/README.md) 与 [Roadmap](manual/changelog/roadmap.md)。
- **开源协议**：本项目基于 **[Mozilla Public License 2.0 (MPL-2.0)](LICENSE)** 开源发布。

<p align="center">
  <sub>Copyright © 2026 pIES Contributors · Built for transparent, auditable Integrated Energy System planning.</sub>
</p>
