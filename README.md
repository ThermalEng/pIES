# pIES — 综合能源系统规划平台

**用物理含义清晰的模型连接多能系统，用开放、可组合的计算能力支撑规划决策。**

pIES 面向园区、建筑等综合能源场景，提供从图形化建模、数据准备、方案计算到结果评价的一体化工作流。

> 正式产品版本为 `0.1.0`；已完成并合入 `master` 的开发里程碑为 `0.7.0`，下一开发目标为 `0.8.0`，首个正式稳定版本为 `1.0.0`。版本历史和开发顺序见[更新日志与 Roadmap](manual/changelog/README.md)。

## 核心特点

- **多能模型**：统一面向电、热、冷、气、氢等载能系统，模型种类丰富，可根据需求自定义模型。
- **物理图景清晰**：采用设备—端口—连接边模型，物理图景清晰，模型抽象明确。
- **建模直观**：通过 GUI 画布完成系统建模、数据绑定、校验、计算和结果查看。
- **模块化开放架构**：设备、装配、计算、求解和分析相互解耦，功能可以自由组合。
- **易于扩展**：支持扩展设备模型、数据格式、计算生成器、求解器和结果适配器。
- **多类分析**：支持方案计算、规划优化、不确定性分析和批量分析。
- **综合评价**：提供能量平衡、容量配置、运行轨迹、经济、环境和可靠性结果评价等功能，方便工程人员使用。
- **工程化运行**：提供 Docker 部署、版本化文档和统一的 GUI 工作流。

## Roadmap 亮点

- **可插拔设备与求解能力（待实现）**：通过稳定扩展接口接入新设备、新算法和新求解器。
- **规划—运行闭环（待实现）**：衔接容量规划、全年运行校核和多场景比较。
- **智能化数据准备（待实现）**：由本地 AI 通过问答和 Excel 整理生成标准项目数据。
- **一键生成完整规划报告（待实现）**：由本地 AI 基于项目与计算结果生成可复核报告。
- **完整版本与恢复体验（待实现）**：支持项目版本创建、查看、恢复和项目包导入。

## 适用场景

- 园区或建筑的电、热、冷综合能源规划；
- 光伏、储能、热泵、制冷和燃气供热等技术的容量配置与协同运行分析；
- 多方案经济性、可靠性、环境影响和敏感性比较；
- 新设备、求解器、规划算法和结果分析方法的研究与集成。

## 快速开始

```bash
docker compose up -d --build
```

源码或 Dockerfile 变化后仍使用上面的 `--build` 命令；Compose 会复用未变化的镜像层和数据目录，并只重建受影响的服务。不要用不带 `--build` 的 `docker compose up` 验证新源码。

浏览器打开 `http://localhost:8080`。默认管理员用户名为 `admin`；初始密码由部署变量 `IESPLAN_DEFAULT_ADMIN_PASSWORD` 提供，首次登录必须修改。

> 生产部署前必须更换默认管理员密码、数据库密码和 `IESPLAN_SECRET_KEY`，并在部署层启用 HTTPS。完整要求见[部署与运行](manual/developer-guide/zh-CN/deployment.md)。

## 文档

本 README 是项目唯一总入口，正式文档以 `manual/` 为唯一正文来源：

- [使用者指南](manual/user-guide/README.md)：从登录到建模、计算、结果和管理的 GUI 操作；
- [开发者指南](manual/developer-guide/README.md)：架构蓝图、公共契约、扩展、部署和贡献规范；
- [更新日志](manual/changelog/README.md)：倒序版本历史、版本规则和 [Roadmap](manual/changelog/roadmap.md)；

## 开发与验证

所有依赖安装、编译、测试、格式化和数据库验证都只能在 Docker 中运行：

```bash
docker compose build backend web
docker compose run --rm backend pytest -q
docker compose up -d --build
```

设计、开发、重构和审查前必须阅读[架构宪法](manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md)。画布拖放由人工核查。

## 许可

Copyright © 2026 pIES Contributors.

本项目采用 [Mozilla Public License 2.0](LICENSE)（SPDX：`MPL-2.0`）。
