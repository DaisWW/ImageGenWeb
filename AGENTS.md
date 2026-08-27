# Snow AI Studio 项目规则

Scope: Snow AI Studio repository root。

这是一个内部 Flask 生图工作站，包含 Web、单个生成 Worker、PostgreSQL、Docker Compose
和可选 Lucida GPU 服务。稳定规则放在本文件，领域细节按任务读取 .codex/ 文档；运行数据、
备份、依赖和测试产物默认不读。

## 默认行为

- 默认用中文交流，除非用户要求其他语言。
- 修改保持最小、可回滚，保留无关工作区改动；优先复用现有服务、事务边界、配置仓储、Worker
  和测试替身，不为单次需求增加抽象。

## 项目概览

- imagegen/ 是 Flask 应用：config、integrations、services、web、models 和 storage。
- migrations/ 是 Alembic 数据库迁移；static/ 和 templates/ 是浏览器资源。
- tests/integration/ 覆盖业务与 HTTP 合同，tests/e2e/ 覆盖真实浏览器流程。
- 生产部署契约是单个 Gunicorn Web 进程和单个 Worker；Worker 用数据库租约拒绝第二个
  活跃实例，不能靠增加进程或 Compose 副本水平扩容。
- PostgreSQL 保存业务/配置状态，文件系统保存图片；data/、output/、outputs/、
  backups/、.ui-test-data/ 和 test-results/ 是运行或测试产物，不是源码。

## 上下文加载

- 先读本文件，再按任务读取 README/architecture 和最多两份相关 .codex 文档；跨领域才追加。
- HTTPS、Lucida、CR、文档和语义流程分别读取对应 docs 或 workflow。
- node_modules/、.venv/、backups/、data/、output/、.ui-test-data/、test-results/ 和生成图片默认不读不改。

## 工作流路由

- Bug/生成失败/计费/部署走 Triage Issue；调用链审查走 Logic Bug Review；模糊方案走 Grill Me。
- Flask、SQLAlchemy、Alembic、Worker、外部 API、前端或 Docker 的 CR/精简加载 code-review；
  写文档加载 doc-writing；普通语义流程加载 workflows。
- 用户未明确要求时不主动提交 Git；提交时使用固定中文格式
  “【模块】动词开头的简单说明”，不使用 feat:、fix: 等英文前缀。

## 默认编码闭环

- 明确范围和可验证结果，只改相关文件，保留他人改动。
- 运行最窄检查，查看完整 diff，复核事务、结算、文件一致性、并发、权限、密钥和部署影响。

## Python、Flask 与分层

- 使用现有 Ruff/格式和类型标注；不在 import 时执行迁移、启动 Worker、写业务数据或
  访问外部服务。
- 路由只处理 HTTP 结构、鉴权和序列化；业务决策与事务归属保持在 services，外部上游
  适配器位于 integrations，配置通过 config 读取。
- API Key 只存在加密配置存储和服务端配置对象；日志、错误响应、审计和浏览器不能返回
  明文密钥、Authorization、完整提示词或上游响应正文。
- 用户输入、文件名、图片尺寸、参考图数量、提示词和分页都要沿用现有边界校验。

## 数据、文件与 Worker

- 服务层负责自己的数据库提交；集成层不提交事务，路由不绕过服务直接写模型。
- 图片先安全写入再提交元数据；失败回滚时删除文件。删除/保留期清理失败时保留元数据
  供下一次重试，不留下悬空记录或孤立图片。
- 生成队列、用户/全局并发、Worker lease、心跳、恢复和结算必须保持幂等；取消、失败、
  重试和成功不能重复扣费或漏退预占。
- 单个 GenerationItem 保存最终实际提示词、渠道、价格和结果；不要用全局配置覆盖历史
  任务，也不要在路由中直接调用上游生图。

## 数据库与迁移

- PostgreSQL 事务、行锁、唯一约束和数据库租约是并发正确性的组成部分；修改模型或 SQL
  时检查旧数据、空数据、重复请求、回滚和升级路径。
- 每个 Alembic migration 必须可在干净库和现有库执行；迁移后运行 alembic check。
- 不直接修改生产数据库或提交运行备份；恢复操作必须显式确认并保留当前 .env/密钥策略。

## 前端、外部服务与安全

- 前端状态不能取代服务端权限、计费或并发校验；检查连点、取消、轮询、失败重试、长文本、
  响应式布局和页面离开。
- 外部 HTTP 有超时、状态/JSON 校验和有限重试；同一图片的重试、渠道熔断和费用预占必须
  可追踪且不会重复向用户扣费。
- Docker 镜像不要包含 .env、数据库、图片、备份或模型权重；部署必须保留命名卷和密钥，
  单 Web/Worker 约束不得被脚本或文档悄悄改变。

## 完成标准

- 受影响检查通过，或明确说明无法运行的命令和残余风险。
- 完整 diff 不包含运行数据、密钥、备份、图片或无关格式化；文档路由与实际模块一致。
