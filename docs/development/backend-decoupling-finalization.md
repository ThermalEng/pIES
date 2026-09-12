# 后端依赖架构最终收口

> 文档类型：临时实施指导；状态：执行中；基线：`c6abb6c`；日期：2026-09-13。
>
> 本文件不是长期架构规范，不改变产品版本，也不替代正式手册。权威要求以
> [架构宪法](../../manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md)、
> 各模块手册和 Roadmap 为准。任务完成并经审查后删除本文件；稳定事实只更新到
> 更新日志，不能把本文件保留成第二套代码或第二套架构正文。

## 一、为什么还需要本轮收口

`c6abb6c` 已完成重要的第一阶段：旧 `services/` 已删除，ORM 基本收回各领域
`persistence`，application 对旧 services 和裸 SQL 的依赖归零，Worker 的事务已上收，
storage 对 audit 的反向依赖已解除，Docker 全量测试为 1454 通过。

但“旧依赖消失”不等于目标架构已经形成。当前仍存在四类问题：

1. 门禁漏检和过期白名单使少量穿透能够在全量测试绿色时继续存在；
2. API、application、领域模块之间仍有绕过规定方向的调用；
3. 一部分领域规则只是从 services 搬到 application，形成新的大模块和多份事实源；
4. 0.8 计算能力尚未实现，但旧计算残体和 analysis 驱动 engine 的形状仍会诱导后续实现走回旧路。

本轮目的不是追求目录形式，也不是提前实现 0.8，而是让现有后端真正收敛到稳定依赖方向，
使 0.8 只需沿明确边界增加实现。

## 二、最终必须得到什么

完成后，生产代码的主依赖方向必须为：

```text
API / Worker
    ↓
application（用例、事务、跨领域编排）
    ↓
领域公开门面与不可变 contract
    ↓
各领域自己的 persistence
    ↓
ORM / storage adapter
```

并为 0.8 保留下列唯一计算方向，不在本轮实现真实生成器或求解器：

```text
application.worker
    → GeneratorProvider
    → Solver Bundle
    → SolverRuntime
    → ResultAdapter
    → ComputeResult
    → analysis / metrics
```

终态要求：

- API 只负责路由、认证、DTO、错误映射和调用一个完整 application 用例；不直接调用领域行为组织业务流程。
- application 只拥有事务、步骤顺序和跨领域协调；单领域规则、状态映射和领域错误归领域所有者。
- 领域之间不直接组合业务；跨领域授权和工作流由 application 完成。
- 外部调用只依赖公开门面或不可变 contract，不导入他域 `persistence`、`repository`、loader、内部 registry 或 ORM。
- 一个规则、常量、状态映射只有一个权威所有者，其他位置直接复用，不复制并用测试锁定相等。
- 没有仅供测试证明“架构存在”、但生产代码完全不用的抽象层。
- Worker 不拼 solver 命令、不解释装配、不直接承担结果分析；analysis 不构造 solver plan、不调用 engine，只消费统一计算结果。
- 0.8 未实现的计算入口继续显式失败，不恢复旧计算链，不保留 `raise` 后不可达的旧实现。

## 三、已知问题基线

以下是最低整改集合，不代表只做字符串替换：

1. `application/namespace.py` 的 `get_or_allocate_namespace()` 直接导入 identity ORM，且当前无生产调用。
2. `test_architecture_gates.py` 的跨表白名单仍含已删除的 services、已整改的 Worker 项和
   `application.namespace → identity`；门禁只检查新增项，没有检查过期项。
3. 用户名、邮箱正则在 models、application、identity 多处复制；项目所有者能力集在 project 与
   application.tasks 复制。
4. API 的 config、validation、model 等路由直接调用 `project.ensure_access` 或 devices 行为，
   个别路由仍自己组织“授权—加载—校验—保存”流程。
5. `project/access.py` 直接依赖 identity 领域完成管理员组合授权。
6. `application/results/writes.py`、`application/identity/service.py`、
   `application/tasks/submissions.py`、`application/packages/transfers.py` 等仍混有领域规则、错误、映射、
   持久化编排与事务；不能仅按行数机械拆文件，必须按权威所有者拆职责。
