# 组合根与启动

> 文档状态：生效蓝图；目标代码边界：`backend/iesplan/bootstrap/` 与进程入口

## 作用

组合根是系统唯一知道“这次部署具体使用哪些实现”的位置。它读取部署配置，发现 devices、modeling、generator、executor、result adapter 和 storage provider，构造各模块独立状态，注入 application/API/Worker，并决定实例是否具备就绪条件。

如果每个业务模块都自行读取环境和选择实现，依赖会变成隐式全局状态；组合根把这种选择集中成可观察、可测试的一次装配。

## 边界

组合根负责：

- 读取并严格校验进程级配置；
- 发现候选 provider，解析版本与依赖；
- 按依赖顺序构造模块并原子发布各自目录；
- 建立数据库、缓存、对象存储和应用用例所需连接；
- 注册 API 路由或 Worker 命令；
- 聚合模块公开健康状态并决定 readiness；
- 在停止时按逆序释放资源。

组合根不实现业务规则，不读取模块私有注册表，不把各模块注册项合并成一个全局表，也不修改业务数据来“修复”启动。

用户算法包不作为启动 provider 候选。组合根只注册受信任的通用沙箱 GeneratorProvider、ExecutorProvider、ResultAdapterProvider 和 `plugin_runner` 客户端，并验证其协议版本、Python runtime、隔离能力和最小自检；具体用户包由任务快照按摘要传递。这样可以在运行期增加用户目录内容，同时不热加载 API 或 Worker 模块，也不改变标准计算链。

## 输入与输出

| 输入 | 要求 |
|---|---|
| 部署配置 | 来源明确、类型严格、秘密不进入日志 |
| provider 候选 | ID、版本、能力、依赖和配置 schema 完整 |
| 基础连接 | 数据库、缓存、BlobStore 等可验证 |
| 进程角色 | API、compute Worker、I/O Worker 等能力边界明确 |

输出是一个完整 `ApplicationContext` 或明确启动失败。context 只向调用方暴露模块公开门面、用例和健康能力，不暴露内部 registry 与连接实现。

## 启动顺序

```text
读取配置并校验秘密/类型
    ↓
建立基础连接并验证最小能力
    ↓
发现 provider 候选
    ↓
分别构造 devices / modeling / generators / executors / result adapters / storage 候选状态
    ↓
交叉验证公开 ID、版本和能力引用
    ↓
一次性发布各模块状态
    ↓
装配 application、API 或 Worker
    ↓
进入 ready
```

停止流程按逆序执行：先停止承接新请求/任务，再等待或取消在途工作，最后释放 provider 和基础连接。

## 增加一个 provider 类型

1. 由所属模块定义 provider contract、ID、版本和能力；
2. 在组合根增加发现与配置装配，不在业务模块读取环境；
3. 明确它依赖哪些已发布模块能力；
4. 在候选阶段完成所有校验；
5. 增加成功、缺依赖、版本不匹配和部分失败测试；
6. 把 readiness 与 provider 的“能否正确服务”关联；
7. 记录实际装配版本，供快照和运维查看。

## 失败语义

- 配置缺失或类型错误：进程启动失败，指出配置键但不泄露秘密值；
- 必需 provider 不存在：不 ready，不使用静态 fallback；
- provider 构造部分失败：候选集合整体废弃，旧状态不被清空；
- 公开引用无法解析：启动依赖校验失败；
- 可选能力失败：只有 contract 明确允许时才以明确 degraded 状态启动；
- 运行期依赖丢失：readiness 反映真实承接能力，不伪装健康。

## 必须遵循的规范

- 只有组合根选择具体实现和读取进程环境；
- 每个模块独立拥有注册状态；
- 正式发布前不做运行期热加载；
- 配置对象不可在模块间作为可变全局字典共享；
- healthz 与 readyz 语义分离；
- 启动日志记录 ID、版本和状态，不记录凭证和内部数据。

## 完成标准

- 各进程角色只装配其需要的能力；
- 启动成功意味着全部必需依赖可用且引用可解析；
- 任一失败不会发布半初始化模块状态；
- 测试可以注入替代 provider，不依赖修改全局变量；
- 关闭流程不会继续领取任务或留下未释放资源。

代码阅读从 API/Worker 进程入口开始，沿配置读取、provider 初始化和 readiness 聚合阅读；迁移完成后这些选择集中在 `bootstrap`，业务模块不再各自执行启动发现。
