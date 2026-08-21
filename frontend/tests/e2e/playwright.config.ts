// QA-E2E-01 Playwright 配置(13.3/13.4 执行与证据)
//
// - 仅在 Docker 内运行(docker-compose e2e 服务), 目标地址 http://web:80;
// - Chromium 为每次变更的最低门禁; firefox/webkit 标记为可选扩展,
//   本地无对应浏览器时跳过(skipIf 由 use.browserName 不存在时触发);
// - 失败自动保存 trace(仅 retain-on-failure), 关键视觉问题截图,
//   复杂失败可改 env 打开 video;
// - 并发 1: 真实用户场景共享同一部署环境, 依赖全局状态(admin/存储),
//   逐场景串行执行保证可重复。

import { defineConfig, devices } from '@playwright/test'

const APP_URL = process.env.E2E_APP_URL ?? 'http://web:80'
const OUTPUT_DIR = process.env.E2E_OUTPUT_DIR ?? './test-results'

export default defineConfig({
  testDir: '.',
  // 前置造数(seed)与场景互相隔离: 每个 spec 文件一个 worker
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  retries: 0,
  globalSetup: './global-setup.ts',
  reporter: [
    ['list'],
    [
      'html',
      { outputFolder: `${OUTPUT_DIR}/html-report`, open: 'never' },
    ],
  ],
  outputDir: `${OUTPUT_DIR}/artifacts`,
  use: {
    baseURL: APP_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: process.env.E2E_VIDEO === 'on' ? 'on' : 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    // 发布前扩展(13.4): 镜像未安装对应浏览器时自动跳过
    // (playwright 在 launch 时找不到浏览器二进制会报错, 因此用条件构建 projects)
    ...(process.env.E2E_BROWSERS === 'all'
      ? [
          { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
          { name: 'webkit', use: { ...devices['Desktop Safari'] } },
        ]
      : []),
  ],
})
