# 临时在线开发快照

日期：2026-10-23。临时交接记录，不是产品规范。

本次将主工作树源码、正式手册、指导文件和可共享过程文档纳入 Git；仓库内原有两份 `.gitignore` 的规则临时注释。

继续处理用户反馈前，阅读[主审问题清单](../reviews/2026-10-23-0.15-user-issues-triage.md)及[决策状态交接（脱敏版）](../reviews/2026-10-23-0.15-reviewer-decision-reconciliation.md)。后者只传递确认状态，不包含真实案例参数，不能代替完整授权输入。

密钥、盲测私有资料、验收原始归档、数据库、依赖／构建缓存、测试产物和独立工作树未上传。本地通过 `.git/info/exclude` 排除这些内容；该文件不会随 clone 分发。在线环境首次提交前也要设置本地排除，不能直接把运行环境全部加入 Git。

在仓库根目录可先复用原版本的本地排除规则（保留本次对文档的开放）：

```sh
git show b5411bb:.gitignore | sed '/^\/docs\/$/d; /^CLAUDE.md$/d; /^AGENTS.md$/d' >> .git/info/exclude
```

另将本机的密钥／环境文件、私有验收资料和测试产物加入本地排除；提交前检查 `git diff --cached --name-only`，不要推送真实案例数据、会话凭据或数据库。

在线开发结束后，原有规则可恢复为：

```sh
git restore --source=b5411bb -- .gitignore frontend/tests/e2e/.gitignore
```

恢复忽略规则不会自动取消已经跟踪的文档。届时应单独审查是否继续公开这些文档，并按用户决定调整索引；不删除本地文档，不擅自改写远端历史。
