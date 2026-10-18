# QA-E2E-01: Playwright 真实用户验收

场景定义见 `docs/reviews/fullstack-decoupling-review-2026-08-20.md` §13(QA-E2E-01)。

## 环境要求(13.4)

- Playwright 与应用均在 Docker 环境运行(`docker compose` e2e 服务);
- Chromium 是每次变更的最低门禁;firefox/webkit 作为发布前扩展(镜像内
  未安装对应浏览器时自动跳过);
- 失败自动保存 trace(`test-results/artifacts`),关键视觉问题截图。

## 运行

```bash
# 1. 构建并启动当前应用（计算 Worker 随 0.8 接入）
docker compose up -d --build web backend io_worker

# 2. 等待 backend 就绪(首次启动会自动 seed admin + 初始化数据库)
#    管理员初始密码: iesplan-admin-initial(首次运行 global-setup 自动完成首登改密)

# 3. 运行 E2E 验收(构建 e2e 镜像并执行)
docker compose up -d e2e --build
docker compose logs -f e2e

# 只跑 Chromium(CI 门禁):
docker compose run --rm e2e npm run test:chromium

# 指定场景:
docker compose run --rm e2e npx playwright test --project=chromium --grep "场景 3"

# 输出位置(失败 trace / 截图 / html 报告):
#   frontend/tests/e2e/test-results/
```

### 0.15 外部设备 YAML 注入验收(隔离覆盖)

`device-yaml-extension.spec.ts` 验收外部设备内容(仓库外新增 YAML)的公开
消费链: 设备选择器 → 画布拖放/参数/连线 → 装配提交 → 真实 Worker → 结果。
外部内容 `device-catalog-external/constant_load.yaml` 经
`docker-compose.ext-device.yml` 只读挂载到扫描目录新子目录, 不修改生产
Compose 与内置 catalog。

该规格只能在挂载栈运行: 默认套件(`playwright.config.ts`)用 `testIgnore`
将其排除(标准部署无外部挂载, 收集它必红; 不是 skip, 也不在设备缺失时通过);
专用配置 `playwright.ext-device.config.ts` 复用默认配置、清除该排除并恰好
收集这一项。

```bash
# 在待验收工作树根目录执行: 数据目录即该根的 ./data(隔离副本);
# 勿在别的仓库根运行, 以免误用主线数据。项目名/端口任取隔离值, 不碰其他栈。
export PIES_EXT_DEVICE_DIR="$PWD/frontend/tests/e2e/device-catalog-external"
export IESPLAN_PORT=18085   # 任取空闲端口, 避免与其他栈冲突
docker compose -p pies-ext-dev -f docker-compose.yml \
  -f frontend/tests/e2e/docker-compose.ext-device.yml up -d --build \
  web backend io_worker compute_worker

# 运行本用例(专用配置; --build 确保测试镜像含当前 spec/配置, 不用陈旧镜像)
docker compose -p pies-ext-dev -f docker-compose.yml \
  -f frontend/tests/e2e/docker-compose.ext-device.yml --profile e2e \
  run --rm --build --no-deps e2e npx playwright test \
  --config playwright.ext-device.config.ts
```

入口自检(不启动应用): 默认 `npx playwright test --project=chromium --list`
不含本规格; `npx playwright test --config playwright.ext-device.config.ts
--list` 恰含一项。

### 0.15 外部数据 CSV 验收(既有公开路径, 含缺口边界)

`device-data-csv-extension.spec.ts` 验收仓库外设备数据 step CSV
(`device-data-external/electric_load_repeat_24h.csv`, `ies.device-data` 2.1.0,
24h `data_repeat` 基线: 偶数步 30 kW / 奇数步 50 kW, `device_id:
ies.device.electric_load`)的公开消费链: 数据页真实 GUI 上传(真实文件 +
质量报告“已通过校验”) → 公开草稿命令声明预定义槽来源(`{mode:
data_repeat, dataset_version_id}`)并 `dataset.bind` 同一版本 → 公开任务
API 提交 → 真实 io/compute Worker 求解 → 逐时/年累计与封存评价对账(手算
关键值: 年负荷 350,400 kWh; 电网购入 425,485.714… kWh; 封存
`energy_net` −297,840.0 CNY) → 结果页真实 GUI(结果摘要含上传数据集版本、
规划解参数预览、封存评价卡)。

覆盖边界(2026-10-18 切片裁决, 详见切片报告):

- 上传与结果查看走真实 GUI; 来源声明与任务提交走公开 API。切片当时校验预检
  对 dict 来源声明产出 `PARAM-UNIT-002` 假阻断, 任务中心 GUI 提交被该校验
  门禁挡住(证据见切片报告); 修复收口(合法声明放行 + GUI 提交闭环)见下文
  `device-data-csv-gui-submit.spec.ts`。本规格不据此做任何断言。
