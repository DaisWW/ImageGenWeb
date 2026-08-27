# Snow AI Studio 工作流

由根 `AGENTS.md` 按任务路由；产品、架构和备份行为以对应文档为准。

## Triage Issue

- 追踪请求、鉴权、服务、事务、Worker、上游、文件与响应；优先修根因。
- 用测试应用、fake provider 和临时数据库增加最窄回归测试，再做最小修复；不触碰真实图片、余额、Key 或生产卷。
- 运行受影响 pytest/Ruff/编译；跨层改动再跑完整 CI 等价检查。真实依赖不可用时明确记录。

## Logic Bug Review

- 按入口、权限、状态/数据流、事务、队列认领、结算、取消/恢复和清理组成完整链路。
- Findings-first，只报告有触发条件、证据和影响的问题；修复后复查调用点、迁移、测试和完整 diff。

## CR 精简

- 先确定行为基线和允许范围，只处理有证据的重复、死代码、职责混杂、隐藏副作用或耦合。
- 一次精简只做一个主题，不混入重命名、格式化、依赖升级或功能变更。
- 详细检查见 `docs/code-review.md`；改后复查调用点、边界、完整 diff 和最窄测试。

## Schema、备份与部署

- 修改模型/迁移前检查现有版本链、空库和回滚；运行 `alembic upgrade head` 与 `alembic check`。
- 修改备份/恢复/Compose 前确认 Web/Worker 停止顺序、命名卷、密钥、健康检查和单实例约束；不覆盖 `.env` 或运行数据。
- HTTPS、反向代理和 Lucida 按对应 docs 验证。

## Grill Me

- 先读代码、README 和架构约束；一次只问一个会改变实现方向的问题，并说明更简单方案和不可逆风险。

## 验证

- Python：`python -m ruff check .`、目标 `ruff format --check`、`python -m compileall -q imagegen app.py run_worker.py`。
- 测试：`python -m pytest -q`；迁移：`alembic upgrade head`、`alembic check`；浏览器：`npm run test:e2e`。
- 依赖：`python -m pip_audit -r requirements.txt`、`npm audit --audit-level=high`。

## 文档

- 稳定规则放 `AGENTS.md`，领域说明放 `.codex/docs/`，短流程放本文件，历史迁移放 `.codex/archive/`。

## Git 提交流程

仅用户明确要求提交或整理提交时执行：

- 先看 `git status --short`、`git diff --stat` 和目标文件完整 diff；排除无关改动，不使用 `git add -A`、`git commit -a` 或宽泛路径。
- 一条提交只表达一个主题；标题必须是中文 `【模块】动词开头的简单说明`，不使用 `feat:`/`fix:`，建议不超过 30 字。
- 正文写改动与验证，命令显式列出文件；提交后核对 hash、文件数、增删行数和验证结果，不改写公共历史。
