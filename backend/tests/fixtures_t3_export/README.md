# T3 验收 fixtures(真实下载, 只读复用)

- `t3-report.xlsx` / `t3-sealed.json`: 2026-10-11 T3 验收通过轮
  (`financial-evaluation.spec.ts`, K=1/N=2/E=3, Docker 真实 HiGHS +
  真实浏览器下载)留存的同一对文件, 由 e2e 直接落盘、
  未经手改, 复制检入供 `test_financial_export_content.py` 精确对照。
- 仅供测试断言读取; 生产代码不得依赖本目录。
