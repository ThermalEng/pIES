# IES Plan — 综合能源系统规划平台

面向设计院工程师、能源服务公司工程人员、园区与单体建筑能源工程师的小型工业园区/单体建筑综合能源系统规划工具。

用户输入建筑或园区条件、能源设备、时序数据、经济参数和环境参数，获得可解释、可追溯的设备配置、运行能流、财务、环境、工程和可靠性结果。

## 快速开始

```bash
# 首次部署
docker compose up -d --build

# 访问
# http://localhost:8080   (默认端口, 可用 IESPLAN_PORT 修改)

# 默认管理员
# 用户名: admin
# 初始密码: 见 backend 环境变量 IESPLAN_DEFAULT_ADMIN_PASSWORD (默认 iesplan-admin-initial)
# 首次登录必须修改密码
```

## 生产部署安全要求

> ⚠️ **生产环境必须覆盖以下默认值**, 否则存在严重安全风险:

| 配置项 | 默认值(仅开发) | 生产要求 |
| --- | --- | --- |
| `IESPLAN_SECRET_KEY` | `dev-only-secret-change-me` | 必须设置高强度随机密钥(下载授权签名等使用) |
| `IESPLAN_DEFAULT_ADMIN_PASSWORD` | `iesplan-admin-initial` | 首启后立即改密, 或部署时注入一次性随机初始密码 |
| `IESPLAN_DB_PASSWORD` | `iesplan_dev_password` | 必须修改数据库密码 |
| HTTPS | 无 | 建议部署层启用 TLS(`Secure` Cookie 自动生效) |
| Redis 认证 | 无 | 生产建议配置密码/ACL(仅局域网时风险可控) |

## 架构

```
浏览器(React) → nginx → FastAPI(统一入口/API) → PostgreSQL(权威事实)
                                        │            Redis(可重建状态)
                                        ├→ 计算 Worker → 隔离求解器子进程
                                        └→ I/O Worker  → 内容寻址对象存储(/data/objects)
```

- **PostgreSQL**：项目、权限、任务事实、版本、证据和业务索引的事务事实来源
- **Redis**：可重建的队列、进度、心跳和缓存状态，不保存唯一业务事实
- **内容寻址对象存储**：大型不可变数据集、快照、中间对象、证据和报告对象
- **浏览器不是权威**：业务规则、项目数据与计算结果一律以服务端为准

## 核心能力（版本 1）

- 电/热/冷三类能源载体建模：电网连接、光伏、电池储能、电/热/冷负荷、热泵、燃气锅炉、电制冷机
- 存量设备与新增建设设备区分；图形化拖拽建模、端口/能源类型/方向校验
- 年度时序数据管理（标准非闰年 365 天，15/30/60 分钟分辨率，固定 UTC 偏移）
- 任意方案评价、规划搜索（多目标）、不确定性分析（固定方案可靠性 / 重规划敏感性）
- 四维结果有效性：物理、最优性、财务（IRR 六状态分类）、可靠性（样本统计）
- 证据与评估不可变；结果应用创建新版本
- Excel 报告导出（固定模板，中英标题）与完整项目包导入/导出
- 中英双语界面、程序内快速帮助、独立教程页

## 目录结构

```
backend/    Python 3.12 + FastAPI + SQLAlchemy 2.0 (API/Worker/求解器/指标)
frontend/   TypeScript + React 18 + Vite + @xyflow/react (主界面/教程页)
docs/       设计规格(数据库/计算模型/任务调度/注册表诊断)与实现契约
scripts/    动态工作流编排脚本(开发过程产物)
```

## 开发与测试

```bash
# 后端测试 (在 Docker 内)
docker compose run --rm backend pytest -q

# 前端构建 (在 Docker 内)
docker compose build frontend

# 启动全部服务
docker compose up -d --build
```

## 设计依据

`IES-Plan-Design-Input.rpd`（设计输入 v2.0.0）与 `docs/spec/` 下的四份规格文档。

## 许可

见 [LICENSE](LICENSE)。
