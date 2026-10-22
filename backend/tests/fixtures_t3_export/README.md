# 财务摘要导出 fixtures(真实下载, 只读复用)

- `t3-report.xlsx` / `t3-sealed.json`: 2026-10-22 D3 税务主链通过轮
  (`tax-chain.spec.ts` 非零税额全链, Docker 真实 HiGHS + 真实浏览器下载)
  留存的同一对文件, 由 e2e 直接落盘、未经手改, 复制检入供
  `test_financial_export_content.py` 精确对照。
- 封存块为当前 `ies.financial-evaluation@1.0.0`: 显式含 `tax_cash`
  税务明细（其中 VAT 为 36 个月，另含退款、所得税、折旧和亏损余额数组）与 `annual_net_flows`
  税后现金流(4 期), 财务摘要表逐行与该封存事实对应。
- 仅供测试断言读取; 生产代码不得依赖本目录。
