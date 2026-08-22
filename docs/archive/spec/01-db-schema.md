# pIES 数据库权威事实关系模式(PostgreSQL)

> 版本:0.1(初始设计)
> 日期:2026-08-18
> 范围:PostgreSQL 权威事实来源(system of record)的完整关系模式

本文档定义 PostgreSQL 中保存权威事实的完整关系模式。核心理念:

- 浏览器、Redis、日志不是权威;PG 保存项目、权限、任务事实、版本、证据与业务索引。
- 工作草稿可改;项目版本与计算快照不可变(追加式版本化)。
- 大型时序/结果对象放入内容寻址对象存储(磁盘),数据库只存引用 + 内容校验值(sha256)。
- 计算快照是任务的唯一输入,绑定项目版本、数据集版本、程序/扩展版本、随机种子、容差、内容校验。
- 任务事实(任务/尝试/租约/fencing token)存 PG;队列、进度、心跳存 Redis(可重建)。
- 每个权威实体只有一个写入单元(U01–U16 业务单元)。

## 0. 通用约定与全局约束

| 主题 | 约定 |
|---|---|
| 命名 | 表名复数、下划线小写(如 `project_versions`);列名下划线小写;标识符长度 ≤ 63 |
| 主键 | 一律 `BIGINT GENERATED ALWAYS AS IDENTITY`(文中写作 `BIGINT PK IDENTITY`);对外引用使用业务唯一键(如 `version_no`、`idempotency_key`) |
| 时间 | 一律 `TIMESTAMPTZ`,统一 UTC 存储;展示/计算时按项目或用户的固定 UTC 偏移转换;偏移列命名 `fixed_utc_offset_minutes` |
| 金额 | `NUMERIC(18,4)`;币种 `TEXT`,`CHECK (col IN ('CNY','USD'))` |
| 内容寻址 | 大对象(时序、结果、文件)只存内容寻址对象存储,库中保存 `object_id` 引用 + 内容校验值;哈希列统一 `TEXT` 且 `CHECK (col ~ '^[0-9a-f]{64}$')` |
| 不可变表 | 追加式版本化:禁止 UPDATE/DELETE。三道防线:(1) 应用层只允许该实体唯一写入单元(U01–U16)发 INSERT;(2) `REVOKE UPDATE, DELETE ON <table> FROM PUBLIC`,且不授予任何角色该表的 UPDATE/DELETE;(3) 建触发器 `tg_<table>_no_update` / `tg_<table>_no_delete`,在 `BEFORE UPDATE OR DELETE` 时 `RAISE EXCEPTION`。允许局部更新的表在定义中注明"仅可更新列",并配列级触发器 |
| 写入单元 | 每个权威实体只有一个写入单元(U01 身份、U02 权限、U03 项目、U04 模型、U05 数据集、U06 配置、U07 任务、U08 快照、U09 结果、U10 不确定性、U11 对象、U12 审计、U13 报告、U14 导入、U15 保留策略、U16 管理维护) |
| 部分唯一索引 | 用于表达"同一时刻至多一条"等行间约束,见各表 `uq_*` 定义(SQL 形式) |
| 软删除 | 用户、设备、数据集等允许软删:状态列置终态,不物理删除;不可变表不提供任何删除路径 |

每张表的定义包含:列(名称/类型/约束/说明)、主键、外键、唯一约束、推荐索引、可变性规则。

---

## 1. 身份(U01 身份写入单元)

### 1.1 `users` 用户账号

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| username | TEXT NOT NULL | 登录名,强制小写;`CHECK (username = lower(username))`、`CHECK (username ~ '^[a-z0-9_]{3,32}$')` |
| display_name | TEXT NOT NULL | 显示名 |
| email | TEXT | 邮箱;`CHECK (email IS NULL OR email ~ '^[^@\s]+@[^@\s]+$')` |
| status | TEXT NOT NULL DEFAULT 'active' | `CHECK (status IN ('active','disabled','locked'))`;生命周期状态:active 正常 / disabled 停用 / locked 锁定 |
| locale | TEXT NOT NULL DEFAULT 'zh-CN' | 语言偏好 |
| timezone | TEXT NOT NULL DEFAULT 'Asia/Shanghai' | IANA 时区 |
| fixed_utc_offset_minutes | INT NOT NULL DEFAULT 480 | `CHECK (fixed_utc_offset_minutes BETWEEN -720 AND 840)`;用户偏好 UTC 偏移(分钟) |
| credential_version | INT NOT NULL DEFAULT 0 | 凭证版本;每次凭证变更(改密/重置)递增 1,使旧会话失效 |
| is_system | BOOLEAN NOT NULL DEFAULT false | 系统服务账号标记,禁止交互登录 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |
| last_login_at | TIMESTAMPTZ | 最后登录时间 |

- 主键:`id`
- 唯一约束:`username`、`email`
- 推荐索引:`idx_users_status ON users (status)`
- 外键:无(被 `user_roles`、`credentials` 等引用)
- 可变性:可 UPDATE(状态、偏好、credential_version 由 U01 写入单元维护);`id`、`created_at` 不可改;删除一律软删(状态置 `disabled`),不物理删除