- 结果页“工程/计算步曲线”区块以 `metrics_summary` 为渲染门, 当前封存链未
  写该键, 区块实测不渲染(既有缺口: 既有 `hourly-steps.spec.ts` 同点失败);
  本规格以摘要/规划解/封存评价卡为结果 GUI 断言面。

```bash
# 在待验收工作树根目录执行: 数据目录即该根的 ./data(隔离副本); 勿在别的
# 仓库根运行, 以免误用主线数据。项目名/端口任取隔离值, 不碰其他栈。
export IESPLAN_PORT=18086   # 任取空闲端口, 避免与其他栈冲突
docker compose -p pies-ext-csv up -d --build web backend io_worker compute_worker

# 运行本用例(默认套件收集; --build 确保测试镜像含当前 spec/工件)
docker compose -p pies-ext-csv --profile e2e run --rm --build --no-deps e2e \
  npx playwright test device-data-csv-extension.spec.ts --project=chromium
```

### 0.15 外部数据 CSV 修复收口: 任务中心 GUI 提交

`device-data-csv-gui-submit.spec.ts` 在同一条公开链上验收修复后的校验门禁:
数据页真实 GUI 上传同一 CSV → 公开草稿命令声明 `{mode: data_repeat,
dataset_version_id}` 来源并完成装配/绑定/基线确认/配置 → 任务中心真实
GUI 提交(K=0/N=1)必须放行合法声明(无 “存在 1 项阻断问题…” 弹层, 修复前
该校验门禁误报 `PARAM-UNIT-002`) → 真实 io/compute Worker 完成 → 公开 API
核对逐时 30/50、年负荷 350,400 kWh、电网年购 425,485.714… kWh、封存
`energy_net` −297,840.0 CNY。提交为被测 GUI 动作, 脚手架与数值核对经公开 API。

```bash
export IESPLAN_PORT=18087   # 任取空闲端口, 避免与其他栈冲突
docker compose -p pies-ext-csv-gui up -d --build web backend io_worker compute_worker
docker compose -p pies-ext-csv-gui --profile e2e run --rm --build --no-deps e2e \
  npx playwright test device-data-csv-gui-submit.spec.ts --project=chromium
```

## 场景清单(§13.3)

| # | 场景 | 文件 |
|---|------|------|
| 1 | 登录、首登改密、退出、会话失效 | `auth-navigation-model.spec.ts` |
| 2 | 创建项目并浏览全部工作台页面 | `auth-navigation-model.spec.ts` |
| 3 | 添加热泵/电池,真实端口合法连接 + 错误连接诊断 | `auth-navigation-model.spec.ts` |
| 4 | 上传年度数据,质量报告并绑定版本 | `data-config-tasks.spec.ts` |
| 5 | 保存非默认财务配置,重读、确认基准、校验 | `data-config-tasks.spec.ts` |
| 6 | 提交任务,状态变化,结果并导出 | `data-config-tasks.spec.ts` |
| 7 | 管理员用户停用/启用 + 存储健康 | `admin-help-lang.spec.ts` |
| 8 | 帮助中心两个指南、Markdown、章节跳转返回 | `admin-help-lang.spec.ts` |
| 9 | 帮助章节深链接刷新保持 | `admin-help-lang.spec.ts` |
| 10 | 语言切换与缺失翻译提示 | `admin-help-lang.spec.ts` |
| 11 | 桌面/移动视口 + 键盘焦点 | `admin-help-lang.spec.ts` |
| 12 | 新建项目模型: 模板表单提交、失败保留输入与诊断、保存状态流转、直接 YAML 编辑(非拖放; 候选保存端点未合并, 由 route mock 提供契约响应, 见 spec 头部注释) | `model-new.spec.ts` |

## 真实用户原则(§13.2)

- 从应用公开入口打开页面并通过 UI 登录(`/login`);
- 定位优先 role/label/可见名称,不依赖脆弱 CSS 层级;
- 业务动作全部通过点击/输入/选择/拖拽/上传/确认完成;
- 禁止直接修改 localStorage、React 状态、数据库或调用业务 API 跳过被验收步骤;
- API 仅用于测试环境前置造数(`setup/api.ts`: 独立用户/管理员注册开关)和
  结束清理(`global-setup.ts`: 清理 `es-` 前缀残留用户);
- 每个场景使用独立用户与项目(幂等键),可重复执行并可清理;
- 页面断言之外同时检查 console error、失败网络请求(`fixtures/session.ts`)。

## 基础设施

- `global-setup.ts` — 环境自检 + 管理员首登改密(幂等) + 清理历史测试用户;
- `fixtures/session.ts` — UI 登录、会话监控(console/requestfailed/pageerror);
- `fixtures/env.ts` — 独立用户/项目/密码生成(幂等键);
- `setup/api.ts` — 仅造数/清理的 API 客户端(与被测动作隔离);
- `playwright.config.ts` — 单 worker 串行(共享部署环境),trace/screenshot on failure。