7. 各领域的 `*Repository` Protocol 在生产代码中没有注入或实现消费，只被导出并由测试检查存在；
   当前真实调用是公开门面对 persistence 函数的别名。
8. `worker/executors.py` 的未实现入口在 `raise` 后仍保留旧 plan/engine/求解代码，Worker 仍直接导入
   engines 与 metrics。
9. `analysis/wrapper.py` 仍以 `_local_plan()` 构造计划，并由 `run_sweep/run_batch` 调用注入 engine；
   这与“ComputeResult → analysis”方向相反。
10. 更新日志已提前声明“后端解耦纠偏完成”；只有本文件全部退出条件满足后才可保留该结论。

## 四、范围与明确排除

### 本轮范围

- 修复以上已知问题及同类依赖问题；
- 收紧静态架构门禁，让当前终态可持续；
- 将单领域规则下沉到领域公开能力，保留 application 的事务与编排；
- 清除旧计算残体，把 analysis/Worker 调整成等待 0.8 正式计算边界的正确形状；
- 同步必要测试、代码注释和更新日志事实。

### 明确排除

- 不实现真实 GeneratorProvider、Solver Bundle 生成、SolverRuntime、ResultAdapter 或 solver；
- 不开发 0.8 用户功能、算法插件上传、运行期热加载、GUI 或选择器；
- 不修改架构宪法；发现规范冲突必须停止并报告；
- 不保留 1.0 兼容入口、别名、fallback、旧响应并集或不可达实现；
- 不增加内部文件 hash、重复校验、假想防御、备用路径或无业务证据的抽象；
- 不因重构顺便修改无关业务语义、数据库结构或公开 HTTP 契约。

## 五、实施波次与并行方式

使用“波次内并行、波次间串行集成”。各子 agent 必须使用独立工作树和独立分支；协调者负责逐项
review、合并、解决冲突和在集成 HEAD 上验证。禁止多个 agent 同时修改同一工作树。

### Wave 0：基线和门禁设计（串行）

- 记录真实生产依赖图和每个违规位置；区分公开 contract 复用与领域行为绕行。
- 将债务白名单改为检测结果精确相等，删除所有过期条目。
- 增加能够捕获 `application → models/ORM`、API 直接调用领域行为、禁止的领域间依赖、Worker
  计算穿透和 analysis 驱动 engine 的门禁。
- 门禁应检查依赖事实，不绑定私有文件名或强迫多一层包装；不能为了门禁复制实现。
- 不在集成分支提交故意失败的门禁；每项门禁与对应整改在同一可通过切片落地。

### Wave 1：残留与虚假抽象（可并行）

切片 A：删除未使用的 namespace ORM helper，或在确有调用证据时下沉到 identity 公共能力。

切片 B：统一用户名、邮箱和项目能力常量的权威所有者，删除“复制同值、测试锁相等”。

切片 C：审计全部 Repository Protocol。生产代码没有真实端口注入需求的直接删除，并调整门面与测试；
不得为保留 Protocol 人为增加容器、工厂或依赖注入框架。

### Wave 2：API 与跨领域边界（可并行，完成后串行集成）

切片 A：把 config、validation、model 等 API 中的授权和业务步骤收进完整 application 用例。
API 可使用公开 DTO/不可变 contract 做传输映射，但不得直接调用领域持久化或领域行为组织工作流。

切片 B：把 `project → identity` 的组合授权上收到 application 授权用例。project 只回答项目自身事实，
identity 只回答身份/角色事实，application 组合两者。

切片 C：扫描其他 API 和领域模块，按同一原则消除同类绕行；不得用新“common service”重新集中耦合。

### Wave 3：领域规则回归所有者（按互不重叠领域并行）

- identity：输入规则、身份错误和身份状态归 identity；application.identity 只编排审计、项目等跨域步骤与事务。
- results：证据/评估状态、结果错误、状态映射和四维评估规则归 results；application.results 只编排
  tasks/project/storage/audit/results。
- tasks：任务状态、业务结局映射和任务错误归 tasks；application.tasks 只编排提交、幂等、快照和事务。
- package/model-template 等大模块按相同标准审计：纯包规则归 package，纯模型规则归 model/devices，
  跨域导入顺序与事务留在 application。

禁止仅把一个千行文件拆成多个 application 文件后宣称完成；判断标准是规则所有权和依赖方向。