### 1.2 `roles` 全局角色

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| code | TEXT NOT NULL | `CHECK (code ~ '^[a-z_]{1,32}$')`;如 `admin`、`operator`、`viewer` |
| name | TEXT NOT NULL | 角色显示名 |
| description | TEXT | 说明 |
| is_system | BOOLEAN NOT NULL DEFAULT false | 系统内置角色,禁止删除/改 code |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`;唯一约束:`code`
- 可变性:仅 name/description 可 UPDATE;`is_system = true` 的行禁止 UPDATE/DELETE(触发器)

### 1.3 `user_roles` 用户-角色授权(追加式历史)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| user_id | BIGINT NOT NULL | 外键 → `users(id)` |
| role_id | BIGINT NOT NULL | 外键 → `roles(id)` |
| granted_by | BIGINT NOT NULL | 授权人 → `users(id)` |
| granted_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 授权时间 |
| revoked_at | TIMESTAMPTZ | 撤销时间;NULL 表示当前有效 |
| revoked_by | BIGINT | 撤销人 → `users(id)` |

- 主键:`id`
- 唯一约束:`UNIQUE (user_id, role_id, granted_at)`(防同一时刻重复授权)
- 部分唯一索引(SQL):每用户每角色至多一条有效授权
  ```sql
  CREATE UNIQUE INDEX uq_user_roles_current ON user_roles (user_id, role_id) WHERE revoked_at IS NULL;
  ```
- 推荐索引:`idx_user_roles_role ON user_roles (role_id)`;`idx_user_roles_user ON user_roles (user_id)`
- 可变性:追加式历史;撤销=置 `revoked_at`/`revoked_by`,禁止物理删除,禁止改 `granted_at`

### 1.4 `credentials` 凭证(哈希、强度、首次改密)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| user_id | BIGINT NOT NULL | 外键 → `users(id)` |
| credential_type | TEXT NOT NULL | `CHECK (credential_type IN ('password','totp','webauthn','recovery_code'))` |
| secret_hash | TEXT NOT NULL | 密码用 Argon2id 哈希;TOTP 为加密后的 secret;绝不存明文 |
| algorithm | TEXT NOT NULL | 哈希算法标识(如 `argon2id`),随算法演进可迁移 |
| cost_params | JSONB NOT NULL DEFAULT '{}' | 强度参数(内存/迭代/并行度) |
| strength_score | SMALLINT NOT NULL | `CHECK (strength_score BETWEEN 0 AND 100)`;口令强度评分 |
| requires_change | BOOLEAN NOT NULL DEFAULT true | 首次登录/重置后必须改密 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| rotated_at | TIMESTAMPTZ | 轮换时间 |
| expires_at | TIMESTAMPTZ | 过期时间(可选) |
| revoked_at | TIMESTAMPTZ | 撤销时间;NULL 表示有效 |
| created_by | BIGINT | 创建来源 → `users(id)`;管理员重置时记录操作者,自注册/自助改密为 NULL |

- 主键:`id`
- 部分唯一索引(SQL):每用户至多一条有效 password 凭证
  ```sql
  CREATE UNIQUE INDEX uq_credentials_active_password ON credentials (user_id)
      WHERE credential_type = 'password' AND revoked_at IS NULL;
  ```
- 推荐索引:`idx_credentials_user ON credentials (user_id, revoked_at)`
- 可变性:不可变,只 INSERT;凭证变更 = 撤销旧行 + 插入新行,并同步递增 `users.credential_version`

### 1.5 `window_sessions` 浏览器会话

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键(会话标识) |
| session_token_hash | TEXT NOT NULL | 会话令牌哈希(sha256),库中不存令牌原文 |
| user_id | BIGINT NOT NULL | 外键 → `users(id)` |
| credential_version_at_issue | INT NOT NULL | 签发时的 `users.credential_version`;校验时若用户当前凭证版本更高则强制接管/失效 |
| status | TEXT NOT NULL DEFAULT 'active' | `CHECK (status IN ('active','takeover_pending','revoked','expired'))` |
| replaced_by_session_id | BIGINT | 替代会话标识,外键 → `window_sessions(id)`(自引用);接管流程:新会话 active → 旧会话 takeover_pending → 用户确认后旧会话 revoked、新会话保留;`CHECK (replaced_by_session_id IS NULL OR replaced_by_session_id <> id)` |
| ip | INET | 登录 IP |
| user_agent | TEXT | 浏览器标识 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| last_seen_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 最后活跃时间 |
| expires_at | TIMESTAMPTZ NOT NULL | 过期时间 |
| revoked_at | TIMESTAMPTZ | 实际撤销时间 |
| revoked_by | BIGINT | 撤销者 → `users(id)`;系统自动过期时为 NULL |

- 主键:`id`
- 唯一约束:`session_token_hash`
- 关键部分唯一索引(SQL):每账号至多一个 active 会话(单点登录)
  ```sql
  CREATE UNIQUE INDEX uq_window_sessions_one_active ON window_sessions (user_id) WHERE status = 'active';
  ```
  每账号至多一个待接管会话
  ```sql
  CREATE UNIQUE INDEX uq_window_sessions_one_pending ON window_sessions (user_id) WHERE status = 'takeover_pending';
  ```
- 推荐索引:`idx_window_sessions_user ON window_sessions (user_id, status)`
- 可变性:仅 `status`、`replaced_by_session_id`、`last_seen_at`、`revoked_at`、`revoked_by` 可 UPDATE(列级触发器限制);`id`、`session_token_hash`、`credential_version_at_issue` 不可改;行保留至审计期结束,不物理删除

### 1.6 `auth_events` 身份认证审计(不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| user_id | BIGINT | 外键 → `users(id)`;登录失败等场景可为 NULL(未识别用户) |
| session_id | BIGINT | 外键 → `window_sessions(id)`,可为 NULL |
| event_type | TEXT NOT NULL | `CHECK (event_type IN ('login_success','login_failure','logout','password_change','credential_reset','account_disabled','session_takeover','permission_change','role_change','session_revoke','maintenance'))` |
| occurred_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 发生时间 |
| ip | INET | 来源 IP |
| user_agent | TEXT | 浏览器标识 |
| detail | JSONB | 事件详情(如失败原因、变更前后权限快照) |

- 主键:`id`
- 推荐索引:`idx_auth_events_user ON auth_events (user_id, occurred_at DESC)`;`idx_auth_events_type ON auth_events (event_type, occurred_at DESC)`;`idx_auth_events_time ON auth_events (occurred_at)`
- 可变性:不可变,仅 INSERT;无任何 UPDATE/DELETE 授权与触发器兜底;按保留策略归档(见 `retention_rules`)

---

## 2. 权限(U02 权限写入单元)

### 2.1 `project_members` 项目成员(owner/viewer)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| user_id | BIGINT NOT NULL | 外键 → `users(id)` |
| role | TEXT NOT NULL | `CHECK (role IN ('owner','viewer'))`;owner 可管理成员与版本,viewer 只读 |
| auth_version | INT NOT NULL | 授权版本;角色变更、转移、成员增减时 `projects` 级授权版本递增,用于客户端缓存失效 |
| granted_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 授权时间 |
| granted_by | BIGINT NOT NULL | 授权人 → `users(id)` |
| revoked_at | TIMESTAMPTZ | 撤销时间;NULL 表示当前有效 |
| revoked_by | BIGINT | 撤销人 → `users(id)` |
| expires_at | TIMESTAMPTZ | 可选过期时间(临时授权);到期由后台任务置 `revoked_at` |

- 主键:`id`
- 唯一约束:`UNIQUE (project_id, user_id, granted_at)`(防同一时刻重复授权)
- 部分唯一索引(SQL):每项目每用户至多一条当前有效成员
  ```sql
  CREATE UNIQUE INDEX uq_project_members_current ON project_members (project_id, user_id) WHERE revoked_at IS NULL;
  ```
- 推荐索引:`idx_project_members_user ON project_members (user_id)`;`idx_project_members_project ON project_members (project_id, role)`
- 关键业务约束:每项目至少一个有效 owner。普通 CHECK 无法跨行表达,由写入单元 U02 事务保证:任何使项目 owner 数降为 0 的更新在事务内先校验 `SELECT count(*) ... WHERE revoked_at IS NULL AND role='owner'`,不足则回滚
- 可变性:追加式;授权=插入行,撤销=置 `revoked_at`;角色变更=撤销旧行+插入新行(auth_version+1)

### 2.2 `ownership_transfers` 项目所有权转移审计

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| from_user_id | BIGINT NOT NULL | 转出方(当前 owner)→ `users(id)` |
| to_user_id | BIGINT NOT NULL | 转入方 → `users(id)` |
| status | TEXT NOT NULL | `CHECK (status IN ('proposed','accepted','completed','cancelled','rejected'))`;proposed 待对方接受 → accepted 已接受 → completed 完成;任一步可 cancelled/rejected |
| transfer_version | INT NOT NULL | 目标授权版本(完成时写入成员 auth_version) |
| proposed_by | BIGINT NOT NULL | 发起人 → `users(id)` |
| proposed_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 发起时间 |
| decided_by | BIGINT | 决策人(接受/拒绝方)→ `users(id)` |
| decided_at | TIMESTAMPTZ | 决策时间 |
| completed_at | TIMESTAMPTZ | 完成时间 |

- 主键:`id`
- 部分唯一索引(SQL):每项目至多一个未决转移
  ```sql
  CREATE UNIQUE INDEX uq_ownership_transfers_open ON ownership_transfers (project_id)
      WHERE status IN ('proposed','accepted');
  ```
- 行约束:`CHECK (from_user_id <> to_user_id)`
- 推荐索引:`idx_ownership_transfers_to ON ownership_transfers (to_user_id, status)`
- 可变性:追加式;仅 status/decided_*/completed_at 可 UPDATE(列级触发器限制);历史行永久保留

### 2.3 `admin_maintenance_actions` 管理员维护操作审计(不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| action_type | TEXT NOT NULL | `CHECK (action_type IN ('backup','restore','purge','reindex','config_change','object_quota_change','retention_change','user_override'))` |
| performed_by | BIGINT NOT NULL | 操作者 → `users(id)`;须为管理员角色 |
| status | TEXT NOT NULL | `CHECK (status IN ('pending','running','succeeded','failed'))` |
| started_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 开始时间 |
| finished_at | TIMESTAMPTZ | 结束时间 |
| params | JSONB | 操作参数快照(变更前值) |
| result | JSONB | 结果摘要(变更后值、影响行数) |

- 主键:`id`
- 推荐索引:`idx_admin_actions_time ON admin_maintenance_actions (started_at DESC)`;`idx_admin_actions_by ON admin_maintenance_actions (performed_by)`
- 可变性:不可变;一次操作一行记录;失败重试=新行;`status` 亦不允许 UPDATE(操作结束以新行归档)

---

## 3. 项目(U03 项目写入单元)

### 3.1 `projects` 项目主表(生命周期状态)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| name | TEXT NOT NULL | 项目名称 |
| description | TEXT | 项目说明 |
| status | TEXT NOT NULL DEFAULT 'active' | `CHECK (status IN ('active','archived','deleted'))`;生命周期:active 进行中 / archived 归档只读 / deleted 软删 |
| owner_id | BIGINT NOT NULL | 冗余镜像当前 owner → `users(id)`;权威在 `project_members`,此处仅供快速查询,由 U02 转移流程同步 |
| currency | TEXT NOT NULL DEFAULT 'CNY' | `CHECK (currency IN ('CNY','USD'))`;财务计算币种 |
| fixed_utc_offset_minutes | INT NOT NULL DEFAULT 480 | `CHECK (fixed_utc_offset_minutes BETWEEN -720 AND 840)`;项目固定 UTC 偏移(分钟),所有时序数据按此偏移解释 |
| schema_version | INT NOT NULL DEFAULT 1 | 项目数据结构模式版本,未来演进时用于迁移判断 |
| current_draft_id | BIGINT | 当前草稿指针 → `drafts(id)`;为 NULL 表示无草稿(循环依赖,建表后 ALTER 添加,见迁移顺序) |
| current_version_id | BIGINT | 当前发布版本指针 → `project_versions(id)`;为 NULL 表示尚无版本 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |

- 主键:`id`
- 唯一约束:`name`(全局唯一,可接受;若需同名多项目,改 `UNIQUE (name, owner_id)`,本设计取全局唯一)
- 推荐索引:`idx_projects_status ON projects (status)`;`idx_projects_owner ON projects (owner_id)`
- 可变性:可 UPDATE(name/description/status/current_draft_id/current_version_id/updated_at);`id`、`created_at`、`fixed_utc_offset_minutes`(一经创建即固定,偏移变更走新版本)、`currency`(同上)不可改;删除一律软删

### 3.2 `drafts` 工作草稿(综合修订号,可改)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| revision | INT NOT NULL | 综合修订号;项目内单调递增(由 U03 写入单元按 `max(revision)+1` 生成) |
| content_hash | TEXT NOT NULL | 草稿内容校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')` |
| parent_draft_id | BIGINT | 父草稿 → `drafts(id)`(自引用);分叉/回退场景使用;须属同一项目(应用层校验) |
| is_current | BOOLEAN NOT NULL DEFAULT false | 是否为项目当前草稿;仅一行可为 true |
| updated_by | BIGINT NOT NULL | 最后修改者 → `users(id)` |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 最后修改时间 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (project_id, revision)`
- 部分唯一索引(SQL):每项目至多一个当前草稿
  ```sql
  CREATE UNIQUE INDEX uq_drafts_current ON drafts (project_id) WHERE is_current;
  ```
- 推荐索引:`idx_drafts_project ON drafts (project_id, revision DESC)`
- 可变性:草稿可改——`content_hash`/`updated_by`/`updated_at`/`is_current` 可 UPDATE;`project_id`/`revision`/`created_at` 不可改;`is_current` 的置位由 U03 写入单元在同一事务内先清旧行
- 与项目指针一致性:`projects.current_draft_id` 必须指向该项目 `is_current = true` 的行(U03 事务保证)

### 3.3 `project_versions` 项目版本(不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| version_no | INT NOT NULL | 版本号,项目内单调递增 |
| name | TEXT NOT NULL | 版本名称 |
| description | TEXT | 版本说明 |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| parent_version_id | BIGINT | 父版本 → `project_versions(id)`(自引用);一般指前一版本;须属同一项目(应用层校验) |
| source_draft_id | BIGINT | 来源草稿 → `drafts(id)`;发布时来自哪个草稿修订,可为 NULL(导入等场景) |
| source_draft_revision | INT | 来源草稿修订号(冗余,防草稿后续变更影响追溯) |
| reason | TEXT NOT NULL | 创建原因(发布/回退/导入/修正) |
| fixed_utc_offset_minutes | INT NOT NULL | `CHECK (fixed_utc_offset_minutes BETWEEN -720 AND 840)`;本版本固定 UTC 偏移(拷贝自项目,版本独立固化) |
| currency | TEXT NOT NULL | `CHECK (currency IN ('CNY','USD'))`;本版本固定币种 |
| schema_version | INT NOT NULL | 本版本内容模式版本 |
| content_hash | TEXT NOT NULL | 版本全部内容的校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')`;任何输入变更都会导致发布时哈希不同 |

