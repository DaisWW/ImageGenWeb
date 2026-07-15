# Snow AI Studio

公司内部 AI 生图工作站。每个用户最多创建 10 个工作站，在同一时间线中完成 GPT 需求对话、提示词整理、排队、生成、取消和结果查看。

主要规则：

- 账户由管理员创建并充值，不开放注册。
- GPT 对话免费；只按成功生成的图片扣 RMB，失败或取消会释放预占金额。
- 每条聊天记录保存发送时间，助手记录额外保存响应耗时。
- 生成期间当前工作站禁止继续对话，但可以取消任务。
- 聊天附件默认作为垫图；单次生成使用用户选择的一条渠道，不做多渠道轮询。
- 图片和生成明细保留 30 天，余额流水长期保留。
- 浏览器不会收到渠道或聊天 API Key。

## 工程结构

```text
imagegen/
  config/         渠道、聊天模型、加密配置和热刷新
  integrations/   OpenAI 兼容聊天与图片上游适配器
  services/       账户、计费、工作站、会话、生图和清理策略
  web/            页面、工作站、生成媒体和管理后台路由
config/           数据库尚无管理员配置时使用的兼容默认值
static/           css、js、图片与第三方静态资源
templates/        页面模板和共享局部模板
tests/integration/ 业务与 HTTP 合同测试
```

详细模块边界、事务归属和扩展规则见 [`docs/architecture.md`](docs/architecture.md)。根目录保留启动脚本、Docker、Alembic 和依赖清单；`data/`、`outputs/`、`backups/`、`.ui-test-data/` 均为运行时数据，不属于源码结构。

## Docker 部署

需要 Docker Desktop 或 Docker Engine（含 Compose）。Windows 首次部署直接双击：

```powershell
.\deploy-docker.cmd
```

脚本默认只监听 `127.0.0.1:18081`；同时自动生成数据库密码、加密密钥和首次管理员密码，构建容器、等待健康检查、注册当前用户登录自启。

确需在可信局域网共享时，显式启用 LAN 模式：

```powershell
.\deploy-docker.ps1 -Lan
```

LAN 模式会监听所有网卡并配置仅允许本地子网访问的 Windows 防火墙规则。它仍使用明文 HTTP，登录密码和会话 Cookie 可被同网段观察；跨机器或跨网段共享前必须配置 TLS 反向代理，并设置 `COOKIE_SECURE=true`、`TRUST_PROXY_HEADERS=true`。后一个开关只适用于请求必经一个可信反向代理的部署，不能在应用直接暴露时启用。`-LocalOnly` 仅为旧部署脚本兼容参数，新部署无需指定。

端口被占用时可从 PowerShell 指定其他端口：

```powershell
.\deploy-docker.ps1 -Port 18082
```

本地开发仍使用 `7860`，Docker 对外端口为 `18081`，二者不会冲突。如通过单个 HTTPS 反向代理部署，将 `.env` 中的 `COOKIE_SECURE` 和 `TRUST_PROXY_HEADERS` 都改为 `true`。

Compose 包含三个容器：

- `web`：Flask/Gunicorn Web 服务，启动前执行 Alembic 数据库迁移。
- `worker`：独立队列 Worker，执行生图、结算和 30 天清理。
- `db`：PostgreSQL 17。

当前架构明确只支持一个 Gunicorn Web 进程和一个 Worker。登录限流、对话与工作站互斥是 Web 进程内状态，Worker 则通过数据库租约拒绝第二个活跃实例；不要通过增加 Gunicorn worker 数或复制 Compose 服务进行水平扩容。

数据库与图片分别保存在 `postgres-data`、`imagegen-data` 命名卷。渠道、模型、价格、队列和上下文配置也保存在 PostgreSQL，重建容器不需要重新修改 YAML。服务默认 `restart: unless-stopped`：Docker 引擎恢复后容器会自动恢复。Windows Docker Desktop 由启动目录入口在当前用户登录后启动；Linux Docker Engine 设置为系统服务后可在无人登录时随系统启动。

工程目录迁移后重新运行一次 `deploy-docker.cmd`，脚本会更新登录自启快捷方式；固定 Compose 项目名会继续使用原有数据库和图片卷。

查看日志与更新：