### Wave 4：0.8 前计算边界清场（Wave 3 后进行）

- 删除 Worker 未实现入口 `raise` 后的全部旧计算代码、旧 selector、旧命令注册、旧 plan 构造和对应测试。
- Worker 仅保留任务领取、租约、调用 application.worker、隔离执行壳和明确未实现错误所需的最小代码。
- analysis 改为消费统一、不可变的计算结果/扫描点结果并做纯分析；删除 `_local_plan` 和直接调用 engine
  的职责。批量/敏感性“生成多个计算请求并调度”的职责预留给 application/0.8，不在 analysis 内执行。
- 只有正式模块手册已经明确稳定字段时，才可建立最小 `computation` contract；不得凭旧代码猜测字段，
  不得创建无生产消费者的占位 Protocol。若现行规范不足以完成契约，停止该小项并报告规范缺口，
  其余清场继续完成。
- 计算仍未实现时，任务必须保持结构化、可见失败；不伪造成功、不 fallback 到旧引擎。

### Wave 5：集成收尾（串行）

- 在集成 HEAD 重新生成真实依赖图，逐项对照第二节终态。
- 清理过期测试名、services 对照描述、迁移注释、白名单和不可达代码。
- 更新日志只能写实际完成事实；如果仍有不满足项，就改回“阶段完成”而不是宣称全部完成。
- 删除本临时指导文件，确认现行手册没有复制本次实现细节。

## 六、复用与抽象裁决

每次保留或新增抽象时必须回答：

1. 至少有两个真实生产消费者，还是只有测试引用？
2. 它是否稳定隐藏了一个会变化的实现，还是仅转发同名函数？
3. 删除它后是否会迫使调用方穿透领域内部？
4. 它是否制造第二份状态、映射、默认值或错误语义？

推荐保留的复用：无状态 core 基元、不可变 contract、领域公开纯函数、领域公开门面、storage adapter。
应删除的“复用”：复制常量后测试相等、无消费者 Protocol、只改名转发、跨领域 common service、兼容包装。

## 七、验证和提交要求

- 所有编译、格式化和测试只在 Docker 中执行，不在主机运行项目 Python/Node 依赖。
- 每个切片先运行受影响的最小测试和架构门禁；每个波次集成后运行相关测试；最终集成 HEAD 只需一次
  Docker 全量测试。局部修改不反复运行无关全量套件。
- 架构门禁至少证明：
  - API 无 ORM、无 persistence、无直接领域行为工作流；
  - application 无 models/ORM 和他域内部模块；
  - 领域持久化只访问本领域表，禁止的领域间业务依赖为零；
  - Worker 无事务、ORM、旧 services、旧 engine selector/命令拼装；
  - analysis 无 engine 调用和 plan 装配；
  - 所有临时债务集合与真实检测精确相等，最终应为空。
- 测试必须验证公开行为、事务、权限和错误语义；不得为了锁定文件布局、转发层或复制常量增加测试。
- 每个独立切片 review 通过后单独提交；提交信息说明职责迁移，不写“misc cleanup”。
- Docker 测试结束后删除本轮生成的非基础镜像和孤儿容器，不删除基础镜像及项目正常基础设施。
- 最终报告列出：提交、依赖图变化、删除的旧入口/抽象、Docker 命令与结果、残余问题。不得只报告测试数。

## 八、完成定义

只有同时满足以下条件才可报告完成：

1. 第二节依赖方向在生产代码中成立，第三节全部问题已关闭或有明确规范阻塞证据；
2. 不存在 application/API/Worker 对 ORM、他域 persistence/repository/内部 registry 的穿透；
3. API 不再自行组织授权与多步骤业务流程，领域间组合只在 application；
4. application 中不再拥有明确的单领域规则副本，权威常量和映射均唯一；
5. 无仅由测试引用的 Repository Protocol 或等价虚假抽象；
6. Worker 与 analysis 的旧计算方向已经清除，未实现计算保持明确失败；
7. 静态门禁无过期白名单，检测与债务集合精确一致且最终为空；
8. Docker 相关测试和最终全量测试通过，工作树干净，非基础测试资源已清理；
9. 更新日志陈述与实际完成范围一致，本临时文件已删除。