- 主键:`id`
- 唯一约束:`UNIQUE (project_id, version_no)`
- 推荐索引:`idx_project_versions_parent ON project_versions (parent_version_id)`;`idx_project_versions_project ON project_versions (project_id, version_no DESC)`
- 可变性:不可变(追加式)。禁止 UPDATE/DELETE:`REVOKE UPDATE, DELETE ON project_versions FROM PUBLIC`,并配触发器兜底:
  ```sql
  CREATE FUNCTION tg_project_versions_immutable() RETURNS trigger AS $$
  BEGIN RAISE EXCEPTION 'project_versions 为不可变表,禁止 %', TG_OP; END $$ LANGUAGE plpgsql;
  CREATE TRIGGER tg_project_versions_no_update BEFORE UPDATE ON project_versions
      FOR EACH ROW EXECUTE FUNCTION tg_project_versions_immutable();
  CREATE TRIGGER tg_project_versions_no_delete BEFORE DELETE ON project_versions
      FOR EACH ROW EXECUTE FUNCTION tg_project_versions_immutable();
  ```
- 无例外列;更正=发布新版本(parent_version_id 指向旧版本)

### 3.4 `version_refs` 版本引用清单(不可变)

版本引用的不可变对象清单:版本内引用的数据集版本、系统图、计算快照、证据包、报告与内容寻址对象,发布时一次性写入,保证版本自包含。

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_version_id | BIGINT NOT NULL | 外键 → `project_versions(id)` |
| ref_type | TEXT NOT NULL | `CHECK (ref_type IN ('dataset_version','system_graph','calc_config','calc_snapshot','evidence_package','report','object'))` |
| object_id | BIGINT NOT NULL | 引用的内容寻址对象 → `objects(id)`;对应对象的 sha256 即 ref_hash 的权威来源 |
| ref_key | TEXT | 业务键(如 `dataset_version_id=12` 的字符串形式),便于直接查询 |
| ref_hash | TEXT | 引用对象的 sha256;`CHECK (ref_hash ~ '^[0-9a-f]{64}$')` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 写入时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (project_version_id, ref_type, object_id)`
- 推荐索引:`idx_version_refs_object ON version_refs (object_id)`(对象引用计数维护用)
- 可变性:不可变,仅 INSERT;禁止 UPDATE/DELETE(触发器与权限同 3.3 模式)

---

## 4. 系统模型(U04 模型写入单元)

### 4.1 `system_graphs` 系统图(版本化)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| draft_id | BIGINT | 外键 → `drafts(id)`;非空表示"工作图",挂草稿可改 |
| project_version_id | BIGINT | 外键 → `project_versions(id)`;非空表示"版本图",发布固化不可变 |
| name | TEXT NOT NULL | 图名称 |
| graph_hash | TEXT NOT NULL | 拓扑校验值(节点/边/参数序列化 sha256);`CHECK (graph_hash ~ '^[0-9a-f]{64}$')` |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 互斥约束(关键):一张图要么是工作图要么是版本图
  ```sql
  CHECK ((draft_id IS NULL) <> (project_version_id IS NULL))
  ```
- 推荐索引:`idx_system_graphs_draft ON system_graphs (draft_id)`;`idx_system_graphs_version ON system_graphs (project_version_id)`
- 可变性:工作图可改(name/graph_hash);版本图不可变——触发器按行判定:
  ```sql
  CREATE FUNCTION tg_system_graphs_version_frozen() RETURNS trigger AS $$
  BEGIN
    IF OLD.project_version_id IS NOT NULL THEN
      RAISE EXCEPTION '版本图不可修改';
    END IF;
    RETURN NEW;
  END $$ LANGUAGE plpgsql;
  CREATE TRIGGER tg_system_graphs_frozen BEFORE UPDATE ON system_graphs
      FOR EACH ROW EXECUTE FUNCTION tg_system_graphs_version_frozen();
  ```
- 版本化流程:发布时由 U04 将当前工作图深拷贝为版本图(graph_hash 重算)并写入 `version_refs`

### 4.2 `devices` 设备(类型、存量/新增、参数、模型精度)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| graph_id | BIGINT NOT NULL | 所属系统图 → `system_graphs(id)` |
| device_type | TEXT NOT NULL | `CHECK (device_type IN ('generator','boiler','chiller','pv','wind','storage','load','source','sink','converter','network','other'))`;细分类别放 `params.type_detail` |
| kind | TEXT NOT NULL | `CHECK (kind IN ('existing','new'))`;存量设备(existing)/新增设备(new),规划选型的核心区分 |
| name | TEXT NOT NULL | 设备名称 |
| description | TEXT | 说明 |
| params | JSONB NOT NULL DEFAULT '{}' | 参数(容量、效率曲线、投资/运维成本等),键与模式版本由 `schema_version` 约定 |
| model_fidelity | TEXT NOT NULL DEFAULT 'medium' | `CHECK (model_fidelity IN ('low','medium','high'))`;模型精度:low 线性静态 / medium 分段线性 / high 非线性动态 |
| status | TEXT NOT NULL DEFAULT 'active' | `CHECK (status IN ('active','retired'))`;软删 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (graph_id, name)`
- 推荐索引:`idx_devices_graph ON devices (graph_id, device_type)`;`idx_devices_kind ON devices (graph_id, kind)`;JSONB 参数索引按需(如 `params -> 'capacity'` 参与筛选时建 `GIN (params)`)
- 可变性:随所属图——工作图内可 UPDATE(参数/精度/状态),版本图内禁止(继承 `system_graphs` 的冻结触发器,经 `graph_id` 判断)

