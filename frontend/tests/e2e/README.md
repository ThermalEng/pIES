# QA-E2E-01: Playwright 真实用户验收

场景定义见 `docs/reviews/fullstack-decoupling-review-2026-08-20.md` §13(QA-E2E-01)。

## 环境要求(13.4)

- Playwright 与应用均在 Docker 环境运行(`docker compose` e2e 服务);
- Chromium 是每次变更的最低门禁;firefox/webkit 作为发布前扩展(镜像内
  未安装对应浏览器时自动跳过);
- 失败自动保存 trace(`test-results/artifacts`),关键视觉问题截图。

## 运行

```bash
# 1. 构建并启动应用(web + backend + worker + io_worker + postgres + redis)
docker compose up -d --build web backend worker io_worker

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