```powershell
docker compose logs -f web worker
docker compose up -d --build
```

备份数据库和原图：

```powershell
py scripts/backup.py
```

命令会短暂停止当前正在运行的 Web 与 Worker，在没有应用写入时生成一致的数据库和文件快照，然后只恢复备份前处于运行状态的服务。结果位于 `backups/<时间>/`，包含 `database.dump`、`files.tar.gz` 和权限收紧的 `deployment.env`。后者包含恢复数据库中加密 API Key 所必需的 `CONFIG_ENCRYPTION_KEY`/`SECRET_KEY`，必须与数据库备份一起离线加密保管；整个备份目录都不要提交到版本库。

## 本地运行

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "至少 10 位强密码"
.\start.ps1
```

`start.ps1` 同时启动 Web 与 Worker。首次登录后，在“管理后台 → 渠道与模型”中配置生图与对话 API。

数据库为空时才使用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 创建首个管理员。系统不开放注册。

## 管理员配置

管理后台支持维护：

- 生图渠道、API 地址与 Key、模型、每张价格、能力和渠道并发。
- 全局并发、排队上限、记录保留天数和异常任务恢复时间。
- OpenAI 兼容的对话模型、API 地址与 Key、推理强度、上下文策略和系统提示词。
- 站点标题、用户资料、余额、用户并发和密码。
- 工作站与素材配额、消息和附件上限、对话并发、生成数量、动画参数与 Worker 周期。
- 运行日志与操作审计，可按时间、用户、模型、渠道、错误码和关联 ID 检索。

保存后的配置由 Web 与 Worker 从同一数据库热加载。API Key 使用 `CONFIG_ENCRYPTION_KEY` 加密；未设置时使用 `SECRET_KEY`。这两个值在配置保存后必须保持稳定，浏览器和审计记录都不会收到明文 Key。

“管理后台 → 日志”记录对话、生图、Worker 和 Web 异常的结构化事件，并为用户侧错误返回可关联的错误 ID。日志只保存响应结构摘要，不保存完整提示词、消息、图片、Authorization 或 API Key；运行日志保留天数可在系统设置中调整，操作审计不随运行日志清理。

`config/channels.yaml` 与 `config/chat_models.yaml` 仅作为首次启动的兼容默认值。数据库中尚未保存管理员配置时，应用才读取它们和对应环境变量；日常运维不需要直接修改配置文件。

数据库连接、监听端口、存储路径、会话密钥和配置加密密钥属于启动与安全参数，由部署脚本和环境变量管理，不在 Web 后台开放修改。

当前默认生图配置：

| 渠道 | 模型 | 单价 | 渠道并发 |
|---|---|---:|---:|
| 刀哥的 | `gpt-image-2` | ¥0.0600/张 | 2（待上游余额/频控恢复后复测） |
| Lucen | `gpt-image-2` | ¥0.0900/张 | 4 |

全局并发为 6，实际调度同时受全局并发、渠道并发和用户并发限制。

## 并发实测

2026-07-14 使用 `gpt-image-2`、`1024x1024`、`low`、PNG 测试 Lucen：

| 并发 | 请求数 | 成功 | 总耗时 |
|---:|---:|---:|---:|
| 1 | 1 | 1 | 32.0 秒 |
| 2 | 4 | 4 | 62.3 秒 |
| 3 | 6 | 6 | 62.2 秒 |
| 4 | 8 | 8 | 66.8 秒 |
| 6 | 12 | 12 | 88.0 秒 |
| 8 | 16 | 16 | 97.0 秒 |

并发 8 仍为 `16/16` 成功，但并发 6 起长尾明显，因此生产默认取 4。刀哥的渠道复测时单请求即返回“请求过于频繁或余额不足”，暂不能据此判断上限，默认并发 2 属于保守值。

## 验证

```powershell
.\.venv\Scripts\python.exe -m compileall -q imagegen app.py run_worker.py
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check imagegen tests app.py run_worker.py migrations scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\pip-audit.exe -r requirements.txt
```

测试覆盖账户与余额、工作站上限、聊天时间/耗时、附件隔离、提示词翻译、生成锁定、批量预占、取消退款、成功/失败结算、配置加密与版本冲突、热刷新和 30 天清理。