### 4.3 `ports` 端口

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| device_id | BIGINT NOT NULL | 所属设备 → `devices(id)` |
| port_type | TEXT NOT NULL | `CHECK (port_type IN ('electric','thermal','cooling','fuel','water','data'))`;电/热/冷/燃料/水/数据 |
| direction | TEXT NOT NULL | `CHECK (direction IN ('in','out','bidirectional'))` |
| name | TEXT NOT NULL | 端口名 |
| capacity | NUMERIC(18,4) | 容量(单位由设备类型约定:kW/MW/GJ/h 等) |
| params | JSONB NOT NULL DEFAULT '{}' | 扩展参数(电压等级、温度等) |

- 主键:`id`
- 唯一约束:`UNIQUE (device_id, name)`
- 推荐索引:`idx_ports_device ON ports (device_id)`;`idx_ports_type ON ports (port_type)`
- 可变性:同 4.2(随所属图冻结规则)

### 4.4 `connections` 连接

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| graph_id | BIGINT NOT NULL | 所属系统图 → `system_graphs(id)`(冗余,便于图级查询与校验) |
| from_port_id | BIGINT NOT NULL | 起点端口 → `ports(id)` |
| to_port_id | BIGINT NOT NULL | 终点端口 → `ports(id)` |
| conn_type | TEXT NOT NULL | `CHECK (conn_type IN ('electric_line','thermal_pipe','cooling_pipe','fuel_pipe','data_link'))` |
| capacity | NUMERIC(18,4) | 输送容量上限 |
| loss_rate | NUMERIC(6,4) NOT NULL DEFAULT 0 | `CHECK (loss_rate BETWEEN 0 AND 1)`;损耗率 |
| params | JSONB NOT NULL DEFAULT '{}' | 扩展参数(长度、电阻、管道直径等) |

- 主键:`id`
- 行约束:`CHECK (from_port_id <> to_port_id)`(禁止自环)
- 唯一约束:`UNIQUE (graph_id, from_port_id, to_port_id, conn_type)`(同图同类型同两端只允许一条)
- 推荐索引:`idx_connections_from ON connections (from_port_id)`;`idx_connections_to ON connections (to_port_id)`;`idx_connections_graph ON connections (graph_id)`
- 端口归属校验:两端端口须属本图设备(应用层校验,写入单元 U04 保证)
- 可变性:同 4.2

---

## 5. 数据集(U05 数据集写入单元)

### 5.1 `datasets` 数据集元数据

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT | 外键 → `projects(id)`;NULL 表示共享/全局数据集 |
| name | TEXT NOT NULL | 数据集名称 |
| description | TEXT | 说明 |
| status | TEXT NOT NULL DEFAULT 'draft' | `CHECK (status IN ('draft','published','deprecated'))`;deprecated 后禁止新建版本 |
| default_license | TEXT | 默认许可证 |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (project_id, name)`(NULL 处理:共享数据集按 `(name)` 全局去重,用 `COALESCE(project_id,0)` 部分表达式唯一索引,或应用层校验;本设计用表达式索引)
  ```sql
  CREATE UNIQUE INDEX uq_datasets_name ON datasets (COALESCE(project_id, 0), name);
  ```
- 推荐索引:`idx_datasets_project ON datasets (project_id)`
- 可变性:可 UPDATE(元数据/状态);删除一律软删(状态置 deprecated)

### 5.2 `dataset_versions` 数据集版本(不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| dataset_id | BIGINT NOT NULL | 外键 → `datasets(id)` |
| version_no | INT NOT NULL | 版本号,数据集内单调递增 |
| timeline | TEXT NOT NULL | `CHECK (timeline IN ('hourly','quarter_hourly','daily','monthly','yearly','custom'))`;时间轴粒度 |
| resolution | TEXT NOT NULL | 分辨率说明(如 '1h'、'15min') |
| fixed_utc_offset_minutes | INT NOT NULL | `CHECK (fixed_utc_offset_minutes BETWEEN -720 AND 840)`;数据时间戳的固定 UTC 偏移 |
| fields | JSONB NOT NULL | 字段定义(名称/类型/单位/含义) |
| units | JSONB NOT NULL | 字段单位表(与 fields 一一对应) |
| quality_report | JSONB | 质量报告(缺失率、异常率、插值说明) |
| provenance | JSONB | 溯源(来源站点/传感器/模型/清洗脚本及版本) |
| license | TEXT | 本版本许可证 |
| content_hash | TEXT NOT NULL | 数据内容校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')`;由全部数据文件内容计算 |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| created_reason | TEXT | 创建原因(导入/修订/补数) |

- 主键:`id`
- 唯一约束:`UNIQUE (dataset_id, version_no)`
- 推荐索引:`idx_dataset_versions_dataset ON dataset_versions (dataset_id, version_no DESC)`
- 可变性:不可变,仅 INSERT;禁止 UPDATE/DELETE(触发器与权限同 3.3 模式);更正=发布新版本

### 5.3 `dataset_files` 数据集版本文件(指向内容寻址对象)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| dataset_version_id | BIGINT NOT NULL | 外键 → `dataset_versions(id)` |
| object_id | BIGINT NOT NULL | 指向内容寻址对象 → `objects(id)`;数据本体(parquet/csv 等)不入库 |
| file_kind | TEXT NOT NULL | `CHECK (file_kind IN ('data','header','manifest','metadata'))` |
| format | TEXT NOT NULL | `CHECK (format IN ('parquet','csv','json'))` |
| row_count | BIGINT NOT NULL DEFAULT 0 | `CHECK (row_count >= 0)`;记录行数 |
| size_bytes | BIGINT NOT NULL DEFAULT 0 | `CHECK (size_bytes >= 0)`;字节数 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (dataset_version_id, object_id)`
- 推荐索引:`idx_dataset_files_object ON dataset_files (object_id)`(对象引用计数维护)
- 一致性要求:`size_bytes` 与 `objects.size_bytes` 一致,`objects.sha256` 与 `dataset_versions.content_hash` 的计算链一致(U05 写入单元校验)
- 可变性:不可变,仅 INSERT

---

## 6. 计算配置(U06 配置写入单元)

### 6.1 `calc_configs` 计算配置(参数当前值、变量、目标、约束、算法、容差、种子)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| name | TEXT NOT NULL | 配置名 |
| description | TEXT | 说明 |
| params | JSONB NOT NULL DEFAULT '{}' | 参数当前值(电价、燃料价、贴现率等) |
| variables | JSONB NOT NULL DEFAULT '[]' | 优化变量声明(名称/上下界/类型 continuous|binary|integer) |
| objectives | JSONB NOT NULL DEFAULT '[]' | 目标函数声明(最小成本/最大收益/多目标权重) |
| constraints | JSONB NOT NULL DEFAULT '[]' | 约束声明(供需平衡、容量、爬坡、碳排放等) |
| min_irr | NUMERIC(6,4) | `CHECK (min_irr IS NULL OR min_irr BETWEEN 0 AND 1)`;最低内部收益率(0–100%) |
| algorithm | TEXT NOT NULL | `CHECK (algorithm IN ('milp','lp','heuristic','ga','exhaustive','custom'))`;算法选择 |
| solver | TEXT | 求解器标识(如 `cbc`、`gurobi`、`highs`) |
| tolerances | JSONB NOT NULL DEFAULT '{}' | 容差(最优性间隙 MIPGap、可行性容差等) |
| random_seed | BIGINT | 随机种子;非 NULL 时结果可复现 |
| status | TEXT NOT NULL DEFAULT 'draft' | `CHECK (status IN ('draft','frozen'))`;frozen 表示已冻结 |
| version | INT NOT NULL DEFAULT 1 | 配置版本;冻结后变更=版本+1 新行 |
| updated_by | BIGINT NOT NULL | 最后修改者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (project_id, name)`(当前行);历史版本行另存于本表新行,`name` 冲突时用 `UNIQUE (project_id, name, version)` 替代——本设计采用后者:
  ```sql
  CREATE UNIQUE INDEX uq_calc_configs_name_version ON calc_configs (project_id, name, version);
  ```
