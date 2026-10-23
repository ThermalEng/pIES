export const meta = {
  name: 'iesplan-backend-units',
  description: '后端业务单元: 身份/项目/模型/数据/配置/校验/任务/Worker/结果/项目包/审计',
  phases: [
    { title: 'Wave1', detail: '身份与项目基础: auth, project, model, dataset, config' },
    { title: 'Wave2', detail: '校验/任务/对象存储: validation, tasks, objects' },
    { title: 'Wave3', detail: 'Worker/结果/项目包/审计 + API 装配' },
    { title: 'Integrate', detail: '集成测试修复' },
  ],
}

const WAVE1 = [
  {
    key: 'auth',
    prompt: `你是 pIES 后端的身份与认证单元(U01)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/identity.py (users/roles/user_roles/credentials/window_sessions/auth_events 已实现)
- /home/mc/Documents/工作文档/pIES/iesplan/models/audit.py
- /home/mc/Documents/工作文档/pIES/iesplan/core/security.py (hash_password/verify_password/token_hash/new_session_token)
- /home/mc/Documents/工作文档/pIES/iesplan/db.py, iesplan/config.py, iesplan/core/errors.py, iesplan/core/diagnostics.py
- 设计输入 RPD 第3节(用户/权限/会话)与 01-db-schema.md 第1节(身份)

## 任务: 实现 iesplan/api/auth.py + iesplan/services/identity.py (可拆多个文件)
1. **身份服务** iesplan/services/identity.py:
   - create_user(db, username, password, role, force_password_change) -> User
   - authenticate(db, username, password) -> (User, error_code|None): 登录限速(同一用户名失败计数, 存内存 dict + 简单时间窗; 5 次失败锁 15 分钟)
   - change_password(db, user, old, new): 校验旧密码、强度、首次改密状态
   - reset_password(db, admin, target_user, new_tmp): 临时密码 + force_password_change=True + 使会话失效
   - deactivate_user / reactivate_user (管理员)
   - create_window_session(db, user, device_info) -> WindowSession: 按 RPD 3.3 单活动窗口: 若已有 active 会话则置为 takeover_pending, 然后新会话 active; 返回 (session, old_session_revoked)
   - revoke_other_sessions / expire_sessions
   - auth 审计: 写 auth_events(登录/登出/失败/改密/重置/停用/接管/权限变化)
   - 会话过期: session_ttl_minutes 检查; last_activity 更新
2. **认证 API** iesplan/api/auth.py (FastAPI router, prefix /api/auth):
   - POST /api/auth/login {username, password, device} -> {token(窗口凭证), user{id,username,role,force_password_change}, needs_takeover_confirm}
   - POST /api/auth/logout (需要凭证)
   - POST /api/auth/change-password {old_password, new_password}
   - POST /api/auth/refresh (会话续期)
   - POST /api/auth/confirm-takeover (确认接管, 返回新窗口凭证)
   - 依赖: 实现 get_current_user (从 Cookie/Header 读窗口凭证, 校验哈希+过期+active), get_current_admin
   - 注册: 默认关闭; 配置开启时 POST /api/auth/register 只能创建 engineer
   - 管理员: GET /api/auth/users(列表), POST /api/auth/users/{id}/reset-password, POST /api/auth/users/{id}/deactivate, POST /api/auth/users/{id}/reactivate, PUT /api/auth/settings(注册开关)
3. **安全**: bcrypt 哈希存储; 响应不泄露堆栈/哈希; 错误用 AppError + 诊断消息键(如 ies.diag.auth.*); 登录失败消息统一不区分用户不存在/密码错误

## 测试
tests/test_auth_api.py (TestClient + 内存 sqlite 或依赖注入假 DB), 覆盖: 登录成功/失败限速/首次改密/窗口接管(旧会话 revoked)/登出/管理员重置密码使会话失效/注册开关。
注意: main.py 的 get_db 依赖如何替换 —— 用 app.dependency_overrides。

## 注意
- 只写 iesplan/services/identity.py, iesplan/api/auth.py, tests/test_auth_api.py (以及需要的 iesplan/services/__init__.py)
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'project',
    prompt: `你是 pIES 后端的项目单元(U02/U03)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/project.py (projects/drafts/project_versions/version_refs), iesplan/models/audit.py
- /home/mc/Documents/工作文档/pIES/iesplan/db.py, iesplan/core/errors.py, iesplan/core/diagnostics.py, iesplan/core/idgen.py
- 设计输入 RPD 第3.2节(项目权限)/第5节(项目生命周期)/第17.2节(项目约束), 01-db-schema.md 第2-3节(权限/项目)

## 任务: 实现 iesplan/services/project.py + iesplan/api/projects.py
1. **访问控制服务** (U02):
   - ensure_access(db, user, project_id, *capabilities) -> None (抛 ForbiddenError)
   - get_role(db, user, project_id) -> owner|viewer|None
   - add_viewer / remove_viewer (仅所有者), transfer_ownership (原所有者→viewer, 审计), maintenance_access(管理员: 只读)
2. **项目服务** (U03):
   - create_project(db, user, name, currency, utc_offset_minutes) -> Project: 创建者=所有者, 创建初始草稿(revision=1)
   - get_project_view(db, user, project_id) -> 项目+草稿摘要+版本列表
   - update_draft(db, user, project_id, commands: list[dict], expected_revision) -> new_revision: 语义命令(20.3 草稿命令: 幂等命令标识/预期修订/窗口会话/唯一写入单元/类型与负载), 乐观锁(预期修订不符→ConflictError), 领域修改+修订递增同一事务
   - create_version(db, user, project_id, name, description, reason, parent_version_id=None, source_result_id=None) -> ProjectVersion: 快照当前草稿内容(模型/布局/数据集绑定/计算配置/语言/货币/UTC偏移/受控扩展清单/内容校验), 不可变
   - archive_project / unarchive_project, delete_project(按 RPD 5.3: 确认→取消任务→一致性检查→硬删除), duplicate_project(复制为独立候选方案)
   - restore_version(创建新版本+新草稿, 不倒写历史)
   - apply_result(应用选定结果: 参数差异补丁应用到新草稿, 创建新版本, 原版本不变)
   - 审计事件写 audit_log
3. **项目 API** iesplan/api/projects.py (prefix /api/projects):
   - POST /api/projects (创建) {name, currency, utc_offset_minutes}
   - GET /api/projects (我可见的项目列表: 所有者+查看者)
   - GET /api/projects/{id} (项目视图)
   - PUT /api/projects/{id}/draft (语义命令批量) {expected_revision, commands: [...]} -> {revision, results}
   - POST /api/projects/{id}/versions {name, description, reason}
   - GET /api/projects/{id}/versions, GET /api/projects/{id}/versions/{vid}
   - POST /api/projects/{id}/archive | unarchive
   - DELETE /api/projects/{id} {confirm: true}
   - POST /api/projects/{id}/duplicate
   - POST /api/projects/{id}/transfer {target_user_id}
   - PUT /api/projects/{id}/viewers {user_id, action: add|remove}

## 测试
tests/test_project_api.py: 创建→添加查看者→查看者读权限(403 编辑)→所有者编辑→版本创建→归档后禁止编辑→删除流程→所有权转移后原所有者变 viewer→审计事件存在。

## 注意
- 只写 iesplan/services/project.py, iesplan/api/projects.py, tests/test_project_api.py
- 草稿命令按 20.3 语义; revision 递增必须与领域修改同事务
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'model',
    prompt: `你是 pIES 后端的系统模型单元(U04)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/model.py (system_graphs/devices/ports/connections)
- /home/mc/Documents/工作文档/pIES/iesplan/core/registry.py (9 类设备类型与参数 schema)
- /home/mc/Documents/工作文档/pIES/iesplan/db.py, iesplan/core/errors.py, iesplan/core/diagnostics.py
- 设计输入 RPD 第7节(建模与设备)/17.3(模型约束), 04-registry-diagnostics.md 第3节(设备参数)

## 任务: 实现 iesplan/services/model.py + iesplan/api/model.py
1. **模型服务** (U04):
   - create_device(db, project_id, device_type, name, params, is_existing, model_precision, position) -> Device: 校验设备类型存在、参数按 registry schema 校验(类型/单位/范围/必填), 生成端口(按设备类型的能源载体: 如 heat_pump 有 electric_in, heat_out, cool_out)
   - update_device / delete_device(级联删除端口与连接, 校验引用)
   - connect(db, project_id, from_port_id, to_port_id, attrs) -> Connection: 校验端口能源类型一致、方向兼容(源→汇)、端口属于同一项目、无重复连接; 不兼容返回带定位的诊断(ConnectionError 或返回 diagnostics 列表)
   - disconnect / update_connection
   - get_graph(db, project_id) -> SystemGraph: 拓扑(设备/端口/连接) + 布局对象
   - 拓扑校验: validate_topology(graph) -> list[Diagnostic]: 孤立设备(无任何连接)警告、未连接负荷警告、能源不平衡(某载体只有源无汇)错误、重复连接错误
   - 参数校验: validate_device_params(device_type, params) -> list[Diagnostic]
   - 图内容: content_hash = sha256(规范化 JSON) 保存
2. **模型 API** iesplan/api/model.py (prefix /api/projects/{id}/model):
   - GET /api/projects/{id}/model (图: 设备+端口+连接+布局)
   - POST /api/projects/{id}/model/devices {device_type, name, params, is_existing, model_precision, position}
   - PUT /api/projects/{id}/model/devices/{device_id} (更新参数/位置/名称)
   - DELETE /api/projects/{id}/model/devices/{device_id}
   - POST /api/projects/{id}/model/connections {from_port_id, to_port_id, attrs}
   - DELETE /api/projects/{id}/model/connections/{conn_id}
   - GET /api/projects/{id}/model/validate (拓扑+参数诊断, 返回 diagnostics 列表)
   - GET /api/registry/device-types (公开: 设备类型+参数 schema, 供前端画布)

## 测试
tests/test_model_api.py: 创建设备(参数范围错误被拒)→连接成功→错误能源类型连接被拒(诊断定位)→孤立设备警告→拓扑校验→图序列化往返→内容哈希稳定。

## 注意
- 只写 iesplan/services/model.py, iesplan/api/model.py, tests/test_model_api.py
- 端口方向约定: 源端口(如 grid 输出) 方向=out, 消费端口(load 输入) 方向=in; 连接要求 from_port.direction='out' 且 to_port.direction='in' 且能源类型一致
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'dataset',
    prompt: `你是 pIES 后端的数据集单元(U05)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/dataset.py (datasets/dataset_versions/dataset_files), iesplan/models/audit.py
- /home/mc/Documents/工作文档/pIES/iesplan/core/timeaxis.py (TimeAxis/build_axis/validate_timestamps), iesplan/core/diagnostics.py, iesplan/core/idgen.py
- /home/mc/Documents/工作文档/pIES/iesplan/db.py, iesplan/config.py, iesplan/core/errors.py
- 设计输入 RPD 第8节(数据约束)/17.4(数据约束), 01-db-schema.md 第5节(数据集)

## 任务: 实现 iesplan/services/dataset.py + iesplan/api/datasets.py
1. **数据集服务** (U05):
   - 标准 CSV 模板: 生成模板(字段说明/单位/示例, 双语注释行) get_template(resolution) -> CSV 字节
   - parse_csv(bytes, resolution) -> (rows, diagnostics): 解析并定位文件/字段/行号错误
   - validate_dataset(df, resolution, utc_offset_minutes) -> (TimeAxis, normalized_df, diagnostics): 校验行数(35040/17520/8760)、时间戳严格递增无重复、无缺失值、单位与范围(负荷≥0, 温度合理 -40..60C, GHI 0..1500 W/m2, 电价≥0)、UTC 偏移固定
   - 阻断性错误阻止提交(blocking=True 的 diagnostic 存在即失败)
   - create_dataset(db, project_id, name, source_category, license, provenance) -> Dataset
   - upload_dataset_version(db, dataset_id, resolution, utc_offset_minutes, fields: dict, data_bytes, meta) -> DatasetVersion: 校验通过后写内容寻址对象(见下), 生成质量报告, 保存溯源/许可证/适用范围
   - list_dataset_versions, get_dataset_version(含数据引用)
   - 内置样例数据: create_builtin_sample(db, project_id, resolution, region='shanghai') -> DatasetVersion: 用确定性伪随机生成合成 365 天样例(电/热/冷负荷+温度+GHI+分时电价+排放因子, 负荷有季节与日模式), 保存为内置数据(来源类别=builtin_sample, 记录地区/时间范围/许可证/溯源)
2. **对象存储接入**: 用 iesplan/services/objects.py 若存在; 否则最小实现: 大对象写 data_dir/objects/{sha256} 文件, objects 表记录引用(见 01 第10.1节)。为后续阶段预留接口。
3. **数据集 API** iesplan/api/datasets.py (prefix /api/projects/{id}/datasets):
   - GET /api/datasets/template?resolution=1h (公开, 返回 CSV 下载)
   - POST /api/projects/{id}/datasets {name, source_category, license, provenance}
   - POST /api/projects/{id}/datasets/{ds_id}/versions (multipart: file, resolution, utc_offset_minutes, fields 描述) -> {dataset_version, quality_report, diagnostics}
   - GET /api/projects/{id}/datasets (列表+最新版本)
   - GET /api/projects/{id}/datasets/{ds_id} (版本列表+质量报告)
   - POST /api/projects/{id}/datasets/{ds_id}/sample (生成内置样例数据)
   - GET /api/projects/{id}/datasets/{ds_id}/versions/{vid} (元数据+溯源, 数据文件不直接返回)

## 测试
tests/test_dataset_api.py: 模板生成→构造合法 CSV(用小 n 分辨率, 如 15min 需要 35040 行, 测试用小分辨率或用 1h 但只测校验逻辑函数)→上传→校验通过→坏数据(乱序/重复/缺行)被阻断→样例数据生成→质量报告存在。

## 注意
- 测试时避免超大文件: 校验逻辑函数可直接单测(行数参数化), API 测试用 1h 分辨率但只造少量行验证错误路径(行数不足会被阻断)或直接单测校验函数
- 只写 iesplan/services/dataset.py, iesplan/api/datasets.py, tests/test_dataset_api.py
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'config',
    prompt: `你是 pIES 后端的计算配置单元(U06)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/calc.py (calc_configs 等)
- /home/mc/Documents/工作文档/pIES/iesplan/core/registry.py, iesplan/core/expression.py, iesplan/core/units.py, iesplan/core/errors.py, iesplan/core/diagnostics.py
- /home/mc/Documents/工作文档/pIES/iesplan/db.py
- 设计输入 RPD 第9.2节(参数变量目标约束)/17.5(计算约束), 02-calc-model.md 第5节(优化构建/目标), 01-db-schema.md 第6节(calc_configs)

## 任务: 实现 iesplan/services/config.py + iesplan/api/config.py
1. **配置服务** (U06):
   - 计算配置结构: {parameters: {设备参数当前值(按设备类型默认), 经济参数, 环境参数}, variables: [{name, type(continuous|integer|enum|boolean), initial, min, max, device_ref}], objectives: [{metric, direction(max/min), weight}], constraints: [{type: predefined|expression, payload}], algorithm: {mode: auto|manual, name}, irr_floor: Decimal, tolerance: {...}, random_seed: int}
   - get_default_config(project_id) -> 基于系统模型设备清单生成默认参数(registry 默认值)与默认变量(新建设备容量为 continuous 变量, 存量固定), 默认目标=税后项目投资 IRR 最大化, 默认最低 IRR 硬约束(如 0.08)
   - save_config(db, project_id, config, expected_revision): 与草稿修订绑定
   - validate_config(config, graph, data_version_ref) -> list[Diagnostic]: 变量类型/初始值在界内/目标合法/约束表达式解析+量纲+范围(用 expression.parse_expr)/IRR 硬约束与折现率是两个独立字段/算法兼容性(auto 不查)
   - get_config(project_id) -> 当前配置(带每个参数的单位/范围/帮助键元数据)
2. **配置 API** iesplan/api/config.py (prefix /api/projects/{id}/config):
   - GET /api/projects/{id}/config (当前配置+参数元数据)
   - PUT /api/projects/{id}/config {config, expected_revision} (保存, 校验不通过返回 diagnostics)
   - POST /api/projects/{id}/config/validate (只校验不保存)
   - GET /api/projects/{id}/config/default (重新生成默认)
   - GET /api/registry/algorithms (算法列表+能力)

## 测试
tests/test_config_api.py: 默认配置生成(新建设备有容量变量)→保存→非法变量界被拒→表达式约束解析错误诊断→IRR 与折现率独立→算法不兼容拒绝。

## 注意
- 只写 iesplan/services/config.py, iesplan/api/config.py, tests/test_config_api.py
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
]

const WAVE2 = [
  {
    key: 'validation',
    prompt: `你是 pIES 后端的项目校验单元(U07)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/services/model.py, iesplan/services/config.py, iesplan/services/dataset.py (上一波次已实现; 若签名与预期不同, 按实际适配)
- /home/mc/Documents/工作文档/pIES/iesplan/core/diagnostics.py, iesplan/core/errors.py
- 设计输入 RPD 第9.3节(任意方案评价)/17.5.7 REQ-CALC-007(校验门禁)/7.2(图形化模型)

## 任务: 实现 iesplan/services/validation.py + iesplan/api/validation.py
1. **校验服务** (U07): 聚合模型/数据/参数/目标/约束/算法能力/财务基准确认证据:
   - validate_project(db, project_id, include_data=True) -> ValidationReport: {status: ok|blocked|warnings, diagnostics: [Diagnostic...], blocks_submit: bool}
   - 覆盖检查项(每项产出诊断):
     a. 模型完整性: 至少 1 个电网连接与 1 个负荷; 电/热/冷每个有负荷的载体都有供给设备; 拓扑校验(调用 model.validate_topology)
     b. 参数: 所有参数有合法当前值(范围/单位); 所有变量有初始值且在界内(调用 config.validate_config)
     c. 数据: 至少一个数据集版本绑定; 所选数据集版本有效(行数/时间轴/无阻断错误); 数据与项目 UTC 偏移一致
     d. 配置: 目标/约束/算法兼容; IRR 硬约束存在
     e. 财务基准确认: 必须有用户确认证据(确认人/确认内容校验信息) —— 提供 mark_baseline_confirmed(db, project_id, user, assumptions_hash) API 记录确认
     f. 计算就绪: 快照可组装(项目版本存在或草稿可固化)
   - 一次返回全部问题; 阻断错误(warning 不能降级 blocking)
2. **校验 API** iesplan/api/validation.py (prefix /api/projects/{id}/validation):
   - POST /api/projects/{id}/validation/run (执行完整预检, 返回 ValidationReport)
   - POST /api/projects/{id}/validation/baseline-confirm {assumptions: dict} (财务基准确认, 记录用户/时间/内容校验)
   - GET /api/projects/{id}/validation (最近报告)

## 测试
tests/test_validation_api.py: 空项目(缺设备)→blocked; 补全设备与数据→通过; 财务基准确认前后状态变化; 阻断错误不被警告降级。

## 注意
- 只写 iesplan/services/validation.py, iesplan/api/validation.py, tests/test_validation_api.py
- 若依赖的 services 函数签名与预期不符, 可小幅调整被依赖服务(但只能修改 services 文件, 不修改 api)
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'tasks',
    prompt: `你是 pIES 后端的任务调度单元(U08)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/docs/spec/03-task-scheduling.md (任务调度完整协议)
- /home/mc/Documents/工作文档/pIES/iesplan/models/calc.py (calc_snapshots/tasks/task_attempts/task_leases/task_progress/task_diagnostics/compute_slots), iesplan/models/result.py
- /home/mc/Documents/工作文档/pIES/iesplan/core/errors.py, iesplan/core/idgen.py, iesplan/core/diagnostics.py, iesplan/config.py, iesplan/db.py
- 设计输入 RPD 第9.4/9.5节(后台任务/任务事实)

## 任务: 实现 iesplan/services/tasks.py + iesplan/api/tasks.py (U08 任务与资源调度)
1. **快照服务**:
   - assemble_snapshot(db, project_id, task_type) -> CalcSnapshot: 从项目版本(或草稿固化)+数据集版本+计算配置组装不可变快照(绑定版本/参数/变量/目标/约束/程序版本/随机种子/容差/内容校验 sha256); 相同内容复用已有快照(去重)
2. **任务服务**:
   - create_task(db, user, project_id, task_type, config=None, idempotency_key=None, parent_task_id=None) -> Task: 幂等键命中返回原任务; 校验门禁通过才能创建(调用 validation 服务或内联最小校验); 存储门禁: 估算存储需求(快照+逐时结果+样本), 可用空间低于阈值→拒绝并给清理建议
   - 任务状态机: queued→running→completed/cancelling→cancelled/timed_out/failed; business_outcome 独立枚举; 终态约束(不可从终态迁移)
   - claim_and_run: cancel_task(用户取消, 传播子任务), retry_task(重试复用同一快照)
   - 并发槽: acquire_slot / release_slot (compute_slots 表, 默认 2 槽; 无槽则任务保持 queued)
   - 进度: record_progress(task_id, stage, percent, message)
   - 任务列表/详情/状态查询
3. **任务 API** iesplan/api/tasks.py (prefix /api/projects/{id}/tasks):
   - POST /api/projects/{id}/tasks {task_type, idempotency_key?, parent_task_id?} (方案评价/规划/不确定性/检查)
   - GET /api/projects/{id}/tasks (列表: 状态/结局/进度/时间)
   - GET /api/projects/{id}/tasks/{task_id} (详情+进度+诊断)
   - POST /api/projects/{id}/tasks/{task_id}/cancel
   - POST /api/projects/{id}/tasks/{task_id}/retry
   - 重复提交语义: 相同 (project, task_type, config_hash) 短时间重复→拒绝或复用(返回已有任务 + 提示)
4. **Redis 队列接入**: 最小实现 iesplan/services/queue.py: enqueue(task_id, queue='compute'|'io'), dequeue, requeue, 心跳读写; Redis 不可用时降级为内存队列(单进程模式)并记录降级状态
   - 注意: 队列/进度/心跳是可重建状态; 权威事实只在 PG

## 测试
tests/test_tasks_api.py: 幂等创建(同键返回同任务)→状态推进(可注入假执行器)→取消→重试同快照→槽限制(2 并发)→存储门禁(模拟空间不足)。

## 注意
- Worker 消费端在下一波次实现, 本波次提供状态机/API/队列 API
- 只写 iesplan/services/tasks.py, iesplan/services/queue.py, iesplan/api/tasks.py, tests/test_tasks_api.py
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'objects',
    prompt: `你是 pIES 后端的对象存储单元(U11)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/audit.py (objects/object_refs/audit_log 等)
- /home/mc/Documents/工作文档/pIES/iesplan/config.py, iesplan/core/idgen.py, iesplan/core/errors.py
- 设计输入 RPD 第23节(存储与对象生命周期), 01-db-schema.md 第10节(审计与对象)

## 任务: 实现 iesplan/services/objects.py (内容寻址对象存储服务)
1. **写入**:
   - put_object(db, content: bytes, content_type, source_category, ref_type=None, ref_id=None) -> Object: 写临时区 data_dir/objects/tmp/{id}, 计算 sha256, 原子提交(rename)到 data_dir/objects/{sha256}, 建立 objects 表记录(大小/哈希/类型/来源/创建时间)
   - 内容去重: 相同 sha256 已存在→复用对象记录(但业务引用按 object_refs 判断)
   - 任何业务记录不得引用半成品对象: 先写完对象再返回引用
2. **读取与校验**: get_object(db, object_id) -> 内容字节(校验哈希一致, 不一致报错), object_info, verify_object(定期完整性校验: 大小+sha256)
3. **引用**: add_ref(db, object_id, ref_type, ref_id) / remove_ref / list_refs; 引用计数
4. **清理**:
   - safe_cleanup(db, dry_run=True) -> 清理计划: 找出无任何业务引用的对象(按 object_refs 与保留规则表 retention_rules 判断), 返回待清理清单; 执行清理(删文件+删记录), 审计
   - 被项目版本/快照/证据包/报告引用的对象不可清理
5. **存储门禁**: estimate_storage(task_type, n_hours, samples) -> bytes 估算; check_capacity(db) -> {free_bytes, safe_threshold, ok, message} (free = 磁盘剩余; ok=free>阈值)
6. **API** iesplan/api/objects.py (内部使用为主, 少量管理接口):
   - GET /api/admin/storage (存储视图: 用量/对象数/引用数/健康) — 仅管理员
   - POST /api/admin/objects/cleanup {dry_run: true} (两阶段清理: 先计划后执行) — 仅管理员
   - GET /api/admin/health (存储健康: 抽样校验对象哈希)

## 测试
tests/test_objects_api.py: 写入→去重(同内容同哈希)→引用→无引用清理→被引用不可清理→哈希校验失败报错→容量估算。

## 注意
- 使用 data_dir(默认 /data), 目录自动创建
- 只写 iesplan/services/objects.py, iesplan/api/objects.py, tests/test_objects_api.py
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
]

const WAVE3 = [
  {
    key: 'worker',
    prompt: `你是 pIES 后端的计算 Worker 实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/docs/spec/03-task-scheduling.md (租约/fencing/队列协议)
- /home/mc/Documents/工作文档/pIES/iesplan/engines/eval_run.py, iesplan/engines/planning.py, iesplan/metrics/financial.py, iesplan/metrics/environmental.py, iesplan/metrics/engineering.py (基础层已实现)
- /home/mc/Documents/工作文档/pIES/iesplan/services/tasks.py, iesplan/services/queue.py, iesplan/services/objects.py (上一波次实现; 签名以实际为准)
- /home/mc/Documents/工作文档/pIES/iesplan/core/timeaxis.py, iesplan/core/diagnostics.py, iesplan/config.py, iesplan/db.py
- 设计输入 RPD 第9.4/9.5节(后台任务/任务事实与写入资格), 12.2节(计算 Worker 职责)

## 任务: 实现 iesplan/worker/ 包 (计算 Worker 与 I/O Worker 共用框架)
1. **iesplan/worker/main.py**:
   - main() 入口: 连接 Redis+PG, 按 worker_type (compute|io) 订阅对应队列; 循环: dequeue→领取(创建 task_attempt + 租约 + fencing token)→执行→提交; 心跳续租(间隔如 15s); 崩溃恢复(租约过期→新尝试); 信号处理(SIGTERM 优雅退出)
   - 槽门禁: 领取前确认 compute_slots 有空位
2. **iesplan/worker/runner.py**: 任务执行分派:
   - 方案评价 (task_type=eval): 从快照读 plan+data → evaluate_plan → 写逐时结果对象(对象存储)+ KPI + 指标对象
   - 规划 (task_type=plan): planning 引擎 → 候选列表 → IRR/NPV 评估 → 候选对象
   - 不确定性 (task_type=uncertainty): 父任务创建样本子任务计划(样本数/种子), 子任务逐个执行(固定方案可靠性: 只重优化运行; 重规划敏感性: 重优化容量)
   - 结果检查 (task_type=check): 对证据包执行四维检查
   - I/O 任务 (task_type=dataset_process|excel_export|package_import|package_export): 数据集处理/Excel/项目包(本项目包功能在另一 agent, 这里只留分派与占位执行器)
   - 每个执行器: 保存证据包(evidence_packages)与结果评估(result_assessments 四维), 更新 result_index, 业务结局(business_outcome)判定, 任务诊断
3. **iesplan/worker/lease.py**: 租约协议: acquire_attempt(task_id) -> (attempt, token) (PG 事务: 检查任务状态 queued、槽位、并发尝试), renew_lease(attempt_id, token), submit_result(attempt_id, token, payload) (fencing: token 不符/租约过期→拒绝), release(attempt_id, token, outcome)
4. **iesplan/worker/solver_process.py**: 隔离求解器子进程封装:
   - run_solver_isolated(fn, args, timeout_sec, mem_limit_mb) -> 结果: subprocess.Popen + 资源限制(prlimit 或简单 timeouts); 超时→SIGTERM→SIGKILL→清理孤儿; 序列化用 pickle 或 json
   - 计算子任务运行在受控隔离子进程(支撑资源限制/超时/取消/孤立进程清理)
5. **iesplan/worker/executors.py**: 各任务类型的执行函数(调用引擎与指标), 进度报告(record_progress), 取消检查点(每阶段检查任务是否 cancelling)

## 测试
tests/test_worker_lease.py, tests/test_worker_runner.py: 租约协议(过期/迟到拒绝/双 Worker 竞争)、执行器单测(迷你快照→结果对象存在→业务结局正确)、隔离进程(超时→终止)。

## 注意
- 测试不依赖真实 Redis: 用假的 QueueClient(memory) 与 sqlite
- 只写 iesplan/worker/ 下文件与对应 tests/
- 中文注释; 完成后报告文件清单`,
  },
  {
    key: 'results',
    prompt: `你是 pIES 后端的证据与结果单元(U12/U13/U14)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/result.py (evidence_packages/result_assessments/result_index/result_selections/reports)
- /home/mc/Documents/工作文档/pIES/iesplan/metrics/validity.py (四维状态), iesplan/metrics/financial.py
- /home/mc/Documents/工作文档/pIES/iesplan/services/objects.py, iesplan/services/tasks.py (签名以实际为准)
- 设计输入 RPD 第10/11节(结果/证据与导出)/17.7/17.8(有效性/结果约束), 01-db-schema.md 第8节(结果)

## 任务: 实现 iesplan/services/results.py + iesplan/api/results.py (U14 结果、选择与报告 + U12 检查)
1. **证据服务**:
   - submit_evidence(db, task_id, attempt_id, token, payload) -> EvidencePackage: 校验当前尝试写入资格(租约+fencing), 保存不可变证据包(快照引用/算法/种子/停止条件/原始求解状态/候选索引/指标对象/逐时结果对象引用/清单+内容校验); 证据包不可变
   - get_evidence(package_id)
2. **评估服务** (U12):
   - run_assessment(db, evidence_package_id, assessment_type) -> ResultAssessment: 对证据执行物理/最优性/财务/可靠性四维检查(调用 metrics.validity), 每次检查创建新评估记录(不覆盖), 保存评估规则版本/时间/诊断/适用范围
   - 物理: 能量守恒残差、容量约束、边界条件; 最优性: 求解状态/Gap/停止原因; 财务: 现金流与 IRR 状态(unique/none/multiple/degenerate/out_of_domain/numerical_failure); 可靠性: 样本统计(未执行/部分/不足/有效)
   - 四维结论独立记录; 汇总只派生 可用/受限使用/不可用
3. **结果索引**: update_result_index(db, task_id, assessment_id, business_outcome): 只更新最新引用, 不覆盖历史评估
4. **结果选择** (U14):
   - select_result(db, user, task_id, solution_id, selection_type, reference_rule=None) -> ResultSelection: 保存所选解标识/用户/类型/参数差异补丁/确认预览内容校验
   - 结果 API 提供差异预览(参数差异补丁生成)
5. **结果 API** iesplan/api/results.py (prefix /api/projects/{id}/results):
   - GET /api/projects/{id}/tasks/{task_id}/result (结果视图: 四维结论/结局/指标摘要/逐时引用)
   - GET /api/projects/{id}/tasks/{task_id}/result/assessments (评估历史, 不可变)
   - POST /api/projects/{id}/tasks/{task_id}/result/assess (触发新评估)
   - POST /api/projects/{id}/tasks/{task_id}/result/select {solution_id, ...}
   - GET /api/projects/{id}/tasks/{task_id}/result/diff (选中结果的参数差异预览)
   - GET /api/projects/{id}/tasks/{task_id}/result/hourly?field=...&start=&end= (逐时结果查询, 从对象存储读, 分页)
   - 结果应用由项目单元处理(apply_result), 这里提供数据
6. **检查任务**: 提供 run_check_task(对已有证据包创建检查任务)

## 测试
tests/test_results_api.py: 证据提交(fencing 校验: 过期 token 拒绝)→评估四维→索引更新→评估历史追加→选择结果→差异预览→逐时分页查询。

## 注意
- 只写 iesplan/services/results.py, iesplan/api/results.py, tests/test_results_api.py
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
  {
    key: 'package',
    prompt: `你是 pIES 后端的项目包单元(U15)与审计单元(U16)实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md
- /home/mc/Documents/工作文档/pIES/iesplan/models/audit.py (audit_log/import_proposals/retention_rules 等), iesplan/models/project.py, iesplan/models/result.py
- /home/mc/Documents/工作文档/pIES/iesplan/services/objects.py, iesplan/services/project.py, iesplan/services/dataset.py (签名以实际为准)
- 设计输入 RPD 第6节(项目包)/13节(诊断审计)/17.8(导出)/23节(存储), 01-db-schema.md 第10节

## 任务: 实现 iesplan/services/package.py + iesplan/services/audit.py + iesplan/api/admin.py + iesplan/api/exports.py
1. **审计服务** (U16):
   - audit(db, actor_id, action, object_type, object_id, revision, result, checksum_info, extra) 统一入口(写 audit_log, 不可变)
   - 审计事件清单常量: 登录/登出/失败/改密/重置/停用/接管/权限变化/项目创建/复制/归档/删除/导入导出/草稿版本快照变化/任务提交取消终止/结果应用/维护操作
   - 只保存身份/时间/动作/对象标识/修订号/结果/必要校验信息, 不复制密码/令牌/完整模型/完整数据集/原始求解日志
2. **项目包服务** (U15):
   - export_package(db, user, project_id) -> PackageExport: 仅所有者; 版本化清单(格式版本/清单/对象清单), 流式导出: 模型/配置/版本/数据集版本与溯源/历史结果证据与评估引用/内容校验; 不含账号/权限/会话/全局配置/密钥
   - 实现为 zip 文件写入 data_dir/packages/{id}.zip + 对象存储记录 + 下载授权(短期单对象授权)
   - import_proposal(db, user, file_bytes, idempotency_key) -> ImportProposal: 校验格式/兼容性/清单/完整性(sha256 逐对象校验); 创建导入提案(暂存对象/拟创建项目快照/分区提交内容/校验报告)
   - confirm_import(db, user, proposal_id) -> Project: 提交导入: 创建新项目身份(不覆盖已有), 导入者成为所有者, 原授权关系不迁移, 历史结果作为证据来源保留(不伪造本地任务)
   - 导入约束: 不得静默覆盖; 每次导入新项目身份; 账号权限会话不随包导入
3. **Excel 报告** (U15/U14):
   - export_excel(db, user, project_id, evidence_package_id, assessment_id, lang='zh') -> bytes: 固定模板(标题中英双语, 默认中文), 内容: 项目版本/计算快照/数据版本/计算配置/算法/结果状态/四维结论/主要指标表/设备配置/财务摘要/环境摘要/工程摘要/适用范围与限制; 使用 openpyxl; 报告固定引用证据包与评估(不重新求解); 导出中注明适用单位与数据来源
   - 查看者可以导出 Excel; 只有所有者可导出项目包
4. **API**:
   - iesplan/api/exports.py (prefix /api/projects/{id}/exports): POST /exports/excel {evidence_package_id, assessment_id, lang} -> 下载授权 token; GET /exports/excel/download?token=; POST /exports/package (仅所有者) -> token; GET /exports/package/download?token=
   - iesplan/api/admin.py (prefix /api/admin): GET /admin/audit (审计查询, 过滤), GET /admin/diagnostics (运维诊断视图), POST /admin/unlock-task {task_id} (管理员解锁), POST /admin/transfer-project {project_id, target_user_id} (停用所有者转移), GET /admin/storage, GET /admin/health (存活/就绪/指标/队列/存储)
5. **下载授权**: 短期单对象授权(签名 token 含 object_id+过期, 过期 5 分钟)

## 测试
tests/test_package_api.py: 导出项目包(含清单/校验)→导入(新项目身份/所有者是导入者/历史结果证据保留)→导入校验失败拒绝→Excel 导出(标题中英/引用证据)→查看者可导 Excel 不可导包→短期授权过期。

## 注意
- 只写上面列出的 services/api/tests 文件
- 中文注释; 完成后报告文件清单与 API 列表`,
  },
]

phase('Wave1')
const w1 = await parallel(WAVE1.map((w) => () =>
  agent(w.prompt, { label: 'w1:' + w.key, phase: 'Wave1', effort: 'high' })
))
log('波次1完成: ' + w1.filter(Boolean).length + '/5')

phase('Wave2')
const w2 = await parallel(WAVE2.map((w) => () =>
  agent(w.prompt, { label: 'w2:' + w.key, phase: 'Wave2', effort: 'high' })
))
log('波次2完成: ' + w2.filter(Boolean).length + '/3')

phase('Wave3')
const w3 = await parallel(WAVE3.map((w) => () =>
  agent(w.prompt, { label: 'w3:' + w.key, phase: 'Wave3', effort: 'high' })
))
log('波次3完成: ' + w3.filter(Boolean).length + '/3')

phase('Integrate')
const all = w1.concat(w2, w3).filter(Boolean)
const integration = await agent(
  `你是 pIES 后端的全量集成验证者。工作目录 /home/mc/Documents/工作文档/pIES。

11 个业务单元 agent 已分 3 波次实现: auth, project, model, dataset, config (波次1); validation, tasks, objects (波次2); worker, results, package/audit/excel (波次3)。另有基础层(config/db/models/core/engines/metrics/main)。

## 步骤
1. 浏览 backend/iesplan 全部结构, 检查:
   - 所有 api 路由已注册到 main.py 的 create_app()(include_router)
   - services 间接口对齐(签名不匹配处直接修复)
   - 检查是否有 import 错误、循环依赖
2. 运行测试并修复到全绿:
   - docker compose build backend
   - docker compose run --rm backend pytest -q 2>&1 | tail -40
   - 逐个修复失败的测试与代码, 重跑到全部通过
   - docker compose run --rm backend ruff check iesplan tests (修复错误)
3. 补一个全链路集成测试 tests/test_integration.py:
   - 注册/登录管理员与工程师
   - 工程师创建项目
   - 添加设备(电网/光伏/热泵/锅炉/制冷机/电池/负荷)与连接
   - 生成样例数据集(内置样例)
   - 保存配置 + 财务基准确认
   - 提交方案评价任务(注入假执行或真实 evaluate_plan 小算例)
   - 任务状态推进到完成; 结果四维评估存在; 选择结果; Excel 导出存在
   - 项目包导出/导入
   - 全程断言关键业务语义(RPD 约束抽查)
4. 输出最终报告: 路由清单, 测试总数/通过数, 遗留问题列表

## 注意
- 修改代码只限 backend/ 下
- 全部测试在 docker 内运行
- 集成测试如依赖 Redis/Postgres 不可用, 用 sqlite + 内存队列的降级路径(与基础层一致)
- 中文输出最终报告`,
  { label: 'integrate:units', phase: 'Integrate', effort: 'high' }
)

log('业务单元全部完成')
return { waves: [w1.length, w2.length, w3.length], integrated: !!integration }
