// 0.15 外部设备 YAML 注入验收专用 Playwright 配置。
//
// 与默认套件单一来源: 复用 playwright.config.ts(baseURL/项目/报告/超时等
// 全部继承), 仅两处差异:
// 1. 清除默认配置对外部设备规格的排除(testIgnore 继承自 base, 置空);
// 2. 恰好收集 device-yaml-extension.spec.ts 一项。
// 该规格要求运行栈经 frontend/tests/e2e/docker-compose.ext-device.yml 只读
// 挂载外部设备内容目录(标准部署没有该挂载, 故默认配置排除); 只能在隔离
// 挂载栈运行, 命令见 frontend/tests/e2e/README.md 同节与 spec 页首。

import baseConfig from './playwright.config'

export default {
  ...baseConfig,
  testIgnore: [],
  testMatch: '**/device-yaml-extension.spec.ts',
}