- 推荐索引:`idx_calc_configs_project ON calc_configs (project_id, name)`
- 关键约束(触发器):`status = 'frozen'` 的行禁止 UPDATE/DELETE;draft 行可自由编辑:
  ```sql
  CREATE FUNCTION tg_calc_configs_frozen() RETURNS trigger AS $$
  BEGIN
    IF OLD.status = 'frozen' THEN
      RAISE EXCEPTION '冻结的计算配置不可修改';
    END IF;
    RETURN NEW;
  END $$ LANGUAGE plpgsql;
  CREATE TRIGGER tg_calc_configs_no_update BEFORE UPDATE ON calc_configs
      FOR EACH ROW EXECUTE FUNCTION tg_calc_configs_frozen();
  ```
- 快照固化:任务运行时由 U08 将配置全文拷贝进 `calc_snapshots.calc_config_snapshot`,此后配置修改不影响已提交任务(任务不可变输入)

---

## 7. 快照与任务(U07 任务写入单元 / U08 快照写入单元)

### 7.1 `calc_snapshots` 计算快照(不可变)

计算快照是任务的唯一输入,固定绑定:项目版本、数据集版本、程序/扩展版本、随机种子、容差、内容校验。

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_version_id | BIGINT NOT NULL | 绑定项目版本 → `project_versions(id)` |
| dataset_version_ids | BIGINT[] NOT NULL DEFAULT '{}' | 绑定的数据集版本 id 数组(→ `dataset_versions(id)`);内容以 `version_refs` 为权威,数组为执行时快捷引用,元素个数/内容由 U08 校验 |
| calc_config_snapshot | JSONB NOT NULL | 计算配置全文拷贝(配置+变量+目标+约束+容差+种子),保证输入固定,不受 `calc_configs` 后续修改影响 |
| program_version | TEXT NOT NULL | 计算程序版本(如 `ies-core@2.3.1`;`id@version` 形式对齐 04 注册表快照 §7.3) |
| extension_versions | JSONB NOT NULL DEFAULT '{}' | 扩展/插件版本表(名称→版本) |
| random_seed | BIGINT NOT NULL | 随机种子(由 `calc_configs.random_seed` 固化,快照强制非 NULL) |
| tolerances | JSONB NOT NULL | 容差快照(最优性间隙、可行性容差等) |
| content_hash | TEXT NOT NULL | 快照内容校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')`;对以上全部输入序列化后计算 sha256 |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 推荐索引:`idx_calc_snapshots_version ON calc_snapshots (project_version_id, created_at DESC)`
- 可复现性要求:相同输入(含 program_version/extension_versions/random_seed/tolerances)必然产生相同 `content_hash`;哈希不同=输入不同
- 可变性:不可变,仅 INSERT;禁止 UPDATE/DELETE(触发器与权限同 3.3 模式)

### 7.2 `tasks` 任务(状态机、类型、业务结局、幂等键)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| type | TEXT NOT NULL | `CHECK (type IN ('calc','optimization','uncertainty','import','export','report','dataset_build'))` |
| status | TEXT NOT NULL | `CHECK (status IN ('queued','running','completed','cancelling','cancelled','timed_out','failed'))`;终态:completed / cancelled / timed_out / failed |
| business_outcome | TEXT | `CHECK (business_outcome IS NULL OR business_outcome IN ('normal_completion','no_recommendation','no_feasible_multi_objective','partial_batch','restricted_results','insufficient_evidence'))`;业务结局(区别于执行 status,两者正交:如任务 timed_out 但返回了可行解时,status=timed_out、outcome=restricted_results) |
| idempotency_key | TEXT NOT NULL | `CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$')`;客户端重试幂等键,唯一 |
| calc_snapshot_id | BIGINT | 外键 → `calc_snapshots(id)`;计算类任务必填(应用层校验:type IN ('calc','optimization','uncertainty') 时非空) |
| requested_by | BIGINT NOT NULL | 请求者 → `users(id)` |
| requested_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 请求时间 |
| priority | SMALLINT NOT NULL DEFAULT 0 | 优先级(越大越先调度) |
| deadline | TIMESTAMPTZ | 截止时间(可选) |
| superseded_by_task_id | BIGINT | 取代本任务的任务 → `tasks(id)`(自引用);新任务提交时在旧任务上记录本指针(业务结局枚举不含 superseded,取代关系以本字段表达) |
| attempt_count | INT NOT NULL DEFAULT 0 | 已尝试次数 |
| max_attempts | INT NOT NULL DEFAULT 3 | `CHECK (max_attempts BETWEEN 1 AND 10)`;最大尝试次数 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |

- 主键:`id`
- 唯一约束:`idempotency_key`(幂等:重复提交返回既有任务)
- 推荐索引:`idx_tasks_status ON tasks (status, priority DESC, requested_at)`(调度扫描);`idx_tasks_project ON tasks (project_id, requested_at DESC)`
- 状态机(应用层状态机,写入单元 U07 唯一驱动):
  ```
  queued → running → completed | cancelled | timed_out | failed
  任意未终态(queued/running/cancelling)均可进入 cancelling → cancelled;running 中断(节点崩溃)由租约过期识别 → timed_out → 重试再入 queued(attempt_count+1 ≤ max_attempts);求解超时同样落 timed_out
  ```
  每次状态迁移经 `task_attempts` 记录;`status` 的非法跳转由 U07 校验,数据库层不建模完整状态机(避免过度约束),但终态(`completed/cancelled/timed_out/failed`)禁止再迁移(触发器):
  ```sql
  CREATE FUNCTION tg_tasks_terminal() RETURNS trigger AS $$
  BEGIN
    IF OLD.status IN ('completed','cancelled','timed_out','failed') AND NEW.status <> OLD.status THEN
      RAISE EXCEPTION '终态任务不可迁移状态';
    END IF;
    RETURN NEW;
  END $$ LANGUAGE plpgsql;
  CREATE TRIGGER tg_tasks_terminal BEFORE UPDATE ON tasks
      FOR EACH ROW EXECUTE FUNCTION tg_tasks_terminal();
  ```
- 可变性:仅 status/business_outcome/attempt_count/superseded_by_task_id/updated_at 可 UPDATE(列级触发器限制);`id`、`idempotency_key`、`type`、`calc_snapshot_id`、`requested_by` 不可改;不物理删除

