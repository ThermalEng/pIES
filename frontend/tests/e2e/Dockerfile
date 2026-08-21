# QA-E2E-01 Playwright 运行环境 (13.4: Playwright 与应用均在 Docker 内运行)
# 使用官方 Playwright 镜像(预装 Chromium/Firefox/WebKit), 独立 node_modules,
# 不污染主机环境(CLAUDE.md 1)。
FROM mcr.microsoft.com/playwright:v1.54.2-noble

WORKDIR /app/e2e

# 仅复制依赖清单, 先装依赖利用层缓存
COPY frontend/tests/e2e/package.json ./
RUN npm install --no-audit --no-fund

# 复制测试代码与共享文件
COPY frontend/tests/e2e/ ./

# 测试结果挂载目录(失败 trace/screenshot/html 报告)
ENV E2E_OUTPUT_DIR=/app/e2e/test-results
VOLUME /app/e2e/test-results

CMD ["npm", "test"]
