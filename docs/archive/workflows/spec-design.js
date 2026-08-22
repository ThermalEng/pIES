export const meta = {
  name: 'iesplan-spec-design',
  description: 'pIES 核心规格并行设计: DB schema / 物理财务模型 / 任务调度 / 注册表与诊断',
  phases: [
    { title: 'Design', detail: '4 个设计 agent 并行产出规格文档' },
    { title: 'Synthesize', detail: '综合设计冲突与修正' },
  ],
}

const DESIGNERS = [
  {
    key: 'db',
    out: 'docs/spec/01-db-schema.md',
    prompt: `你是 pIES 的数据架构师。设计 PostgreSQL 权威事实的完整关系模式。

## 系统背景
pIES 是综合能源系统(电/热/冷)规划软件。核心理念(必须遵循):
- 浏览器/Redis/日志不是权威;PostgreSQL 保存项目、权限、任务事实、版本、证据和业务索引的事务事实来源。
- 工作草稿可改;项目版本与计算快照不可变(追加式版本化)。
- 大型时序/结果对象放入内容寻址对象存储(磁盘),数据库只存引用+内容校验值(sha256)。
- 计算快照是任务的唯一输入,绑定项目版本、数据集版本、程序/扩展版本、随机种子、容差、内容校验。
- 任务事实:任务/尝试/租约/fencing token 存 PG;队列、进度、心跳存 Redis(可重建)。
- 每个权威实体只有一个写入单元(U01-U16 业务单元)。

## 需要设计的表(以 PG 命名,下划线小写复数)
1. 身份: users, roles, user_roles(或角色列), credentials(哈希、强度、首次改密), window_sessions(状态: active/takeover_pending/revoked/expired, 凭证版本, 替代会话标识), auth_events(审计:登录/登出/失败/改密/重置/停用/接管/权限变化)
2. 权限: project_members(owner/viewer 角色、授权版本、可选过期), ownership_transfers(审计), admin_maintenance_actions
3. 项目: projects(生命周期状态), drafts(综合修订号 revision、内容哈希), project_versions(不可变:名称、说明、创建者、原因、父版本、来源结果、固定 UTC 偏移、货币、模式版本、内容校验), version_refs(版本引用的不可变对象清单)
4. 系统模型: devices(类型、存量/新增、参数 JSONB、模型精度), ports, connections, system_graphs(版本化)
5. 数据集: datasets(元数据), dataset_versions(时间轴、分辨率、UTC 偏移、字段、单位、质量报告、溯源、许可证、内容校验), dataset_files(指向内容寻址对象)
6. 计算配置: calc_configs(参数当前值、变量、目标、约束、最低IRR、算法选择、容差、种子)
7. 快照与任务: calc_snapshots(内容校验), tasks(状态机、类型、业务结局、幂等键), task_attempts(尝试序号、租约、fencing token、心跳、停止原因), task_leases, task_progress(PG 持久进度,Redis 只存可重建部分), task_diagnostics, compute_slots(并发槽)
8. 结果: evidence_packages(不可变), result_assessments(四维:物理/最优性/财务/可靠性), result_index(仅最新评估引用), result_selections, reports(Excel 报告对象引用)
9. 不确定性: uncertainty_snapshots, sample_tasks(父子), sample_records
10. 审计与对象: audit_log(通用), objects(内容寻址对象元数据、引用计数、配额), object_refs, import_proposals, retention_rules

## 要求
- 每张表给出:表名、列(名称/类型/约束/说明)、主键、外键、唯一约束、推荐索引。
- 重要约束用 SQL CHECK 表达(如 window_sessions 每账号最多一个 active —— 用部分唯一索引)。
- 不可变表要设计追加式模式:如何保证"不原地修改"(禁止 UPDATE 该表或仅允许特定列)。
- 时间列一律 TIMESTAMPTZ(UTC);项目同时保存固定 UTC 偏移。
- 金额列用 NUMERIC(18,4),币种列用 TEXT CHECK IN ('CNY','USD')。
- 给出 migration 顺序建议(哪些表先建)。
- 输出为完整中文 Markdown 文档写到 docs/spec/01-db-schema.md,包含全部表定义,不用省略。

你必须实际把文档写入磁盘(用 Write 工具)。`,
  },
  {
    key: 'calc',
    out: 'docs/spec/02-calc-model.md',
    prompt: `你是 pIES 的能源系统数学建模专家。设计计算引擎的数学模型规格。

## 系统背景
综合能源系统(电/热/冷)规划软件,Python 实现。设备范围(版本1):电网连接(购/售电、分时电价、需量费、并网容量、禁止反送电)、光伏(温度/辐照/朝向/倾角)、电池储能(充放互斥/SOC/衰减/循环寿命/更换)、电/热/冷负荷、热泵(供热供冷、性能随温度)、燃气锅炉、电制冷机。支持存量(容量固定)与新增(容量为优化变量)设备。热/冷输配损耗、传输容量、泵耗电。

## 需要设计的内容
1. **时间轴语义**:标准非闰年 365 天,支持 15/30/60 分钟步长(分别 35040/17520/8760 行),固定 UTC 偏移,无夏令时。时间索引如何映射(年度序号 0..N-1)。
2. **单位系统**:内部 SI(J/W/K),接口展示 kWh/MW/°C;单位换算表;每个数值必须携带单位+量纲;金额用精确十进制,矩阵用浮点。
3. **能量平衡模型**:每个时间步:电平衡(电网购电+光伏+电池放电 = 电负荷+热泵耗电+电制冷机耗电+泵耗电+电池充电+售电);热平衡(锅炉+热泵供热 = 热负荷+输配损耗);冷平衡(电制冷机 = 冷负荷+损耗)。约束:禁止反送电(售电=0)、并网容量、负荷满足(默认不允许削减,允许时需惩罚项并显著报告)。
4. **设备模型**(给出数学式):光伏(PV 功率=辐照×面积×效率×(1-温度系数×(Tc-Tref)))、电池(SOC 递推、充放互斥二进制、循环寿命累计、容量衰减、更换决策)、热泵(COP 随环境温度、供热/供冷模式)、燃气锅炉(效率、天然气消耗)、电制冷机(COP/能效)。
5. **优化问题构建**:MILP 框架:连续变量(功率流、容量、能量)+ 二进制变量(设备启停、充放互斥、建设决策、更换)。目标函数:税后项目投资 IRR 最大化(默认单目标)如何转化为可求解形式(建议:先建基准方案,收益=相对基准的节省购能费用+售能收入;目标可转为 NPV 最大化的代理或直接枚举容量后取 IRR)。变量类型:连续/整数/枚举/布尔。默认变量集(新建设备:容量连续变量)。
6. **多目标方法**:加权法、优先级法、ε-约束法、Pareto 解集;所有方法都必须有最低税后项目投资 IRR 硬约束(不可被权重抵消)。
7. **任意方案评价**:固定全部参数,不做容量优化,只求解逐时运行(给定容量下的运行优化)。
8. **逐时结果内容**:逐时功率流、SOC、购售电、能耗、费用、排放。
9. **收敛与停止**:每物理量独立残差/容差/归一化;达到时间上限保存可行解+最优性信息;Gap 定义。
10. **固定方案可靠性 vs 重规划敏感性**的数学差异:固定容量只重优化运行;重规划重优化容量+选择+运行。样本使用可复现种子。

## 输出
完整中文 Markdown 规格文档(数学公式用 LaTeX 格式,代码块中给关键算法的伪代码),写到 docs/spec/02-calc-model.md。必须覆盖以上全部 10 点,可直接作为求解器适配层的实现依据。用 Write 工具实际写入磁盘。`,
  },
  {
    key: 'tasks',
    out: 'docs/spec/03-task-scheduling.md',
    prompt: `你是 pIES 的任务调度与分布式一致性架构师。设计任务状态机与调度协议。

## 系统背景
B/S 应用:FastAPI 后端 + 独立计算 Worker + 独立 I/O Worker + PostgreSQL(权威) + Redis(可重建状态)。计算必须在服务端后台执行,浏览器关闭后继续;计算快照不可变;Worker 只能写有效租约内的尝试数据。

## 设计内容
1. **任务状态机**:排队→运行→完成/取消中→已取消/超时/失败。业务结局独立于技术状态:正常完成/无可推荐方案/无可行多目标方案/批量部分完成/仅产生受限使用结果/证据不足不可用。状态与结局分别保存与展示。
2. **幂等任务创建**:创建任务接受幂等键;相同幂等键重复请求返回原任务。快照内容校验(sha256)去重,相同输入复用快照。
3. **租约与 fencing token 协议**:PG 中 task_attempts 保存当前尝试+租约期限+fencing token;Worker 心跳续租(Redis 心跳可重建,但写入资格由 PG 决定);租约过期或 token 失效的尝试不得提交进度/结果/证据;迟到尝试拒绝;详细时序协议(租约时间线、心跳间隔、过期判定、恢复策略)。
4. **队列与并发槽**:Redis 队列(可重建,丢失后可从 PG 重建);compute 与 io 两套逻辑队列、独立资源额度;默认 2 并发规划槽(目标硬件可 3);超出排队。子任务(不确定性样本)共享全局额度,不能绕过门禁。I/O Worker 与计算 Worker 不同队列。
5. **取消/超时/失败**:取消传播(父子任务)、隔离求解器子进程的终止(信号→强杀→清理孤儿)、8 小时硬超时(管理员可调)、有限重试、失败诊断保存(任务标识、停止阶段、已有可行解、恢复建议)。
6. **进度与心跳**:进度记录哪些字段;哪些进 Redis 哪些进 PG;持久进度(批量任务按已完成子任务恢复)。
7. **存储门禁**:提交前估算存储需求;低于安全阈值阻止提交并提示清理。
8. **状态查询 API 语义**:浏览器如何轮询任务状态与进度;完成后的结果获取。
9. **批量任务(父-子)**:创建、并发配额分配、部分完成结局、按已完成样本恢复、取消传播。
10. **健康与指标**:存活/就绪端点、任务指标、队列指标、存储容量、关联标识(任务/尝试/快照 trace id)。

## 输出
完整中文规格文档(含状态机图 mermaid、租约时序图 mermaid、协议字段表),写到 docs/spec/03-task-scheduling.md。用 Write 工具实际写入磁盘。`,
  },
  {
    key: 'registry',
    out: 'docs/spec/04-registry-diagnostics.md',
    prompt: `你是 pIES 的受控扩展与诊断体系设计师。设计注册表、单位系统与诊断目录。

## 系统背景
pIES 综合能源规划软件。约束:
- 设备、算法、数据源、指标通过受控注册扩展;扩展必须声明标识、版本、兼容性、能力、单位、参数、迁移规则;扩展不能访问用户会话、任意 DB 连接、任意服务器路径;只加载随产品安装并经过测试的受控扩展;不执行用户上传的任意代码;自定义表达式用受限语法+白名单函数。
- 诊断:每条用户可见诊断必须有稳定代码、严重程度、是否阻断、中英消息键、对象/字段/时间位置、修复建议、关联标识。后端只提供诊断数据和消息键,不硬编码 UI 文案。
- 帮助:核心页面/按钮/参数/错误/结果有稳定帮助主题,帮助内容中英双语、离线可读。
- 精度选择:用户可按项目/设备选择模型精度等级,精度选择不改变数据、权限和结果追踪规则。

## 设计内容
1. **受控注册表**:注册表数据结构(注册项:类型、id、版本、声明的能力清单、参数 schema、兼容版本、迁移规则);注册与加载流程(随产品安装、签名或校验和验证、启动时校验);类型目录:设备类型、算法、指标、数据源、单位、表达式函数。
2. **设备类型注册示例**:完整给出 8 种设备(电网连接/光伏/电池/电负荷/热负荷/冷负荷(冷热组合)/热泵/燃气锅炉/电制冷机)的参数 schema:参数名、单位、范围、默认值、存量/新增、是否可为优化变量、帮助主题键。
3. **表达式引擎**:受限语法(仅代数运算、比较、白名单函数)、解析→类型检查→量纲检查→范围检查的流水线;函数白名单(数学函数如 abs/min/max/sin/exp/幂,时间聚合函数);禁止列表(eval/exec/导入/IO/网络)。给出安全评估。
4. **诊断体系**:诊断码命名规范(如 DATA-TS-001, CONN-TYPE-002);严重程度(blocking/error/warning/info);消息键目录结构(如 ies.diag.data.ts_dup);诊断对象 JSON 结构(含 message_key, params, location: {object_type, object_id, field, row}, severity, blocking, code, fix_hint_key, ref_ids)。
5. **帮助主题目录**:帮助主题命名规范;核心主题清单(建模/连接/数据导入/校验/参数/规划配置/任务/结果/四维有效性/不确定性/导出/项目包/账号/权限/离线);主题与页面/参数/诊断的关联方式(通过元数据键)。
6. **模型精度**:精度等级定义(如 1=简化线性、2=标准、3=详细非线性);每级对设备模型的差异;精度元数据如何进入计算快照。
7. **单位注册**:单位类别(能量/功率/温度/金额/时长/角度)、SI 基准、换算系数、展示格式(中英);量纲运算规则。
8. **中英消息键目录**:给出一套 JSON 结构的消息键模板 + 至少 40 个具体消息键示例(覆盖登录、校验、任务、结果、存储),中英文各一。

## 输出
完整中文规格文档(含 JSON 示例),写到 docs/spec/04-registry-diagnostics.md。用 Write 工具实际写入磁盘。`,
  },
]