### 7.3 `task_attempts` 任务尝试(尝试序号、心跳、停止原因)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| task_id | BIGINT NOT NULL | 外键 → `tasks(id)` |
| attempt_no | INT NOT NULL | 尝试序号,任务内从 1 递增 |
| worker_id | TEXT | 执行 worker 标识(节点+进程) |
| status | TEXT NOT NULL | `CHECK (status IN ('pending','running','succeeded','failed','stopped'))`;stopped=租约过期/超时被终止 |
| stop_reason | TEXT | 停止原因(超时/节点崩溃/手动终止/错误) |
| started_at | TIMESTAMPTZ | 开始时间 |
| finished_at | TIMESTAMPTZ | 结束时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (task_id, attempt_no)`
- 推荐索引:`idx_task_attempts_task ON task_attempts (task_id, attempt_no DESC)`
- 可变性:仅 status/worker_id/stop_reason/started_at/finished_at 可 UPDATE;attempt_no 与 task_id 不可改;终态行不再迁移

### 7.4 `task_leases` 任务租约(fencing token)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| attempt_id | BIGINT NOT NULL | 外键 → `task_attempts(id)` |
| lease_token | UUID NOT NULL | fencing token;worker 每次写回必须携带,防陈旧 worker 越权写入 |
| acquired_by | TEXT NOT NULL | 获取者(worker 标识) |
| acquired_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 获取时间 |
| renewed_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 最近续租时间(心跳) |
| expires_at | TIMESTAMPTZ NOT NULL | 过期时间;`renewed_at < expires_at` 的租约视为持有中 |
| status | TEXT NOT NULL | `CHECK (status IN ('active','expired','released','revoked'))` |

- 主键:`id`
- 唯一约束:`lease_token`
- 部分唯一索引(SQL):每尝试至多一个有效租约
  ```sql
  CREATE UNIQUE INDEX uq_task_leases_one_active ON task_leases (attempt_id) WHERE status = 'active';
  ```
- 推荐索引:`idx_task_leases_token ON task_leases (lease_token)`;`idx_task_leases_expiry ON task_leases (expires_at) WHERE status = 'active'`(过期回收扫描)
- 租约协议:worker 心跳=`UPDATE task_leases SET renewed_at=now(), expires_at=now()+interval` 并带 `WHERE lease_token = <token> AND status='active'`;影响行数=0 即租约失效,worker 必须停止写回(数据写回同时校验 token,见 7.3 的 fencing 规则);过期租约由守护进程置 `expired` 并把对应 attempt 置 `stopped`
- 可变性:仅 renewed_at/expires_at/status 可 UPDATE;历史租约行保留

### 7.5 `task_progress` 任务进度(PG 持久进度)

Redis 只存可重建部分(实时百分比、阶段文本、秒级心跳);PG 持久化最终进度,每尝试至多一行,用于恢复与审计。

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| attempt_id | BIGINT NOT NULL | 外键 → `task_attempts(id)` |
| progress_percent | NUMERIC(5,2) NOT NULL DEFAULT 0 | `CHECK (progress_percent BETWEEN 0 AND 100)`;完成百分比 |
| stage | TEXT NOT NULL | 当前阶段标识(如 `solve`、`postprocess`) |
| detail | JSONB | 阶段详情(迭代次数、当前目标值等) |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 最后更新 |

- 主键:`id`
- 部分唯一索引(SQL):每尝试至多一行持久进度
  ```sql
  CREATE UNIQUE INDEX uq_task_progress_latest ON task_progress (attempt_id);
  ```
- 推荐索引:`idx_task_progress_attempt ON task_progress (attempt_id)`
- 写入策略:worker 每 5–10 秒一次 `INSERT ... ON CONFLICT (attempt_id) DO UPDATE`;高频心跳只进 Redis(可重建),不写本表
- 可变性:仅 progress_percent/stage/detail/updated_at 可 UPDATE

### 7.6 `task_diagnostics` 任务诊断(不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| task_id | BIGINT NOT NULL | 外键 → `tasks(id)` |
| attempt_id | BIGINT | 外键 → `task_attempts(id)`,可为 NULL(调度前诊断) |
| level | TEXT NOT NULL | `CHECK (level IN ('blocking','error','warning','info'))`;严重程度枚举对齐 04 诊断体系(§5.2) |
| code | TEXT | 诊断码,格式 `<域>-<类别>-<三位序号>`(对齐 04 文档 §5.1),如 `TASK-DATA-002`(快照哈希不匹配) |
| message | TEXT NOT NULL | 可读信息 |
| stack_trace | TEXT | 堆栈(仅 blocking/error) |
| context | JSONB | 上下文(输入摘要、参数片段) |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 时间 |

- 主键:`id`
- 推荐索引:`idx_task_diagnostics_task ON task_diagnostics (task_id, created_at)`;`idx_task_diagnostics_level ON task_diagnostics (level, created_at)`
- 可变性:不可变,仅 INSERT

### 7.7 `compute_slots` 计算并发槽

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| pool_name | TEXT NOT NULL | 资源池名(如 `cpu-pool-1`、`gpu-pool-1`) |
| status | TEXT NOT NULL | `CHECK (status IN ('free','busy','draining','offline'))`;draining=不再接收新任务 |
| capacity | INT NOT NULL DEFAULT 1 | `CHECK (capacity >= 1)`;槽并发度 |
| in_use | INT NOT NULL DEFAULT 0 | `CHECK (in_use >= 0)`;占用数 |
| current_attempt_id | BIGINT | 当前绑定尝试 → `task_attempts(id)`;NULL 表示空闲 |
| last_heartbeat_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 节点心跳(可重建信息:真实心跳仍走 Redis,此处为持久化的最后一次) |

- 主键:`id`
- 行约束:`CHECK (in_use <= capacity)`(由 U07 写入单元维护,保证同一时刻成立)
- 部分唯一索引(SQL):每槽至多绑定一个当前任务
  ```sql
  CREATE UNIQUE INDEX uq_compute_slots_attempt ON compute_slots (current_attempt_id) WHERE current_attempt_id IS NOT NULL;
  ```
- 推荐索引:`idx_compute_slots_pool ON compute_slots (pool_name, status)`
- 可变性:仅 status/in_use/current_attempt_id/last_heartbeat_at 可 UPDATE;槽位本身由运维注册,不物理删除

---

## 8. 结果(U09 结果写入单元 / U13 报告写入单元)

### 8.1 `evidence_packages` 证据包(不可变)

计算/评估产物打包为不可变证据包,大结果内容在对象存储,库中存引用与校验值。

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| task_id | BIGINT NOT NULL | 产生任务 → `tasks(id)` |
| attempt_id | BIGINT | 产生尝试 → `task_attempts(id)` |
| calc_snapshot_id | BIGINT NOT NULL | 输入快照 → `calc_snapshots(id)`(证据与输入一一绑定) |
| object_id | BIGINT NOT NULL | 打包结果对象 → `objects(id)`(对象存储内不可变 blob) |
| content_hash | TEXT NOT NULL | 包内容校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')` |
| status | TEXT NOT NULL | `CHECK (status IN ('complete','partial','invalid'))`;invalid=校验失败不可用 |
| created_by | BIGINT NOT NULL | 创建者(通常为 worker 代理) → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 推荐索引:`idx_evidence_packages_task ON evidence_packages (task_id)`;`idx_evidence_packages_snapshot ON evidence_packages (calc_snapshot_id)`
- 可变性:不可变,仅 INSERT;禁止 UPDATE/DELETE(触发器与权限同 3.3 模式)

### 8.2 `result_assessments` 结果评估(四维,不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| evidence_package_id | BIGINT NOT NULL | 被评估证据包 → `evidence_packages(id)` |
| assessor | TEXT NOT NULL | `CHECK (assessor IN ('system','human'))`;系统自动评估或人工评估 |
| assessed_by | BIGINT | 评估人 → `users(id)`;assessor='human' 时必填(应用层校验) |
| dimension_physical | TEXT NOT NULL | `CHECK (dimension_physical IN ('pass','fail','unknown'))`;物理可行性(潮流/热网/供需平衡) |
| dimension_optimality | TEXT NOT NULL | `CHECK (dimension_optimality IN ('pass','fail','unknown'))`;最优性(间隙达标/多方案排序稳定) |
| dimension_financial | TEXT NOT NULL | `CHECK (dimension_financial IN ('pass','fail','unknown'))`;财务(IRR/NPV 达标、现金流约束满足) |
| dimension_reliability | TEXT NOT NULL | `CHECK (dimension_reliability IN ('pass','fail','unknown'))`;可靠性(失负荷概率、备用裕度) |
| overall_score | NUMERIC(5,2) | `CHECK (overall_score IS NULL OR overall_score BETWEEN 0 AND 100)`;综合得分 |
| comment | TEXT | 评估说明 |
| detail | JSONB | 分维度详情(指标值、阈值、越限项) |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 评估时间 |

- 主键:`id`
- 推荐索引:`idx_result_assessments_evidence ON result_assessments (evidence_package_id, created_at DESC)`
- 可变性:不可变,仅 INSERT;覆盖评估=追加新行,最新引用由 `result_index` 指向

### 8.3 `result_index` 结果索引(仅最新评估引用)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| project_version_id | BIGINT NOT NULL | 外键 → `project_versions(id)`;按版本索引结果 |
| evidence_package_id | BIGINT NOT NULL | 外键 → `evidence_packages(id)` |
| assessment_id | BIGINT | 最新评估引用 → `result_assessments(id)`;NULL=尚无评估;仅指向"最新"一条,历史评估通过 8.2 查询 |
| result_hash | TEXT NOT NULL | 结果业务哈希(输入快照哈希+结果摘要哈希);`CHECK (result_hash ~ '^[0-9a-f]{64}$')` |
| is_latest | BOOLEAN NOT NULL DEFAULT true | 是否为该版本当前最新结果索引行 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 部分唯一索引(SQL):每项目版本至多一条最新结果索引
  ```sql
  CREATE UNIQUE INDEX uq_result_index_latest ON result_index (project_version_id) WHERE is_latest;
  ```
- 推荐索引:`idx_result_index_project ON result_index (project_id, project_version_id DESC)`
- 可变性:仅 `is_latest`/`assessment_id` 可 UPDATE(转交最新标记、挂接新评估);其余列不可改;新结果发布=插入新行并把旧行 `is_latest=false`(U09 同一事务)

### 8.4 `result_selections` 结果选中(业务索引,追加式)

