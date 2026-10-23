export const meta = {
  name: 'iesplan-e2e-verify',
  description: '全栈集成验证: 构建/启动/端到端流程/验收对照/修复',
  phases: [
    { title: 'E2E', detail: '全栈冒烟与核心闭环验证' },
    { title: 'Accept', detail: '验收标准对照与问题修复' },
  ],
}

phase('E2E')
const e2e = await agent(
  `你是 pIES 的全栈端到端验证者。工作目录 /home/mc/Documents/工作文档/pIES。

后端(基础层+业务单元)与前端均已实现并通过各自的单元测试/构建。现在做全栈集成验证。

## 步骤
1. **全栈构建与启动**:
   - docker compose build (全部服务)
   - docker compose up -d postgres redis (先起依赖)
   - docker compose up -d (全部)
   - 等待健康: 轮询 docker compose ps 直到 backend/worker/web 正常; curl http://localhost:8080/api/healthz 与 /api/readyz
2. **核心闭环 E2E**(用 curl 或写一个 python 脚本在 docker 内跑; 推荐写 tests/e2e_full.py 放 backend/tests/ 下用 TestClient 但连真实 postgres? 更稳妥: 直接 curl 走真实 HTTP 全栈):
   1. 管理员登录(admin/初始密码)→ 首次改密
   2. 创建工程师账号(管理员)
   3. 工程师登录 → 创建项目(CNY, +08:00)
   4. 建模: 添加 6-8 类设备(电网连接/光伏/电池/热泵/锅炉/制冷机/电负荷/热负荷/冷负荷)与连接
   5. 数据: 生成内置样例数据集(1h 分辨率)
   6. 配置: 保存默认配置 + 财务基准确认
   7. 校验: validation run 应通过(无阻断错误)
   8. 提交任务: 方案评价(eval) → 轮询状态到 completed; 查看结果(四维评估存在)
   9. 提交规划任务(plan) → 轮询完成; 查看候选列表与 IRR
   10. 选择结果 → 差异预览 → 应用结果(创建新版本)
   11. Excel 导出(zh) → 下载文件存在且为 xlsx
   12. 项目包导出(所有者) → 下载; 查看者导出包应 403; 查看者导出 Excel 应成功
   13. 项目包导入(另一个工程师) → 新项目身份/所有者正确
   14. 归档项目 → 禁止编辑; 撤销归档
   15. 审计查询(管理员) → 事件存在
   16. 存储视图与健康端点
3. **发现的问题分两类处理**:
   - 阻断性 bug: 直接修复(只改 backend/ 或 frontend/ 内文件), 重建/重启相应服务, 重跑
   - 非阻断问题: 记录到问题清单
4. **输出报告**: 每步结果(通过/失败+原因), 修复记录, 遗留问题清单

## 注意
- 全部操作在 docker 内; 不在主机跑 python/node
- 若 e2e 脚本不方便 curl 手打, 可写 /home/mc/Documents/工作文档/pIES/backend/tests/e2e_full.py 用 httpx 连 http://localhost:8080 跑完整流程(放 tests/ 但不要求 pytest 默认收集, 或标记 @pytest.mark.e2e)
- 中文输出最终报告`,
  { label: 'e2e:full-stack', phase: 'E2E', effort: 'high' }
)

phase('Accept')
const accept = await agent(
  `你是 pIES 的验收审查者。工作目录 /home/mc/Documents/工作文档/pIES。

全栈已构建并完成端到端冒烟验证。现在对照设计输入做最终验收与补漏。

## 步骤
1. 阅读 /home/mc/Documents/工作文档/pIES/IES-Plan-Design-Input.rpd 的 17.9 节(编号化产品约束目录)与 18 节(产品完成边界 AC-MVP-001~008)
2. 对照当前代码(后端 backend/ 前端 frontend/), 逐条检查 AC-MVP-001~008 的实现情况:
   - AC-MVP-001 全年冷热电系统规划: 3 种分辨率同模型; 能量守恒/容量约束
   - AC-MVP-002 求解有效性: MILP 状态表达(可行/界/Gap/停止原因); 财务 IRR 六状态
   - AC-MVP-003 不确定性分析: 固定方案 vs 重规划分离; 种子可复现; 无效样本隔离
   - AC-MVP-004 工程证据: 关键指标参考值与容差; 合成案例
   - AC-MVP-005 账号权限生命周期: 首改密/限速/会话失效/窗口接管/所有权/归档删除/项目包
   - AC-MVP-006 数据离线运维: 导入校验/UTC 偏移/存储门禁/容器重建数据不丢
   - AC-MVP-007 多目标与结果应用: 最低 IRR 硬约束/Pareto/不同结局/应用创建新版本
   - AC-MVP-008 帮助国际化可用性: 中英闭环/术语一致/键盘/WCAG
3. 对每项给出: 已实现/部分实现/未实现 + 证据(文件/端点/测试)
4. 对部分/未实现项, 能补则补(小补丁, 只改项目内文件); 大缺口记录到遗留清单
5. 检查核心不变量(2.3): 浏览器非权威/快照不可变/计算绑定完整/四维独立/受控注册/内容寻址/不执行用户代码
6. 输出最终验收报告(中文): 逐条对照表 + 遗留问题 + 交付说明(如何启动/默认账号/功能清单)`,
  { label: 'accept:review', phase: 'Accept', effort: 'high' }
)

log('验收完成')
return { e2e: !!e2e, accept: !!accept }