phase('Design')
const results = await parallel(DESIGNERS.map((d) => () =>
  agent(d.prompt, { label: `design:${d.key}`, phase: 'Design', effort: 'high' })
))

phase('Synthesize')
const done = results.filter(Boolean)
const synthesis = await agent(
  `你负责 pIES 设计阶段的综合审查。下面 4 份设计规格文档已经写入磁盘(它们是并行产出的,可能存在术语不一致、接口不一致或冲突):
- docs/spec/01-db-schema.md (数据库 schema)
- docs/spec/02-calc-model.md (计算模型)
- docs/spec/03-task-scheduling.md (任务调度)
- docs/spec/04-registry-diagnostics.md (注册表与诊断)

请:
1. 逐个打开阅读(Read 工具)。
2. 找出跨文档的不一致(如:同一实体在不同文档的表名/字段名不同、时间戳语义冲突、状态枚举不一致、诊断码风格不一致、单位体系冲突)。
3. 直接在文档中修正(Edit 工具),保证关键术语统一:表名(users, projects, drafts, project_versions, system_graphs, datasets, dataset_versions, calc_snapshots, tasks, task_attempts, evidence_packages, result_assessments, result_index, objects, audit_log, window_sessions)、时间戳一律 TIMESTAMPTZ、诊断码格式(域-类别-编号)、任务状态枚举(queued/running/completed/cancelling/cancelled/timed_out/failed)、业务结局枚举(normal_completion/no_recommendation/no_feasible_multi_objective/partial_batch/restricted_results/insufficient_evidence)。
4. 输出一份简短的协调报告:修正了哪些不一致、遗留风险。

用中文输出最终报告。`,
  { label: 'synthesize:specs', phase: 'Synthesize', effort: 'high' }
)

log('设计规格完成')
return { synthesized: !!synthesis }