用户从各版本结果中选择"决策采用"结果,历史选中保留。

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| result_index_id | BIGINT NOT NULL | 被选中结果 → `result_index(id)` |
| selected_by | BIGINT NOT NULL | 选择人 → `users(id)` |
| selected_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 选择时间 |
| reason | TEXT | 选择理由 |
| is_current | BOOLEAN NOT NULL DEFAULT true | 是否为当前采用结果 |

- 主键:`id`
- 部分唯一索引(SQL):每项目至多一个当前采用结果
  ```sql
  CREATE UNIQUE INDEX uq_result_selections_current ON result_selections (project_id) WHERE is_current;
  ```
- 推荐索引:`idx_result_selections_project ON result_selections (project_id, selected_at DESC)`
- 可变性:仅 `is_current` 可 UPDATE;换选=新行+旧行置 false(U09 同一事务)

### 8.5 `reports` 报告(Excel 报告对象引用)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| report_type | TEXT NOT NULL | `CHECK (report_type IN ('excel','pdf','html'))` |
| object_id | BIGINT NOT NULL | 报告文件对象 → `objects(id)`(Excel 等二进制体在对象存储) |
| content_hash | TEXT NOT NULL | 文件校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')` |
| generated_by_task_id | BIGINT | 生成任务 → `tasks(id)`,可为 NULL(手工导出) |
| generated_by | BIGINT NOT NULL | 生成者 → `users(id)` |
| generated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 生成时间 |
| status | TEXT NOT NULL | `CHECK (status IN ('generating','ready','failed'))` |

- 主键:`id`
- 推荐索引:`idx_reports_project ON reports (project_id, generated_at DESC)`
- 可变性:仅 `status` 可 UPDATE;报告内容不可改(重新生成=新报告行+新对象)

---

## 9. 不确定性(U10 不确定性写入单元)

### 9.1 `uncertainty_snapshots` 不确定性快照(不可变)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| calc_snapshot_id | BIGINT NOT NULL | 基座计算快照 → `calc_snapshots(id)`(在确定基准上叠加不确定性) |
| method | TEXT NOT NULL | `CHECK (method IN ('monte_carlo','lhs','scenario','robust'))`;采样方法:蒙特卡洛/拉丁超立方/场景/鲁棒 |
| n_samples | INT NOT NULL | `CHECK (n_samples BETWEEN 1 AND 1000000)`;样本数 |
| random_seed | BIGINT NOT NULL | 采样随机种子(与基座种子独立) |
| distributions | JSONB NOT NULL | 变量分布定义(变量名→分布类型与参数) |
| content_hash | TEXT NOT NULL | 快照校验值;`CHECK (content_hash ~ '^[0-9a-f]{64}$')` |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |

- 主键:`id`
- 推荐索引:`idx_uncertainty_snapshots_calc ON uncertainty_snapshots (calc_snapshot_id)`
- 可变性:不可变,仅 INSERT;禁止 UPDATE/DELETE(触发器与权限同 3.3 模式)

### 9.2 `sample_tasks` 采样任务(父子)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| uncertainty_snapshot_id | BIGINT NOT NULL | 所属快照 → `uncertainty_snapshots(id)` |
| parent_task_id | BIGINT | 批次父任务(运行整批样本的驱动任务)→ `tasks(id)`,可为 NULL |
| parent_sample_id | BIGINT | 父采样 → `sample_tasks(id)`(自引用,树形分解:大批拆子批) |
| sample_index | INT NOT NULL | 本层样本序号 |
| depth | INT NOT NULL DEFAULT 0 | `CHECK (depth BETWEEN 0 AND 10)`;树深度 |
| params | JSONB | 本样本参数(采样点取值) |
| status | TEXT NOT NULL | `CHECK (status IN ('queued','running','completed','cancelling','cancelled','timed_out','failed'))`;与 `tasks.status` 同枚举 |

- 主键:`id`
- 唯一约束:顶层样本序号唯一——`UNIQUE (uncertainty_snapshot_id, sample_index) WHERE parent_sample_id IS NULL`(部分唯一索引):
  ```sql
  CREATE UNIQUE INDEX uq_sample_tasks_top ON sample_tasks (uncertainty_snapshot_id, sample_index)
      WHERE parent_sample_id IS NULL;
  ```
  子样本在应用层校验 `(parent_sample_id, sample_index)` 唯一(部分唯一索引 `uq_sample_tasks_child` 同上模式)
- 推荐索引:`idx_sample_tasks_snapshot ON sample_tasks (uncertainty_snapshot_id, status)`;`idx_sample_tasks_parent ON sample_tasks (parent_sample_id)`
- 可变性:仅 `status` 可 UPDATE;样本定义(sample_index/params/depth)不可改

### 9.3 `sample_records` 样本记录

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| sample_task_id | BIGINT NOT NULL | 所属采样 → `sample_tasks(id)` |
| variable_name | TEXT NOT NULL | 变量名(须在 `uncertainty_snapshots.distributions` 中声明) |
| value | NUMERIC(18,4) NOT NULL | 采样值 |
| unit | TEXT | 单位 |

- 主键:`id`
- 唯一约束:`UNIQUE (sample_task_id, variable_name)`(每个采样每个变量一条)
- 推荐索引:`idx_sample_records_task ON sample_records (sample_task_id)`
- 可变性:追加式;样本执行结果写入后不 UPDATE(与 `sample_tasks.status` 迁移由 U10 事务联动)

---

## 10. 审计与对象(U11 对象写入单元 / U12 审计写入单元 / U14 导入 / U15 保留策略)

### 10.1 `objects` 内容寻址对象(元数据、引用计数、配额)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| oid | TEXT NOT NULL | 内容寻址标识(取 sha256 前 64 位十六进制,与 sha256 列一致);`CHECK (oid ~ '^[0-9a-f]{64}$')` |
| sha256 | TEXT NOT NULL | 内容校验值;`CHECK (sha256 ~ '^[0-9a-f]{64}$')`;与 oid 内容相同,两者互为保证 |
| size_bytes | BIGINT NOT NULL | `CHECK (size_bytes >= 0)`;内容字节数 |
| storage_path | TEXT NOT NULL | 对象存储磁盘路径(相对根路径) |
| media_type | TEXT | MIME 类型 |
| status | TEXT NOT NULL DEFAULT 'stored' | `CHECK (status IN ('stored','orphaned','pending_deletion','deleted'))`;orphaned=引用计数归零待清理;pending_deletion=等待物理删除;deleted=已删(元数据保留) |
| ref_count | INT NOT NULL DEFAULT 0 | `CHECK (ref_count >= 0)`;引用计数(由 `object_refs` 等维护,写入单元 U11 原子增减) |
| quota_bytes | BIGINT NOT NULL DEFAULT 0 | `CHECK (quota_bytes >= 0)`;配额(0=不限);对象超配额时写入拒绝 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| last_referenced_at | TIMESTAMPTZ | 最后被引用时间 |

- 主键:`id`
- 唯一约束:`oid`、`sha256`(内容去重:同一内容只存一份)
- 推荐索引:`idx_objects_status ON objects (status, last_referenced_at)`(清理扫描);`idx_objects_path ON objects (storage_path)`
- 不可变性:内容相关列(`oid`/`sha256`/`size_bytes`/`storage_path`)禁止 UPDATE;仅 `status`/`ref_count`/`quota_bytes`/`last_referenced_at` 可 UPDATE(列级触发器限制)
- 删除协议:引用计数归零→`orphaned`→保留期结束(见 `retention_rules`)→`pending_deletion`→物理删除并置 `deleted`

### 10.2 `object_refs` 对象引用清单

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| object_id | BIGINT NOT NULL | 被引用对象 → `objects(id)` |
| ref_type | TEXT NOT NULL | 引用类别(如 `dataset_file`、`evidence_package`、`report`、`version_ref`) |
| ref_entity_type | TEXT NOT NULL | 引用方实体类型(如 `dataset_files`、`evidence_packages`) |
| ref_entity_id | BIGINT NOT NULL | 引用方实体 id |
| purpose | TEXT | 用途说明 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 引用建立时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (object_id, ref_type, ref_entity_type, ref_entity_id)`
- 推荐索引:`idx_object_refs_entity ON object_refs (ref_entity_type, ref_entity_id)`
- 一致性:每增一条引用,`objects.ref_count` 原子 +1;解除引用=删除本行并 -1(U11 事务)
- 可变性:追加式;解除引用=物理删除该行(引用不是权威事实,删除路径唯一允许)

### 10.3 `audit_log` 通用审计日志(不可变)

业务操作通用审计,与 `auth_events`(身份域)互补;两者共用保留策略。

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| entity_type | TEXT NOT NULL | 实体类型(表名,如 `project_versions`) |
| entity_id | BIGINT NOT NULL | 实体 id |
| action | TEXT NOT NULL | 动作(如 `insert`、`revoke`、`transfer`) |
| actor_id | BIGINT | 操作者 → `users(id)`,可为 NULL(系统动作) |
| actor_type | TEXT NOT NULL | `CHECK (actor_type IN ('user','system','admin'))` |
| occurred_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 发生时间 |
| ip | INET | 来源 IP |
| before | JSONB | 变更前关键字段 |
| after | JSONB | 变更后关键字段 |
| request_id | TEXT | 请求追踪 id |
| trace_id | TEXT | 分布式链路 id |

- 主键:`id`
- 推荐索引:`idx_audit_log_entity ON audit_log (entity_type, entity_id, occurred_at DESC)`;`idx_audit_log_time ON audit_log (occurred_at)`;`idx_audit_log_actor ON audit_log (actor_id, occurred_at DESC)`
- 可变性:不可变,仅 INSERT;禁止 UPDATE/DELETE;按 `retention_rules` 归档分区

### 10.4 `import_proposals` 导入提议(外部数据入库前的评审记录)

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| project_id | BIGINT NOT NULL | 外键 → `projects(id)` |
| proposer_id | BIGINT NOT NULL | 提议人 → `users(id)` |
| source_type | TEXT NOT NULL | `CHECK (source_type IN ('excel','csv','json','dxf','gis','other'))` |
| source_hash | TEXT NOT NULL | 源文件校验值;`CHECK (source_hash ~ '^[0-9a-f]{64}$')`;源文件本体按对象入对象存储 |
| source_path | TEXT | 源文件对象存储路径(冗余快照) |
| status | TEXT NOT NULL DEFAULT 'proposed' | `CHECK (status IN ('proposed','validated','approved','rejected','applied'))`;applied=已导入并生成数据集/版本 |
| review_summary | JSONB | 评审结论(字段映射、行数、冲突) |
| review_errors | JSONB | 校验错误清单 |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| decided_by | BIGINT | 决策人 → `users(id)` |
| decided_at | TIMESTAMPTZ | 决策时间 |

- 主键:`id`
- 推荐索引:`idx_import_proposals_project ON import_proposals (project_id, status)`
- 可变性:追加式;仅 `status`/`review_summary`/`review_errors`/`decided_by`/`decided_at` 可 UPDATE(列级触发器限制);applied 后不可再变

### 10.5 `retention_rules` 保留策略

| 列名 | 类型 | 约束 / 说明 |
|---|---|---|
| id | BIGINT PK IDENTITY | 主键 |
| entity_type | TEXT NOT NULL | 适用实体类型(如 `auth_events`、`audit_log`、`objects`、`task_diagnostics`) |
| object_kind | TEXT NOT NULL | 对象类别(如 `evidence_package`、`dataset_file`、`log`);与 entity_type 组合成策略键 |
| retention_days | INT NOT NULL | `CHECK (retention_days BETWEEN 1 AND 36500)`;保留天数 |
| apply_to | TEXT NOT NULL DEFAULT 'all' | `CHECK (apply_to IN ('all','orphaned','referenced'))`;all=全部 / orphaned=仅孤儿对象 / referenced=仅被引用对象 |
| status | TEXT NOT NULL DEFAULT 'active' | `CHECK (status IN ('active','paused'))` |
| created_by | BIGINT NOT NULL | 创建者 → `users(id)` |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | 最后更新时间 |

- 主键:`id`
- 唯一约束:`UNIQUE (entity_type, object_kind, apply_to)`
- 可变性:策略本身可 UPDATE(status/retention_days/apply_to);策略变更历史写入 `admin_maintenance_actions`

---

## 11. 迁移顺序建议

建表顺序由外键依赖与循环引用决定;循环引用(projects → drafts → projects)通过"先建表、后补指针列"解决。每步在同一迁移事务内;表上触发器与部分唯一索引随该表创建同步建立。

| 步骤 | 表 | 说明 |
|---|---|---|
| 1 | `users`, `roles`, `user_roles`, `credentials` | 身份基础,无外部依赖(credentials.created_by/users 自引用同批) |
| 2 | `objects`, `object_refs` | 内容寻址对象先行,后续所有引用方依赖 `objects(id)` |
| 3 | `projects`(不含指针列) | 项目主表;同时建 `admin_maintenance_actions`(仅依赖 users) |
| 4 | `drafts`, `project_versions`, `version_refs` | 草稿与不可变版本;`version_refs` 依赖 objects |
| 5 | `ALTER TABLE projects ADD COLUMN current_draft_id ... , current_version_id ...` | 补项目指针列(循环依赖解除),并加部分唯一/一致性校验 |
| 6 | `project_members`, `ownership_transfers` | 权限,依赖 projects;`ownership_transfers` 在成员表之后(业务顺序) |
| 7 | `system_graphs`, `devices`, `ports`, `connections` | 系统模型,依赖 projects/drafts/project_versions |
| 8 | `datasets`, `dataset_versions`, `dataset_files` | 数据集,依赖 projects/objects |
| 9 | `calc_configs`, `calc_snapshots` | 计算配置与快照,依赖 projects/project_versions |
| 10 | `tasks`, `task_attempts`, `task_leases`, `task_progress`, `task_diagnostics`, `compute_slots` | 任务域,依赖 projects/calc_snapshots;`tasks.superseded_by_task_id` 自引用同批 |
| 11 | `window_sessions`, `auth_events`, `audit_log` | 会话与审计,依赖 users;`window_sessions.replaced_by_session_id` 自引用同批 |
| 12 | `evidence_packages`, `result_assessments`, `result_index`, `result_selections`, `reports` | 结果域,依赖 tasks/calc_snapshots/objects |
| 13 | `uncertainty_snapshots`, `sample_tasks`, `sample_records` | 不确定性,依赖 calc_snapshots/tasks |
| 14 | `import_proposals`, `retention_rules` | 导入与保留策略,依赖 projects/users |
| 15 | 全部不可变表触发器与部分唯一索引复核;`auth_events`/`audit_log` 按月分区(可选) | 收尾:验证 `REVOKE UPDATE, DELETE` 覆盖、触发器就位、写入单元权限矩阵生效 |

关键依赖链(拓扑顺序的强制原因):

- `objects` → 一切 `object_id` 外键(version_refs/dataset_files/evidence_packages/reports/object_refs)
- `projects` → drafts / project_versions / project_members / 模型 / 数据集 / 配置 / 任务 / 结果
- `project_versions` → calc_snapshots → tasks → evidence_packages → result_index
- 循环引用仅一处:`projects.current_draft_id → drafts → projects`,由步骤 3/4/5 的"后置 ALTER"解决
- 自引用外键(`drafts.parent_draft_id`、`project_versions.parent_version_id`、`window_sessions.replaced_by_session_id`、`tasks.superseded_by_task_id`、`sample_tasks.parent_sample_id`)均在同一批建表内声明,PG 支持同表自引用

不可变表清单(仅 INSERT,禁止 UPDATE/DELETE):`auth_events`、`admin_maintenance_actions`、`project_versions`、`version_refs`、`dataset_versions`、`dataset_files`、`calc_snapshots`、`task_diagnostics`、`evidence_packages`、`result_assessments`、`uncertainty_snapshots`、`audit_log`。半不可变(允许列级更新)表在其定义中注明"仅可更新列"。

---

## 附录:写入单元与表归属

| 写入单元 | 权威表 |
|---|---|
| U01 身份 | users, roles, user_roles, credentials, window_sessions, auth_events |
| U02 权限 | project_members, ownership_transfers |
| U03 项目 | projects, drafts, project_versions, version_refs |
| U04 模型 | system_graphs, devices, ports, connections |
| U05 数据集 | datasets, dataset_versions, dataset_files |
| U06 配置 | calc_configs |
| U07 任务 | tasks, task_attempts, task_leases, task_progress, task_diagnostics, compute_slots |
| U08 快照 | calc_snapshots |
| U09 结果 | evidence_packages, result_assessments, result_index, result_selections |
| U10 不确定性 | uncertainty_snapshots, sample_tasks, sample_records |
| U11 对象 | objects, object_refs |
| U12 审计 | audit_log |
| U13 报告 | reports |
| U14 导入 | import_proposals |
| U15 保留策略 | retention_rules |
| U16 管理维护 | admin_maintenance_actions |





